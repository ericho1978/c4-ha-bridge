"""设备基类：负责 MQTT Discovery、状态发布与命令路由。

设计要点（基于实测的 Control4 OS 2.10.2 + 第三方驱动数据结构）：
  - 设备通过 `proxy` 字段识别类型（light_v2 / blind / thermostatV2 / ...）
  - 状态统一走「批量变量读取」：bridge 一次请求取回全系统变量，再分发到各设备
  - 每个设备声明自己需要的变量名（STATE_VARS），便于批量请求聚合
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BaseDevice(ABC):
    """所有桥接设备的基类。"""

    #: Home Assistant 组件类型，子类覆盖
    ha_component: str = "switch"

    #: 需要从 Control4 读取的变量名（用于批量聚合）
    STATE_VARS: Tuple[str, ...] = ()

    def __init__(
        self,
        item_id: int,
        name: str,
        c4,
        mqtt,
        topic_prefix: str,
        discovery_prefix: str,
        room: str = "",
        proxy: str = "",
        raw: Optional[Dict[str, Any]] = None,
    ):
        self.item_id = int(item_id)
        self.name = name
        self.room = room
        self.proxy = proxy
        self.raw = raw or {}
        self.c4 = c4
        self.mqtt = mqtt
        self.topic_prefix = topic_prefix
        self.discovery_prefix = discovery_prefix

        self.unique_id = f"c4_{self.item_id}"
        self.base_topic = f"{topic_prefix}/{self.ha_component}/{self.unique_id}"
        self.state_topic = f"{self.base_topic}/state"
        self.command_topic = f"{self.base_topic}/set"
        self.availability_topic = f"{topic_prefix}/bridge/state"

        self._state: Dict[str, Any] = {}
        self._online = True

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def full_name(self) -> str:
        """带房间前缀的显示名。"""
        return f"{self.room} {self.name}" if self.room else self.name

    @property
    def device_name(self) -> str:
        return f"{self.name} (Control4)"

    @staticmethod
    def truthy(value: Any) -> bool:
        """把 Control4 返回的 0/1/True/False/"Off"/"On" 统一判真。"""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().lower()
        if text in ("", "0", "false", "off", "no", "closed", "undefined"):
            return False
        if text in ("1", "true", "on", "yes", "open"):
            return True
        try:
            return float(text) != 0
        except ValueError:
            return bool(text)

    @staticmethod
    def as_int(value: Any) -> Optional[int]:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 子类需要实现
    # ------------------------------------------------------------------
    @abstractmethod
    def get_discovery_config(self) -> Dict[str, Any]:
        """返回 HA MQTT Discovery 配置（不含公共字段）。"""
        raise NotImplementedError

    @abstractmethod
    def update_from_vars(self, values: Dict[str, Any]) -> None:
        """用批量读到的变量值更新内部状态缓存。"""
        raise NotImplementedError

    def command_subscriptions(self) -> List[Tuple[str, Callable]]:
        """返回 [(订阅主题, 处理函数)]，默认只订阅统一命令主题。"""
        return [(self.command_topic, self.handle_command)]

    async def handle_command(self, payload: str) -> None:
        """处理来自 HA 的命令，子类按需覆盖。"""
        logger.warning(f"{self.full_name}: 未实现命令处理，忽略 {payload!r}")

    # ------------------------------------------------------------------
    # 状态读取（逐设备兜底路径）
    # ------------------------------------------------------------------
    async def fetch_state(self) -> None:
        """逐设备读取自身变量（批量读取失败时的兜底）。"""
        if not self.STATE_VARS:
            return
        values = await self.c4.get_item_variables_map(self.item_id)
        if values:
            self.update_from_vars(values)

    # ------------------------------------------------------------------
    # MQTT 发布
    # ------------------------------------------------------------------
    async def publish_state(self) -> None:
        if not self._state:
            return
        await self.mqtt.publish(self.state_topic, json.dumps(self._state), retain=True)

    async def publish_discovery(self, extra: Optional[Dict[str, Any]] = None) -> None:
        config = self.get_discovery_config()
        if extra:
            config.update(extra)

        config.setdefault("name", self.full_name)
        config.setdefault("unique_id", self.unique_id)
        config.setdefault("state_topic", self.state_topic)
        config.setdefault("availability_topic", self.availability_topic)
        config.setdefault("payload_available", "online")
        config.setdefault("payload_not_available", "offline")
        config.setdefault("device", self._device_block())

        topic = f"{self.discovery_prefix}/{self.ha_component}/{self.unique_id}/config"
        await self.mqtt.publish(topic, json.dumps(config), retain=True)
        logger.debug(f"已发布 Discovery: {self.full_name} ({self.ha_component})")

    def _device_block(self) -> Dict[str, Any]:
        model = self.raw.get("model") or self.raw.get("protocolName") or self.proxy
        info: Dict[str, Any] = {
            "identifiers": [f"control4_{self.item_id}"],
            "name": self.device_name,
            "manufacturer": self.raw.get("manufacturer") or "Control4",
            "model": model or "Control4 Device",
            "via_device": "c4-ha-bridge",
        }
        if self.room:
            info["suggested_area"] = self.room
        return info

    async def unpublish_discovery(self) -> None:
        topic = f"{self.discovery_prefix}/{self.ha_component}/{self.unique_id}/config"
        await self.mqtt.publish(topic, "", retain=True)

    # ------------------------------------------------------------------
    # 命令下发辅助
    # ------------------------------------------------------------------
    async def c4_command(self, command: str, params: Optional[Dict[str, Any]] = None) -> None:
        """向本设备下发 Control4 命令。"""
        await self.c4.send_command(self.item_id, command, params or {})

    # ------------------------------------------------------------------
    # 调试
    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover
        return "<{} {} id={} proxy={}>".format(
            type(self).__name__, self.full_name, self.item_id, self.proxy
        )
