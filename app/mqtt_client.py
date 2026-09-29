"""异步 MQTT 客户端封装（基于 gmqtt）。

gmqtt 各版本 API 存在差异，这里做兼容处理：
  - 遗嘱消息（LWT）：优先用构造函数 will_message；部分版本提供 set_will_message；
    都不可用时自动跳过，不影响启动
  - MQTT 协议版本常量：新版在 gmqtt.mqtt.constants，旧版可能在其他位置；
    取不到时交由 gmqtt 自行协商
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Callable, Dict, Optional

import gmqtt

logger = logging.getLogger(__name__)


def _mqtt_version_311():
    """取 MQTT 3.1.1 版本常量，取不到则返回 None（由 gmqtt 自行协商）。"""
    for module_path in ("gmqtt.mqtt.constants", "gmqtt.constants"):
        try:
            module = __import__(module_path, fromlist=["MQTTv311"])
            return getattr(module, "MQTTv311")
        except Exception:  # noqa: BLE001
            continue
    return None


def _client_accepts_will_message() -> bool:
    """判断 gmqtt.Client 构造函数是否支持 will_message 参数。"""
    try:
        sig = inspect.signature(gmqtt.Client.__init__)
    except (TypeError, ValueError, AttributeError):
        return False
    return "will_message" in sig.parameters


async def _maybe_await(result):
    """兼容同步/异步两种返回。

    gmqtt 0.7.0 的 subscribe() 是同步方法（返回消息 id），
    而部分版本返回 awaitable。这里统一处理，避免 await 非 awaitable 报错。
    """
    if inspect.isawaitable(result):
        return await result
    return result


class MQTTClient:
    """带断线重连的异步 MQTT 客户端。"""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "c4-ha-bridge",
        topic_prefix: str = "c4-ha",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.topic_prefix = topic_prefix

        self.state_topic = "{}/bridge/state".format(topic_prefix)

        self._client: Optional[gmqtt.Client] = None
        self._subscriptions: Dict[str, Callable] = {}
        #: 连接就绪事件，bridge 启动时用于等待 MQTT 可用
        self.connected = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        self._client = self._build_client()

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        connect_kwargs = {"keepalive": 60}
        version = _mqtt_version_311()
        if version is not None:
            connect_kwargs["version"] = version

        logger.info("正在连接 MQTT Broker %s:%s ...", self.host, self.port)
        await self._client.connect(self.host, port=self.port, ssl=False,
                                   **connect_kwargs)

    def _build_client(self) -> gmqtt.Client:
        """构造 Client，并尽力设置遗嘱消息（各版本 API 不同，失败不阻断）。"""
        will = self._build_will_message()
        params = {}
        if will is not None and _client_accepts_will_message():
            params["will_message"] = will

        client = gmqtt.Client(self.client_id, clean_session=True, **params)

        if self.username:
            client.set_auth_credentials(self.username, self.password)

        if "will_message" in params:
            return client

        # 构造函数不支持时，尝试其它设置方式
        setter = getattr(client, "set_will_message", None)
        if callable(setter):
            for args, kwargs in (
                ((self.state_topic, "offline"), {"qos": 1, "retain": True}),
                ((self.state_topic, "offline"), {}),
            ):
                try:
                    setter(*args, **kwargs)
                    logger.debug("已通过 set_will_message 设置遗嘱消息")
                    return client
                except Exception as exc:  # noqa: BLE001
                    logger.debug("set_will_message 调用失败: %s", exc)

        if will is not None:
            try:
                client._will_message = will  # 兜底：直接写内部字段
                logger.debug("已通过 _will_message 设置遗嘱消息")
            except Exception as exc:  # noqa: BLE001
                logger.debug("设置遗嘱消息失败，跳过 LWT: %s", exc)
        else:
            logger.debug("当前 gmqtt 未提供 Message 类，跳过 LWT")

        return client

    def _build_will_message(self):
        """构造遗嘱消息对象；gmqtt 未导出 Message 时返回 None。"""
        message_cls = getattr(gmqtt, "Message", None)
        if message_cls is None:
            return None
        try:
            return message_cls(self.state_topic, "offline", qos=1, retain=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("构造遗嘱消息失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------
    def _on_connect(self, client, flags, rc, properties):
        logger.info("已连接 MQTT Broker (rc=%s)", rc)
        self.connected.set()
        asyncio.create_task(self._resubscribe_all())
        asyncio.create_task(self.publish(self.state_topic, "online", retain=True))

    def _on_disconnect(self, client, packet, exc=None):
        logger.warning("与 MQTT Broker 断开连接")
        self.connected.clear()

    async def _resubscribe_all(self) -> None:
        """重连后恢复所有订阅。"""
        await self.connected.wait()
        for topic in self._subscriptions:
            try:
                await _maybe_await(self._client.subscribe(topic, qos=1))
                logger.debug("已重新订阅 %s", topic)
            except Exception as exc:  # noqa: BLE001
                logger.error("重新订阅 %s 失败: %s", topic, exc)

    # ------------------------------------------------------------------
    # 订阅 / 发布
    # ------------------------------------------------------------------
    async def subscribe(self, topic: str, callback: Callable) -> None:
        self._subscriptions[topic] = callback
        if self.is_connected and self._client:
            try:
                await _maybe_await(self._client.subscribe(topic, qos=1))
                logger.debug("已订阅 %s", topic)
            except Exception as exc:  # noqa: BLE001
                logger.error("订阅 %s 失败: %s", topic, exc)

    def _on_message(self, client, topic, payload, qos, properties):
        msg = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        logger.debug("收到 MQTT 消息 %s: %s", topic, msg[:200])

        callback = self._subscriptions.get(topic)
        if callback:
            asyncio.create_task(self._safe_callback(callback, topic, msg))
            return

        for sub_topic, cb in self._subscriptions.items():
            if self._topic_matches(sub_topic, topic):
                asyncio.create_task(self._safe_callback(cb, topic, msg))
                return

    async def _safe_callback(self, callback: Callable, topic: str, payload: str):
        try:
            await callback(topic, payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("处理 %s 的消息出错: %s", topic, exc, exc_info=True)

    @staticmethod
    def _topic_matches(subscription: str, topic: str) -> bool:
        """简易通配符匹配（支持 + 与 #）。"""
        sub_parts = subscription.split("/")
        topic_parts = topic.split("/")

        for i, sub_part in enumerate(sub_parts):
            if sub_part == "#":
                return True
            if i >= len(topic_parts):
                return False
            if sub_part == "+":
                continue
            if sub_part != topic_parts[i]:
                return False

        return len(sub_parts) == len(topic_parts)

    async def publish(self, topic: str, payload: str, qos: int = 0,
                      retain: bool = False) -> None:
        if not self.is_connected or not self._client:
            logger.debug("MQTT 未连接，跳过发布 %s", topic)
            return
        try:
            self._client.publish(topic, payload, qos=qos, retain=retain)
        except Exception as exc:  # noqa: BLE001
            logger.error("发布到 %s 失败: %s", topic, exc)

    async def disconnect(self) -> None:
        if not self._client:
            return
        try:
            await self.publish(self.state_topic, "offline", retain=True)
            await _maybe_await(self._client.disconnect())
        except Exception:  # noqa: BLE001
            pass
        self.connected.clear()
