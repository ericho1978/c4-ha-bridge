"""灯光设备（Control4 proxy: light_v2）。

实测结论（OS 2.10.2，第三方调光/开关驱动）：

  驱动类型             数量   状态变量                命令
  **** 开关型          N/A    LIGHT_STATE              ON, OFF, TOGGLE
  **** 调光型          N/A    LIGHT_STATE,LIGHT_LEVEL  ON, OFF, TOGGLE, SET_LEVEL, RAMP_TO_LEVEL, RAMP_TO_PRESET
  **** 开关型          N/A    LIGHT_STATE              ON, OFF, TOGGLE

因此**不按 proxy 一刀切**，而是按设备实际暴露的变量自动判定是否可调光：
批量读取时若该设备返回了 LIGHT_LEVEL，即认为可调光，并补发一次
Discovery 以启用亮度（HA 的 light 组件不需要重建实体）。

亮度统一用 0-100（brightness_scale: 100），与 Control4 的 LIGHT_LEVEL 一致，
无需换算。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from .base import BaseDevice

logger = logging.getLogger(__name__)


class LightDevice(BaseDevice):
    """Control4 灯光，映射为 HA light 实体（自动适配开关型 / 调光型）。"""

    ha_component = "light"

    STATE_VARS = ("LIGHT_STATE", "LIGHT_LEVEL")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_dimmer = False
        #: 记录已发布的形态，用于判断是否需要重发 Discovery
        self._published_dimmer = None

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    @property
    def brightness_command_topic(self) -> str:
        return f"{self.base_topic}/brightness/set"

    def command_subscriptions(self) -> List[Tuple[str, Any]]:
        """始终订阅两个主题。

        调光形态是在首轮状态读取后才确定的，而订阅在启动时建立，
        因此这里无条件订阅亮度主题，避免调光设备漏订阅。
        """
        return [
            (self.command_topic, self.handle_command),
            (self.brightness_command_topic, self.handle_brightness_command),
        ]

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def get_discovery_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {
            "command_topic": self.command_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_topic": self.state_topic,
            "state_value_template": "{{ value_json.state }}",
            "optimistic": False,
        }

        if self.is_dimmer:
            config.update(
                {
                    "brightness_state_topic": self.state_topic,
                    "brightness_value_template": "{{ value_json.brightness }}",
                    "brightness_command_topic": self.brightness_command_topic,
                    "brightness_scale": 100,
                    # 仅开灯时优先沿用上一次的亮度；无历史时下发 payload_on
                    "on_command_type": "last",
                }
            )
        return config

    async def publish_discovery(self, extra=None) -> None:
        self._published_dimmer = self.is_dimmer
        await super().publish_discovery(extra)

    def _discovery_stale(self) -> bool:
        return self._published_dimmer is not None and (
            self._published_dimmer != self.is_dimmer
        )

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def update_from_vars(self, values: Dict[str, Any]) -> None:
        if "LIGHT_STATE" in values:
            self._state["state"] = (
                "ON" if self.truthy(values["LIGHT_STATE"]) else "OFF"
            )

        # 出现 LIGHT_LEVEL 即说明该设备可调光
        if "LIGHT_LEVEL" in values:
            if not self.is_dimmer:
                self.is_dimmer = True
                # 逐条用 DEBUG，避免上百个调光灯刷屏；
                # bridge 会在本轮结束后汇总一条 INFO
                logger.debug("%s 检测到 LIGHT_LEVEL，升级为调光设备", self.full_name)
            level = self.as_int(values.get("LIGHT_LEVEL"))
            if level is not None:
                self._state["brightness"] = max(0, min(100, level))

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    async def handle_command(self, payload: str) -> None:
        cmd = (payload or "").strip().upper()
        if cmd not in ("ON", "OFF"):
            logger.warning(f"{self.full_name}: 无法识别的灯光指令 {payload!r}")
            return

        await self.c4_command(cmd)
        self._state["state"] = cmd
        if cmd == "OFF":
            self._state.pop("brightness", None)
        await self.publish_state()
        logger.info(f"{self.full_name} -> {cmd}")

    async def handle_brightness_command(self, payload: str) -> None:
        level = self.as_int(payload)
        if level is None:
            logger.warning(f"{self.full_name}: 无效亮度 {payload!r}")
            return

        level = max(0, min(100, level))
        if level == 0:
            await self.c4_command("OFF")
            self._state["state"] = "OFF"
            self._state.pop("brightness", None)
            logger.info(f"{self.full_name} -> 亮度 0（按关灯处理）")
        else:
            await self.c4_command("SET_LEVEL", {"LEVEL": level})
            self._state["state"] = "ON"
            self._state["brightness"] = level
            logger.info(f"{self.full_name} -> 亮度 {level}%")

        await self.publish_state()
