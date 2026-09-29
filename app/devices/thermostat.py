"""空调 / 温控器（Control4 proxy: thermostatV2）。

实测结论（OS 2.10.2 + **** 空调温控驱动）：
  变量：
      TEMPERATURE_C / TEMPERATURE_F      当前温度
      HEAT_SETPOINT_C / COOL_SETPOINT_C  制热 / 制冷设定温度
      HVAC_MODE                          'Off' / 'On' / 'Heat' / 'Cool' / 'Dehumidify' / '通风' / 'Auto'
      HVAC_STATE                         运行状态
      FAN_MODE                           'High' / 'Medium' / 'Low' / 'Auto'
      HVAC_MODES_LIST / FAN_MODES_LIST   可用模式列表（字符串，逗号分隔）
      SCALE                              'CELSIUS' / 'FAHRENHEIT'
  命令：
      SET_MODE_HVAC       {"MODE": <模式名>}
      SET_MODE_FAN        {"MODE": <风速名>}
      SET_SETPOINT_HEAT   {"CELSIUS": 26} / {"FAHRENHEIT": 79}
      SET_SETPOINT_COOL   {"CELSIUS": 26} / {"FAHRENHEIT": 79}

注意：模式列表里含中文项（如「通风」），因此模式映射在运行时依据
      HVAC_MODES_LIST 动态生成，而不是写死。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseDevice

logger = logging.getLogger(__name__)

# Control4 模式 -> HA 模式（HA 仅支持 off/heat/cool/auto/dry/fan_only）
C4_TO_HA_MODE = {
    "off": "off",
    "heat": "heat",
    "heating": "heat",
    "emergency_heat": "heat",
    "cool": "cool",
    "cooling": "cool",
    "auto": "auto",
    "heat_cool": "auto",
    "automatic": "auto",
    "dehumidify": "dry",
    "dry": "dry",
    "dehum": "dry",
    "fan": "fan_only",
    "fan_only": "fan_only",
    "on": "fan_only",
    "通风": "fan_only",
}

# HA 模式 -> 优先选择的 Control4 模式名（按顺序匹配设备实际列表）
HA_TO_C4_PREFERRED = {
    "off": ["Off", "OFF", "off"],
    "heat": ["Heat", "HEAT", "heat"],
    "cool": ["Cool", "COOL", "cool"],
    "auto": ["Auto", "AUTO", "auto"],
    "dry": ["Dehumidify", "Dry", "DEHUMIDIFY"],
    "fan_only": ["通风", "Fan", "On", "Fan Only"],
}

DEFAULT_HA_MODES = ["off", "heat", "cool", "auto"]
DEFAULT_FAN_MODES = ["auto", "low", "medium", "high"]


class ThermostatDevice(BaseDevice):
    """Control4 空调，映射为 HA climate 实体。"""

    ha_component = "climate"

    STATE_VARS = (
        "TEMPERATURE_C",
        "TEMPERATURE_F",
        "HEAT_SETPOINT_C",
        "HEAT_SETPOINT_F",
        "COOL_SETPOINT_C",
        "COOL_SETPOINT_F",
        "HVAC_MODE",
        "HVAC_STATE",
        "FAN_MODE",
        "SCALE",
        "HVAC_MODES_LIST",
        "FAN_MODES_LIST",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_celsius = True

        self._ha_modes: List[str] = list(DEFAULT_HA_MODES)
        self._ha_to_c4: Dict[str, str] = {
            "off": "Off", "heat": "Heat", "cool": "Cool", "auto": "Auto",
        }
        self._ha_fan_modes: List[str] = list(DEFAULT_FAN_MODES)
        self._ha_to_c4_fan: Dict[str, str] = {
            "auto": "Auto", "low": "Low", "medium": "Medium", "high": "High",
        }

        # 记录已发布的配置，变化时重发 discovery
        self._published_unit: Optional[str] = None
        self._published_modes: Optional[Tuple[str, ...]] = None
        self._published_fan: Optional[Tuple[str, ...]] = None

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    @property
    def temperature_command_topic(self) -> str:
        return f"{self.base_topic}/temperature/set"

    @property
    def mode_command_topic(self) -> str:
        return f"{self.base_topic}/mode/set"

    @property
    def fan_mode_command_topic(self) -> str:
        return f"{self.base_topic}/fan_mode/set"

    def command_subscriptions(self):
        return [
            (self.mode_command_topic, self.handle_mode_command),
            (self.temperature_command_topic, self.handle_temperature_command),
            (self.fan_mode_command_topic, self.handle_fan_mode_command),
        ]

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def get_discovery_config(self) -> Dict[str, Any]:
        unit = "C" if self.use_celsius else "F"
        return {
            "temperature_unit": unit,
            "min_temp": 4 if self.use_celsius else 40,
            "max_temp": 32 if self.use_celsius else 90,
            "temp_step": 1,
            "modes": list(self._ha_modes),
            "fan_modes": list(self._ha_fan_modes),
            "action_topic": self.state_topic,
            "action_template": "{{ value_json.action }}",
            "current_temperature_topic": self.state_topic,
            "current_temperature_template": "{{ value_json.current_temperature }}",
            "mode_command_topic": self.mode_command_topic,
            "mode_state_topic": self.state_topic,
            "mode_state_template": "{{ value_json.mode }}",
            "temperature_command_topic": self.temperature_command_topic,
            "temperature_state_topic": self.state_topic,
            "temperature_state_template": "{{ value_json.temperature }}",
            "fan_mode_command_topic": self.fan_mode_command_topic,
            "fan_mode_state_topic": self.state_topic,
            "fan_mode_state_template": "{{ value_json.fan_mode }}",
        }

    # ------------------------------------------------------------------
    # 模式表构建
    # ------------------------------------------------------------------
    @staticmethod
    def _split_list(raw: Any) -> List[str]:
        if not raw:
            return []
        return [x.strip() for x in str(raw).split(",") if x.strip()]

    def _rebuild_modes(self, c4_modes: List[str]) -> bool:
        """依据设备实际模式列表生成 HA 模式表，返回是否发生变化。"""
        if not c4_modes:
            return False

        ha_modes: List[str] = []
        reverse: Dict[str, str] = {}
        for mode in c4_modes:
            ha_mode = C4_TO_HA_MODE.get(mode.strip().lower())
            if not ha_mode:
                logger.debug("%s: 未识别的模式 %r，忽略", self.full_name, mode)
                continue
            if ha_mode not in ha_modes:
                ha_modes.append(ha_mode)
            reverse.setdefault(ha_mode, mode)

        if not ha_modes:
            return False

        # 按偏好修正反向映射
        for ha_mode, prefs in HA_TO_C4_PREFERRED.items():
            if ha_mode not in reverse:
                continue
            for pref in prefs:
                if pref in c4_modes:
                    reverse[ha_mode] = pref
                    break

        changed = ha_modes != self._ha_modes or reverse != self._ha_to_c4
        self._ha_modes = ha_modes
        self._ha_to_c4 = reverse
        return changed

    def _rebuild_fan_modes(self, c4_fan_modes: List[str]) -> bool:
        if not c4_fan_modes:
            return False

        ha_modes: List[str] = []
        reverse: Dict[str, str] = {}
        for mode in c4_fan_modes:
            key = mode.strip().lower()
            if not key:
                continue
            if key not in ha_modes:
                ha_modes.append(key)
            reverse.setdefault(key, mode.strip())

        if not ha_modes:
            return False

        changed = ha_modes != self._ha_fan_modes or reverse != self._ha_to_c4_fan
        self._ha_fan_modes = ha_modes
        self._ha_to_c4_fan = reverse
        return changed

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------
    def update_from_vars(self, values: Dict[str, Any]) -> None:
        need_republish = False

        # 单位
        scale = values.get("SCALE")
        if scale:
            use_c = str(scale).strip().upper().startswith("CEL")
            if use_c != self.use_celsius:
                self.use_celsius = use_c
                need_republish = True

        # 模式列表 / 风速列表
        c4_modes = self._split_list(values.get("HVAC_MODES_LIST"))
        if c4_modes and self._rebuild_modes(c4_modes):
            need_republish = True
        c4_fan = self._split_list(values.get("FAN_MODES_LIST"))
        if c4_fan and self._rebuild_fan_modes(c4_fan):
            need_republish = True

        suffix = "C" if self.use_celsius else "F"

        current = self._num(values.get(f"TEMPERATURE_{suffix}"))
        heat = self._num(values.get(f"HEAT_SETPOINT_{suffix}"))
        cool = self._num(values.get(f"COOL_SETPOINT_{suffix}"))

        mode_raw = values.get("HVAC_MODE")
        ha_mode = "off"
        if mode_raw is not None:
            ha_mode = C4_TO_HA_MODE.get(str(mode_raw).strip().lower(), "off")
        self._state["mode"] = ha_mode

        # 目标温度：制热看 heat_setpoint，其余看 cool_setpoint
        target = heat if ha_mode == "heat" else cool
        if target is None:
            target = cool if cool is not None else heat

        if current is not None:
            self._state["current_temperature"] = current
        if target is not None:
            self._state["temperature"] = target

        fan_raw = values.get("FAN_MODE")
        if fan_raw:
            self._state["fan_mode"] = str(fan_raw).strip().lower()

        hvac_state = values.get("HVAC_STATE")
        if hvac_state is not None:
            self._state["action"] = self._to_action(hvac_state)

        if need_republish:
            # 置空已发布记录，bridge 会在本轮结束后重发 discovery
            self._published_unit = None
            self._published_modes = None
            self._published_fan = None

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_action(hvac_state: Any) -> str:
        text = str(hvac_state).strip().lower()
        if "cool" in text:
            return "cooling"
        if "heat" in text:
            return "heating"
        if "fan" in text or "通风" in text:
            return "fan"
        if "dry" in text or "dehumid" in text:
            return "drying"
        return "idle"

    async def publish_discovery(self, extra=None) -> None:
        """发布前先确认模式表是否需要更新（首轮状态读完后才会知道真实模式列表）。"""
        self._published_unit = "C" if self.use_celsius else "F"
        self._published_modes = tuple(self._ha_modes)
        self._published_fan = tuple(self._ha_fan_modes)
        await super().publish_discovery(extra)

    def _discovery_stale(self) -> bool:
        return (
            self._published_unit != ("C" if self.use_celsius else "F")
            or self._published_modes != tuple(self._ha_modes)
            or self._published_fan != tuple(self._ha_fan_modes)
        )

    # ------------------------------------------------------------------
    # 命令处理
    # ------------------------------------------------------------------
    async def handle_mode_command(self, payload: str) -> None:
        ha_mode = (payload or "").strip().lower()
        c4_mode = self._ha_to_c4.get(ha_mode)
        if not c4_mode:
            logger.warning(f"{self.full_name}: 不支持的模式 {payload!r}")
            return
        await self.c4_command("SET_MODE_HVAC", {"MODE": c4_mode})
        self._state["mode"] = ha_mode
        await self.publish_state()
        logger.info(f"{self.full_name} 模式 -> {c4_mode}")

    async def handle_temperature_command(self, payload: str) -> None:
        temp = self._num(payload)
        if temp is None:
            logger.warning(f"{self.full_name}: 无效温度 {payload!r}")
            return

        mode = self._state.get("mode", "cool")
        command = "SET_SETPOINT_HEAT" if mode == "heat" else "SET_SETPOINT_COOL"
        param = "CELSIUS" if self.use_celsius else "FAHRENHEIT"
        await self.c4_command(command, {param: temp})
        self._state["temperature"] = temp
        await self.publish_state()
        logger.info(f"{self.full_name} 设定温度 -> {temp}°{param[0]}")

    async def handle_fan_mode_command(self, payload: str) -> None:
        ha_fan = (payload or "").strip().lower()
        c4_fan = self._ha_to_c4_fan.get(ha_fan)
        if not c4_fan:
            logger.warning(f"{self.full_name}: 不支持的风速 {payload!r}")
            return
        await self.c4_command("SET_MODE_FAN", {"MODE": c4_fan})
        self._state["fan_mode"] = ha_fan
        await self.publish_state()
        logger.info(f"{self.full_name} 风速 -> {c4_fan}")

    async def handle_command(self, payload: str) -> None:
        """兜底：统一命令主题（当前 HA discovery 未使用）。"""
        await self.handle_mode_command(payload)
