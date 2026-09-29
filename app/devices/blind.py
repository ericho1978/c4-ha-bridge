"""窗帘 / 电动窗（Control4 proxy: blind）。

实测结论（OS 2.10.2 + **** 窗帘驱动）：
  状态变量：Open / Fully Open / Fully Closed / Stopped / Closing / Opening / Level / Target Level
  可用命令：
      SET_LEVEL_TARGET:LEVEL_TARGET_OPEN     （开）
      SET_LEVEL_TARGET:LEVEL_TARGET_CLOSED   （关）
      STOP
      TOGGLE

命令参数有两种可能的写法，这里做自动兼容：
  A. command=SET_LEVEL_TARGET + tParams={"LEVEL_TARGET": "LEVEL_TARGET_OPEN"}
  B. command="SET_LEVEL_TARGET:LEVEL_TARGET_OPEN" + 空 tParams
首次下发时按 A 尝试，失败则切到 B 并记住，后续不再重试 A。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import BaseDevice

logger = logging.getLogger(__name__)

TARGET_OPEN = "LEVEL_TARGET_OPEN"
TARGET_CLOSED = "LEVEL_TARGET_CLOSED"


class BlindDevice(BaseDevice):
    """Control4 窗帘，映射为 HA cover 实体（开/关/停）。"""

    ha_component = "cover"

    STATE_VARS = (
        "Level",
        "Open",
        "Fully Open",
        "Fully Closed",
        "Stopped",
        "Closing",
        "Opening",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # None=未确定, "A" / "B"
        self._cmd_form: Optional[str] = None

    # ------------------------------------------------------------------
    def get_discovery_config(self) -> Dict[str, Any]:
        return {
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "state_value_template": "{{ value_json.state }}",
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "device_class": "blind",
            "optimistic": False,
        }

    def update_from_vars(self, values: Dict[str, Any]) -> None:
        position = self._position_from(values)

        # Control4 语义：Stopped=1 时，Closing/Opening 只表示「上次移动方向」，
        # 并不代表当前正在运动。因此必须先判断 Stopped，
        # 仅在未停止时才报 opening/closing，否则按位置判定 open/closed。
        stopped = self.truthy(values.get("Stopped"))
        closing = self.truthy(values.get("Closing"))
        opening = self.truthy(values.get("Opening"))

        if not stopped and closing:
            state = "closing"
        elif not stopped and opening:
            state = "opening"
        else:
            state = "open" if position > 0 else "closed"

        self._state["state"] = state
        self._state["position"] = position

    @staticmethod
    def _position_from(values: Dict[str, Any]) -> int:
        """推断 0-100 的位置。"""
        level = values.get("Level")
        try:
            level_num = int(float(level)) if level is not None else None
        except (TypeError, ValueError):
            level_num = None

        # Level 能给出明确百分比（>1）时优先使用
        if level_num is not None and level_num > 1:
            return max(0, min(100, level_num))

        if BaseDevice.truthy(values.get("Fully Open")):
            return 100
        if BaseDevice.truthy(values.get("Fully Closed")):
            return 0

        # 开着但无法确定具体位置
        if BaseDevice.truthy(values.get("Open")):
            return level_num if level_num == 100 else 50
        return 0

    # ------------------------------------------------------------------
    async def handle_command(self, payload: str) -> None:
        cmd = (payload or "").strip().upper()
        if cmd == "OPEN":
            await self._set_target(TARGET_OPEN)
        elif cmd == "CLOSE":
            await self._set_target(TARGET_CLOSED)
        elif cmd == "STOP":
            await self.c4_command("STOP")
            self._state["state"] = "open" if self._state.get("position", 0) > 0 else "closed"
            await self.publish_state()
            logger.info(f"{self.full_name} -> STOP")
        else:
            logger.warning(f"{self.full_name}: 无法识别的窗帘指令 {payload!r}")
            return

    async def _set_target(self, target: str) -> None:
        """下发目标位置，自动适配两种命令写法。"""
        if self._cmd_form != "B":
            try:
                await self.c4_command("SET_LEVEL_TARGET", {"LEVEL_TARGET": target})
                if self._cmd_form is None:
                    self._cmd_form = "A"
                    logger.info(
                        f"{self.full_name}: 使用命令写法 A"
                        "（SET_LEVEL_TARGET + tParams）"
                    )
                await self._after_target(target)
                return
            except Exception as exc:  # noqa: BLE001
                if self._cmd_form == "A":
                    raise
                logger.warning(f"{self.full_name}: 写法 A 失败({exc})，改用写法 B")

        await self.c4_command(f"SET_LEVEL_TARGET:{target}", {})
        if self._cmd_form != "B":
            self._cmd_form = "B"
            logger.info(f"{self.full_name}: 使用命令写法 B（command 内嵌参数）")
        await self._after_target(target)

    async def _after_target(self, target: str) -> None:
        """乐观反馈，让 HA 立刻显示开合方向，下一轮同步会校正。"""
        self._state["state"] = "opening" if target == TARGET_OPEN else "closing"
        await self.publish_state()
        logger.info(f"{self.full_name} -> {target}")
