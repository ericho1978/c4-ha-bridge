#!/usr/bin/env python3
"""
C4-HA Bridge —— Control4 到 Home Assistant 的 MQTT 桥接

支持设备：灯光（light_v2）、窗帘（blind）、空调（thermostatV2）、继电器（relaysingle_relay_c4）
适用 Control4 系统：OS 2.10.1+（实测 2.10.2 / EA-5）

直接通过 aiohttp 调用 Control4 的云端认证与本地 Director REST 接口，
不依赖 pyControl4，避免其版本与 Python 版本限制。
"""
import asyncio
import logging
import os
import signal
import sys

from bridge import C4HABridge
from config import load_config
from mqtt_client import MQTTClient


def setup_logging(level: str = "INFO") -> None:
    """配置日志。"""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def find_config() -> str:
    """按优先级查找配置文件。"""
    candidates = []
    env_path = os.getenv("CONFIG_PATH")
    if env_path:
        candidates.append(env_path)
    candidates += ["/app/config/config.yaml", "config/config.yaml", "config.yaml"]

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


async def main() -> None:
    config_path = find_config()
    if not config_path:
        print("ERROR: 未找到配置文件，尝试过的路径：")
        print("  $CONFIG_PATH, /app/config/config.yaml, config/config.yaml, config.yaml")
        sys.exit(1)

    config = load_config(config_path)
    setup_logging(config.log_level)

    logger = logging.getLogger("main")
    logger.info("配置文件: %s", config_path)

    mqtt = MQTTClient(
        host=config.mqtt.host,
        port=config.mqtt.port,
        username=config.mqtt.username,
        password=config.mqtt.password,
        client_id="c4-ha-bridge",
        topic_prefix=config.mqtt.topic_prefix,
    )

    bridge = C4HABridge(config, mqtt)

    await mqtt.connect()

    try:
        await bridge.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("桥接启动失败: %s", exc, exc_info=True)
        await mqtt.disconnect()
        sys.exit(1)

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("收到退出信号")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows 不支持

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("正在退出 ...")
    await bridge.stop()
    await mqtt.disconnect()
    logger.info("已退出")


if __name__ == "__main__":
    asyncio.run(main())
