"""
C4-HA Bridge 主逻辑。

流程：
  1. 账户/token 连接 Control4 Director
  2. 拉取 /api/v1/items，按 `proxy` 字段识别设备类型（只取 type=7 的功能代理）
  3. 发布 HA MQTT Discovery
  4. 批量读取变量刷新状态（一次请求刷新全部设备）
  5. 订阅 MQTT 命令主题并转发到 Control4
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from c4_compat import C4AccountCompat, C4DirectorCompat
from config import AppConfig
from devices import BaseDevice, resolve_device_class

logger = logging.getLogger(__name__)

# Control4 item type：7 = 可控制的功能代理（proxy），6 = 硬件设备父节点
ITEM_TYPE_PROXY = 7


class C4HABridge:
    def __init__(self, config: AppConfig, mqtt_client):
        self.config = config
        self.mqtt = mqtt_client
        self.director: Optional[C4DirectorCompat] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.devices: Dict[int, BaseDevice] = {}
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False
        #: 记录批量读取是否可用，便于日志与排障
        self._batch_ok: Optional[bool] = None

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def start(self) -> None:
        logger.info("C4-HA Bridge 启动中 ...")
        self._running = True

        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=30),
        )

        await self._connect_c4()
        await self.mqtt.connected.wait()
        logger.info("MQTT 已连接，开始发现设备 ...")

        await self._discover_devices()
        if not self.devices:
            logger.error("未发现任何可桥接设备，请检查 devices.proxies 配置")

        await self._publish_all_discovery()
        await self._setup_command_handlers()
        await self._refresh_states()

        if self.config.polling.enabled:
            self._polling_task = asyncio.create_task(self._polling_loop())

        logger.info("C4-HA Bridge 启动完成，共 %d 个设备", len(self.devices))

    async def stop(self) -> None:
        logger.info("C4-HA Bridge 停止中 ...")
        self._running = False

        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

        if self.session:
            await self.session.close()

        await self.mqtt.publish(
            f"{self.config.mqtt.topic_prefix}/bridge/state", "offline", retain=True
        )
        logger.info("C4-HA Bridge 已停止")

    # ==================================================================
    # Control4 连接
    # ==================================================================
    async def _connect_c4(self) -> None:
        c4_config = self.config.c4

        if c4_config.director_token:
            self.director = C4DirectorCompat(
                ip=c4_config.ip, token=c4_config.director_token, session=self.session
            )
            if c4_config.verify_director_token:
                try:
                    items = await self.director.get_all_item_info()
                    logger.info(
                        "已连接 C4 Director %s（使用配置中的 token，%d 个条目）",
                        c4_config.ip, len(items),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "配置中的 director_token 无效（%s），改为账户认证", exc
                    )
                    self.director = None
            else:
                logger.info("已连接 C4 Director %s（使用配置中的 token）", c4_config.ip)
                return

        account = C4AccountCompat(
            username=c4_config.username, password=c4_config.password, session=None
        )
        logger.info(
            "正在认证 Control4 账户 %s（%s）", c4_config.username, account.api_flavor
        )
        await account.authenticate()

        controllers = await account.get_controllers()
        if not controllers:
            raise RuntimeError("账户下未找到任何 Control4 控制器")

        for ctrl in controllers:
            logger.info(
                "  控制器: %s (common=%s, model=%s)",
                ctrl["name"], ctrl["common_name"], ctrl["model"] or "unknown",
            )

        selected = self._pick_controller(controllers)
        token = await account.get_director_token(selected)
        if not token:
            raise RuntimeError("获取 Director Token 失败")

        self.director = C4DirectorCompat(
            ip=c4_config.ip, token=token, session=self.session
        )
        items = await self.director.get_all_item_info()
        logger.info("已连接 C4 Director %s（账户认证，%d 个条目）",
                    c4_config.ip, len(items))

    def _pick_controller(self, controllers: List[Dict[str, Any]]) -> Dict[str, Any]:
        """优先选择 IP 与配置一致的控制器。"""
        for ctrl in controllers:
            raw = ctrl.get("raw") or {}
            for key in ("networkAddress", "ip", "ipAddress", "localIP"):
                if raw.get(key) == self.config.c4.ip:
                    return ctrl
        return controllers[0]

    # ==================================================================
    # 设备发现
    # ==================================================================
    async def _discover_devices(self) -> None:
        assert self.director is not None
        cfg = self.config.devices

        all_items = await self.director.get_all_item_info()
        logger.info("C4 共返回 %d 个条目", len(all_items))

        proxies = [it for it in all_items if self._item_type(it) == ITEM_TYPE_PROXY]
        logger.info("其中 type=%d 的功能代理设备 %d 个", ITEM_TYPE_PROXY, len(proxies))

        skipped: Dict[str, int] = {}
        excluded = 0
        disabled = 0

        for item in proxies:
            item_id = self._item_id(item)
            if item_id is None:
                continue

            proxy = str(item.get("proxy") or "")
            device_cls = resolve_device_class(proxy, cfg.proxies)

            if device_cls is None:
                skipped[proxy or "(空)"] = skipped.get(proxy or "(空)", 0) + 1
                continue

            if device_cls.ha_component in cfg.disabled_components:
                disabled += 1
                continue

            if cfg.include_ids and item_id not in cfg.include_ids:
                continue
            if item_id in cfg.exclude_ids:
                excluded += 1
                continue

            device = device_cls(
                item_id=item_id,
                name=str(item.get("name") or f"Device_{item_id}"),
                c4=self.director,
                mqtt=self.mqtt,
                topic_prefix=self.config.mqtt.topic_prefix,
                discovery_prefix=self.config.mqtt.discovery_prefix,
                room=str(item.get("roomName") or ""),
                proxy=proxy,
                raw=item,
            )
            self.devices[item_id] = device

        # 统计
        by_component: Dict[str, int] = {}
        for dev in self.devices.values():
            by_component[dev.ha_component] = by_component.get(dev.ha_component, 0) + 1
        logger.info("已桥接设备统计: %s", by_component)
        if excluded:
            logger.info("按配置排除 %d 个设备", excluded)
        if disabled:
            logger.info("按配置禁用组件，跳过 %d 个设备", disabled)
        if skipped:
            top = sorted(skipped.items(), key=lambda x: -x[1])[:10]
            logger.info(
                "未支持的 proxy 类型（已跳过）: %s",
                ", ".join(f"{p}×{c}" for p, c in top),
            )

    @staticmethod
    def _item_id(item: Dict[str, Any]) -> Optional[int]:
        try:
            return int(item.get("id"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _item_type(item: Dict[str, Any]) -> int:
        try:
            return int(item.get("type", 0))
        except (TypeError, ValueError):
            return 0

    # ==================================================================
    # Discovery / 命令订阅
    # ==================================================================
    async def _publish_all_discovery(self) -> None:
        for dev in self.devices.values():
            try:
                await dev.publish_discovery()
            except Exception as exc:  # noqa: BLE001
                logger.error("发布 %s 的 Discovery 失败: %s", dev.full_name, exc)

    async def _setup_command_handlers(self) -> None:
        for dev in self.devices.values():
            for topic, handler in dev.command_subscriptions():
                await self.mqtt.subscribe(
                    topic,
                    self._make_handler(dev, handler),
                )

    @staticmethod
    def _make_handler(device: BaseDevice, handler):
        async def _cb(topic: str, payload: str) -> None:
            try:
                await handler(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "处理 %s 的命令失败 (%s=%r): %s",
                    device.full_name, topic, payload, exc,
                )

        return _cb

    # ==================================================================
    # 状态刷新
    # ==================================================================
    async def _refresh_states(self) -> None:
        if not self.devices:
            return

        data, mode = await self._read_states()

        updated = 0
        for dev in self.devices.values():
            values = data.get(dev.item_id)
            if not values:
                continue
            dev.update_from_vars(values)
            await dev.publish_state()
            updated += 1

        # 设备类型/模式表发生变化时补发 discovery（如灯光升级为调光、空调模式表更新）
        republished = 0
        for dev in self.devices.values():
            checker = getattr(dev, "_discovery_stale", None)
            if checker and checker():
                try:
                    await dev.publish_discovery()
                    republished += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error("重发 %s 的 Discovery 失败: %s", dev.full_name, exc)
        if republished:
            logger.info(
                "本轮补发 Discovery %d 个（设备类型/能力发生变化）", republished
            )

        logger.debug("状态刷新完成: %d/%d 个设备（方式=%s）",
                     updated, len(self.devices), mode)

    async def _read_states(self) -> Tuple[Dict[int, Dict[str, Any]], str]:
        """读取所有设备状态，依次尝试：一次批量 -> 逐变量批量 -> 逐设备。"""
        assert self.director is not None

        names: List[str] = sorted(
            {n for dev in self.devices.values() for n in dev.STATE_VARS}
        )
        if not names:
            return {}, "none"

        if self.config.polling.use_batch and self._batch_ok is not False:
            try:
                data = await self.director.get_all_items_variables(names)
                if data:
                    if self._batch_ok is None:
                        self._batch_ok = True
                        logger.info("批量变量读取可用（一次请求刷新 %d 个变量名）",
                                    len(names))
                    return data, "batch"
                logger.debug("批量变量读取返回空结果")
            except Exception as exc:  # noqa: BLE001
                logger.warning("批量变量读取失败，尝试逐个变量: %s", exc)

            # 逐个变量批量
            merged: Dict[int, Dict[str, Any]] = {}
            got_any = False
            for name in names:
                try:
                    part = await self.director.get_all_items_variables([name])
                except Exception as exc:  # noqa: BLE001
                    logger.debug("批量读取变量 %s 失败: %s", name, exc)
                    continue
                if part:
                    got_any = True
                for iid, values in part.items():
                    merged.setdefault(iid, {}).update(values)
            if got_any:
                if self._batch_ok is None:
                    self._batch_ok = True
                    logger.info("批量变量读取可用（按变量逐个请求）")
                return merged, "batch-per-var"

            self._batch_ok = False
            logger.warning("批量变量读取不可用，退化为逐设备读取（设备多时较慢）")

        return await self._read_per_device(), "per-device"

    async def _read_per_device(self) -> Dict[int, Dict[str, Any]]:
        assert self.director is not None
        semaphore = asyncio.Semaphore(self.config.polling.concurrency)
        result: Dict[int, Dict[str, Any]] = {}

        async def _one(dev: BaseDevice) -> None:
            async with semaphore:
                try:
                    values = await self.director.get_item_variables_map(dev.item_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("读取 %s 状态失败: %s", dev.full_name, exc)
                    return
                if values:
                    result[dev.item_id] = values

        await asyncio.gather(*[_one(d) for d in self.devices.values()])
        return result

    # ==================================================================
    # 轮询
    # ==================================================================
    async def _polling_loop(self) -> None:
        interval = self.config.polling.interval
        logger.info("开始状态轮询（间隔 %ds）", interval)

        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                await self._refresh_states()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("轮询出错: %s", exc)
                await asyncio.sleep(5)
