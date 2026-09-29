"""
配置加载。支持 YAML 文件，环境变量作为兜底。
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

# 默认的 Control4 proxy -> HA 组件映射（实测 OS 2.10.2 得到）
DEFAULT_PROXY_MAP = {
    "light_v2": "light",
    "blind": "cover",
    "thermostatV2": "climate",
    "relaysingle_relay_c4": "switch",
}


@dataclass
class C4Config:
    ip: str = ""
    username: str = ""
    password: str = ""
    director_token: Optional[str] = None
    verify_director_token: bool = True


@dataclass
class MQTTConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: str = "c4-ha"
    discovery_prefix: str = "homeassistant"


@dataclass
class DeviceFilterConfig:
    """设备筛选配置。"""

    #: Control4 proxy 名称 -> HA 组件名（light/cover/climate/switch）
    proxies: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROXY_MAP))

    #: 整类禁用的组件，例如 ["switch"] 可关闭全部继电器
    disabled_components: List[str] = field(default_factory=list)

    #: 只桥接这些 item id（留空=全部）
    include_ids: List[int] = field(default_factory=list)

    #: 排除这些 item id
    exclude_ids: List[int] = field(default_factory=list)


@dataclass
class PollingConfig:
    enabled: bool = True
    #: 状态轮询间隔（秒）。设备较多时不要设太小。
    interval: int = 30
    #: 是否使用批量变量接口（一次请求刷新全部设备状态）
    use_batch: bool = True
    #: 批量不可用、退化为逐设备读取时的并发数
    concurrency: int = 8


@dataclass
class AppConfig:
    c4: C4Config
    mqtt: MQTTConfig
    devices: DeviceFilterConfig
    polling: PollingConfig
    log_level: str = "INFO"


def _as_int_list(value) -> List[int]:
    out: List[int] = []
    for item in value or []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    c4_data = data.get("control4", {}) or {}
    mqtt_data = data.get("mqtt", {}) or {}
    devices_data = data.get("devices", {}) or {}
    polling_data = data.get("polling", {}) or {}

    proxies = devices_data.get("proxies")
    if not isinstance(proxies, dict) or not proxies:
        proxies = dict(DEFAULT_PROXY_MAP)

    disabled = devices_data.get("disabled_components") or []
    if not isinstance(disabled, list):
        disabled = []

    return AppConfig(
        c4=C4Config(
            ip=c4_data.get("ip") or os.getenv("C4_IP", ""),
            username=c4_data.get("username") or os.getenv("C4_USERNAME", ""),
            password=c4_data.get("password") or os.getenv("C4_PASSWORD", ""),
            director_token=c4_data.get("director_token") or os.getenv("C4_DIRECTOR_TOKEN"),
            verify_director_token=bool(c4_data.get("verify_director_token", True)),
        ),
        mqtt=MQTTConfig(
            host=mqtt_data.get("host") or os.getenv("MQTT_HOST", "127.0.0.1"),
            port=int(mqtt_data.get("port") or os.getenv("MQTT_PORT", "1883")),
            username=mqtt_data.get("username") or os.getenv("MQTT_USERNAME"),
            password=mqtt_data.get("password") or os.getenv("MQTT_PASSWORD"),
            topic_prefix=mqtt_data.get("topic_prefix", "c4-ha"),
            discovery_prefix=mqtt_data.get("discovery_prefix", "homeassistant"),
        ),
        devices=DeviceFilterConfig(
            proxies={str(k): str(v) for k, v in proxies.items()},
            disabled_components=[str(x).lower() for x in disabled],
            include_ids=_as_int_list(devices_data.get("include_ids")),
            exclude_ids=_as_int_list(devices_data.get("exclude_ids")),
        ),
        polling=PollingConfig(
            enabled=bool(polling_data.get("enabled", True)),
            interval=max(5, int(polling_data.get("interval", 30))),
            use_batch=bool(polling_data.get("use_batch", True)),
            concurrency=max(1, int(polling_data.get("concurrency", 8))),
        ),
        log_level=data.get("log_level", "INFO"),
    )
