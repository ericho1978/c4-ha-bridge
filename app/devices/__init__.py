"""Control4 设备桥接类。"""
from .base import BaseDevice
from .blind import BlindDevice
from .light import LightDevice
from .registry import (
    COMPONENT_REGISTRY,
    PROXY_REGISTRY,
    resolve_device_class,
)
from .relay import RelayDevice
from .thermostat import ThermostatDevice

__all__ = [
    "BaseDevice",
    "BlindDevice",
    "LightDevice",
    "RelayDevice",
    "ThermostatDevice",
    "PROXY_REGISTRY",
    "COMPONENT_REGISTRY",
    "resolve_device_class",
]
