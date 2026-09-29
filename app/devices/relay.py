"""继电器 / 干接点（Control4 proxy: relaysingle_relay_c4）。

实测结论（OS 2.10.2）：
  状态变量：RelayState（0/1）、StateVerified
  可用命令：CLOSE（闭合=通电）、OPEN（断开=断电）、TOGGLE

命名容易混淆，这里统一为：
  HA ON  -> Control4 CLOSE
  HA OFF -> Control4 OPEN
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from .base import BaseDevice

logger = logging.getLogger(__name__)


class RelayDevice(BaseDevice):
    """Control4 继电器，映射为 HA switch 实体。"""

    ha_component = "switch"

    STATE_VARS = ("RelayState", "StateVerified")

    def get_discovery_config(self) -> Dict[str, Any]:
        return {
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "state_value_template": "{{ value_json.state }}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "optimistic": False,
        }

    def update_from_vars(self, values: Dict[str, Any]) -> None:
        raw = values.get("RelayState")
        if raw is None:
            raw = values.get("StateVerified")
        if raw is not None:
            self._state["state"] = "ON" if self.truthy(raw) else "OFF"

    async def handle_command(self, payload: str) -> None:
        cmd = (payload or "").strip().upper()
        if cmd == "ON":
            c4_cmd = "CLOSE"
        elif cmd == "OFF":
            c4_cmd = "OPEN"
        else:
            logger.warning(f"{self.full_name}: 无法识别的继电器指令 {payload!r}")
            return

        await self.c4_command(c4_cmd)
        self._state["state"] = cmd
        await self.publish_state()
        logger.info(f"{self.full_name} -> {cmd} (Control4 {c4_cmd})")
