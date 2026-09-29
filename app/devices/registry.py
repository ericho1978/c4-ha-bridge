"""Control4 proxy 名称 -> 设备类 的映射表。

依据实测（OS 2.10.2）确定：设备的 `proxy` 字段才是准确的设备类型标识，
而 `type` 只是粗粒度的节点类型（6/7 都是 "device"，7 才是可控制的功能代理）。

未列入 PROXY_REGISTRY 的 proxy 会被跳过并计入日志，可通过配置 devices.proxies 扩展。
"""
from __future__ import annotations

from typing import Dict, Optional, Type

from .base import BaseDevice
from .blind import BlindDevice
from .light import LightDevice
from .relay import RelayDevice
from .thermostat import ThermostatDevice

# proxy -> 设备类
PROXY_REGISTRY: Dict[str, Type[BaseDevice]] = {
    "light_v2": LightDevice,
    "blind": BlindDevice,
    "blind_ir": BlindDevice,
    "thermostatV2": ThermostatDevice,
    "thermostat": ThermostatDevice,
    "relaysingle_relay_c4": RelayDevice,
    "relaysingle_relay": RelayDevice,
}

# HA 组件名 -> 设备类（用于配置里用 light/cover/climate/switch 简化书写）
COMPONENT_REGISTRY: Dict[str, Type[BaseDevice]] = {
    "light": LightDevice,
    "cover": BlindDevice,
    "climate": ThermostatDevice,
    "switch": RelayDevice,
}


def resolve_device_class(proxy: str, overrides: Optional[Dict[str, str]] = None) -> Optional[Type[BaseDevice]]:
    """按 proxy 找设备类，支持配置覆盖（proxy -> 组件名 或 类名）。"""
    if overrides:
        target = overrides.get(proxy)
        if target:
            cls = COMPONENT_REGISTRY.get(str(target).lower())
            if cls:
                return cls
            for candidate in (
                LightDevice, BlindDevice, ThermostatDevice, RelayDevice
            ):
                if candidate.__name__ == target:
                    return candidate
    return PROXY_REGISTRY.get(proxy)
