#!/usr/bin/env python3
"""
C4 API 连通性测试脚本（兼容 pyControl4 1.x / 2.x）

用法：
    python3 test_c4_api.py                      # 自动查找配置文件
    python3 test_c4_api.py -c config/config.yaml
    python3 test_c4_api.py --ip **** --user a@b.com --pass xxx
"""
import argparse
import asyncio
import os
import sys

import yaml

# 让脚本能直接 import app/ 下的模块
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

# 配置文件候选路径（按优先级）
CONFIG_CANDIDATES = [
    "config/config.yaml",
    "config.yaml",
    "/app/config/config.yaml",
]


def find_config(explicit: str = None):
    """按候选顺序查找配置文件。"""
    candidates = [explicit] if explicit else []
    candidates += CONFIG_CANDIDATES
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


async def test_c4_connection(ip: str, username: str, password: str, saved_token: str = ""):
    """测试 Control4 API 连接并输出详细信息。"""
    import aiohttp
    from c4_compat import (
        AUTHENTICATION_ENDPOINT,
        C4AccountCompat,
        C4DirectorCompat,
    )

    print("=" * 62)
    print("  Control4 API 连通性测试")
    print("=" * 62)
    print(f"  控制器 IP : {ip}")
    print(f"  账户      : {username}")
    print("=" * 62)
    print()

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False),
        timeout=aiohttp.ClientTimeout(total=30),
    )

    try:
        # ---- Step 1: 账户认证 ----
        print("[1/6] 正在认证 Control4 账户...")
        account = C4AccountCompat(username, password, session=None)
        print(f"  实现方式: {account.api_flavor}")
        print(f"  认证端点: {AUTHENTICATION_ENDPOINT}")

        try:
            await account.authenticate()
            print("  ✅ 账户认证成功")
        except Exception as e:
            print(f"  ❌ 账户认证失败: {e!r}")
            print()
            print("  可能原因:")
            print("    - 用户名或密码错误")
            print("    - 账户没有主人/经销商权限")
            print("    - 无法访问 apis.control4.com（网络/防火墙）")
            print()
            print("  排查命令:")
            print("    curl -sS -o /dev/null -w '%{http_code}\\n' "
                  "https://apis.control4.com/authentication/v1/rest")
            return False

        # ---- Step 2: 控制器列表 ----
        print()
        print("[2/6] 正在获取控制器列表...")
        try:
            controllers = await account.get_controllers()
            if not controllers:
                print("  ❌ 未找到任何控制器")
                return False
            print(f"  ✅ 找到 {len(controllers)} 台控制器:")
            for i, ctrl in enumerate(controllers):
                print(f"     [{i}] {ctrl['name']}")
                print(f"         型号      : {ctrl['model'] or 'unknown'}")
                print(f"         commonName: {ctrl['common_name']}")
                print(f"         uuid      : {ctrl['uuid']}")
        except Exception as e:
            print(f"  ❌ 获取控制器列表失败: {e}")
            return False

        # ---- Step 3: Director Token ----
        print()
        print("[3/6] 正在获取 Director 访问令牌...")
        try:
            token = await account.get_director_token(controllers[0])
            if not token:
                print("  ❌ 返回的 Token 为空")
                return False
            print("  ✅ Director Token 获取成功")
            print(f"     (前20位: {token[:20]}...)")
        except Exception as e:
            print(f"  ❌ 获取 Director Token 失败: {e}")
            return False

        # ---- Step 4: 连接 Director ----
        print()
        print("[4/6] 正在连接 Director (局域网 REST API)...")
        try:
            director = C4DirectorCompat(ip=ip, token=token, session=session)
            all_items = await director.get_all_item_info()
            print(f"  ✅ Director 连接成功，共发现 {len(all_items)} 个条目")
        except Exception as e:
            print(f"  ❌ Director 连接失败: {e}")
            print()
            print("  可能原因:")
            print("    - IP 地址不正确")
            print("    - 控制器 443 端口未开放")
            print("    - 控制器与本机不在同一局域网")
            return False

        # ---- Step 5: 设备分类统计 ----
        print()
        print("[5/6] 正在按类型统计设备...")
        try:
            type_counts = {}
            category_counts = {}
            samples = {"lights": [], "blinds": [], "thermostats": [],
                       "scenes": [], "relays": [], "rooms": [], "other": []}

            for item in all_items:
                dtype = str(item.get("type", "unknown")).lower()
                dcat = str(item.get("category", "unknown")).lower()
                name = item.get("name", "unknown")
                item_id = item.get("id", 0)

                type_counts[dtype] = type_counts.get(dtype, 0) + 1
                category_counts[dcat] = category_counts.get(dcat, 0) + 1

                label = "  [{}] {}".format(item_id, name)

                if "light" in dtype or "dimmer" in dtype or dcat == "lights":
                    _push(samples["lights"], label)
                elif ("blind" in dtype or "shade" in dtype or "motor" in dtype
                      or dcat in ("motorization", "motors")):
                    _push(samples["blinds"], label)
                elif ("thermostat" in dtype or "hvac" in dtype or "climate" in dtype
                      or dcat in ("thermostats", "climate", "comfort")):
                    _push(samples["thermostats"], "{} (type={})".format(label, dtype))
                elif "scene" in dtype or dcat == "scenes":
                    _push(samples["scenes"], label)
                elif "relay" in dtype or "lock" in dtype or dcat == "relays":
                    _push(samples["relays"], label)
                elif dtype in ("room", "room_device"):
                    _push(samples["rooms"], label)
                else:
                    _push(samples["other"], "{} (type={}, cat={})".format(label, dtype, dcat))

            print("  按 category 分类:")
            for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
                print("    {:28s}: {:3d}".format(cat, count))

            print()
            print("  按 type 分类 (前 20):")
            for dtype, count in sorted(type_counts.items(), key=lambda x: -x[1])[:20]:
                print("    {:32s}: {:3d}".format(dtype, count))

            print()
            print("  示例设备:")
            labels = {
                "lights": "灯光",
                "blinds": "窗帘",
                "thermostats": "空调/温控",
                "scenes": "场景",
                "relays": "继电器",
                "rooms": "房间",
                "other": "其他",
            }
            for key, devices in samples.items():
                if devices:
                    print("    [{}]:".format(labels[key]))
                    for dev in devices:
                        print("      " + dev)
        except Exception as e:
            print(f"  ⚠️  设备统计出错: {e}")

        # ---- Step 6: 读取设备状态 ----
        print()
        print("[6/6] 正在测试设备状态读取...")
        light_items = [
            item for item in all_items
            if "light" in str(item.get("type", "")).lower()
            or "dimmer" in str(item.get("type", "")).lower()
        ]

        if light_items:
            test_item = light_items[0]
            test_id = int(test_item["id"])
            print(f"  测试设备: {test_item.get('name')} (id={test_id})")

            try:
                variables = await director.get_item_variables(test_id)
                print(f"  ✅ 可用变量 {len(variables)} 个 (前10):")
                for v in variables[:10]:
                    print("    - {}: {}".format(v.get("name"), v.get("value")))
                if len(variables) > 10:
                    print("    ... 还有 {} 个".format(len(variables) - 10))
            except Exception as e:
                print(f"  ⚠️  读取变量列表失败: {e}")

            for var in ("LIGHT_STATE", "LIGHT_LEVEL"):
                try:
                    val = await director.get_item_variable_value(test_id, var)
                    print(f"  ✅ {var} = {val}")
                except Exception as e:
                    print(f"  ⚠️  读取 {var} 失败: {e}")
        else:
            print("  ⚠️  未找到灯光设备，跳过")

        # ---- 总结 ----
        print()
        print("=" * 62)
        print("  ✅ 全部测试通过")
        print("=" * 62)
        print()
        print("  提示：可将下面的 Director Token 填入 config.yaml 的")
        print("  control4.director_token，跳过账户认证，加快启动。")
        print()
        print("  director_token: \"{}\"".format(token))
        print()
        return True

    finally:
        await session.close()


def _push(bucket: list, value: str, limit: int = 6) -> None:
    if len(bucket) < limit:
        bucket.append(value)


def main():
    parser = argparse.ArgumentParser(description="Control4 API 连通性测试")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")
    parser.add_argument("--ip", help="C4 控制器 IP（覆盖配置文件）")
    parser.add_argument("--user", "-u", help="C4 账户用户名")
    parser.add_argument("--pass", "-p", dest="password", help="C4 账户密码")
    args = parser.parse_args()

    ip, username, password, saved_token = args.ip, args.user, args.password, ""

    if not (ip and username and password):
        config_path = find_config(args.config)
        if not config_path:
            print("❌ 未找到配置文件，尝试过的路径:")
            for p in CONFIG_CANDIDATES:
                print("    " + p)
            print()
            print("请指定配置文件，或用命令行参数：")
            print("  python3 test_c4_api.py --ip <IP> --user <账号> --pass <密码>")
            sys.exit(1)

        print(f"已加载配置文件: {config_path}")
        print()
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        c4_cfg = cfg.get("control4", {}) or {}
        ip = ip or c4_cfg.get("ip", "")
        username = username or c4_cfg.get("username", "")
        password = password or c4_cfg.get("password", "")
        saved_token = c4_cfg.get("director_token", "") or ""

    if not ip or not username or not password:
        print("❌ 缺少必要参数（ip / username / password）")
        sys.exit(1)

    try:
        ok = asyncio.run(test_c4_connection(ip, username, password, saved_token))
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(130)


if __name__ == "__main__":
    main()
