#!/usr/bin/env python3
"""
定向探测 5：****_****_Relay（22 个继电器灯）的控制入口在哪。

已知：
  - 这些 type=7 条目的 /variables 与 /commands 均为空
  - 但 URIs 里额外暴露了 /properties（之前未查询）
  - bindings 显示绑定到 ****_****_Gateway（二楼灯光网关）

本脚本查：
  [1] /properties 端点返回什么（继电器 / 灯 / 网关 对照）
  [2] 网关条目的完整 API 面（variables / commands / properties）
  [3] 全部 ***** 相关条目汇总
  [4] 可选：对单个继电器试探候选命令，看 HTTP 层是否接受

用法：
    python3 test_c4_probe5.py
    python3 test_c4_probe5.py --try-commands          # 会尝试真正下发命令
    python3 test_c4_probe5.py --relay-id 2578 --try-commands
"""
import argparse
import asyncio
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

CONFIG_CANDIDATES = ["config/config.yaml", "config.yaml", "/app/config/config.yaml"]

# 试探用的候选命令（仅 --try-commands 时下发）
CANDIDATE_COMMANDS = [
    ("ON", {}),
    ("OFF", {}),
    ("TOGGLE", {}),
    ("CLOSE", {}),
    ("OPEN", {}),
    ("PULSE", {}),
    ("SET_LEVEL", {"LEVEL": 0}),
    ("RELAY_STATE", {"STATE": 1}),
]


def find_config(explicit=None):
    for path in ([explicit] if explicit else []) + CONFIG_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def dump(obj, limit=1800):
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > limit:
        text = text[:limit] + "\n... (已截断)"
    return text.replace("\n", "\n    ")


async def get_json(director, uri):
    raw = await director.send_get_request(uri)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw


async def probe(ip, username, password, saved_token="",
                try_commands=False, relay_id=None):
    import aiohttp
    from c4_compat import C4AccountCompat, C4DirectorCompat

    token = saved_token
    if not token:
        print("[认证] 获取 Director Token ...")
        account = C4AccountCompat(username, password, session=None)
        await account.authenticate()
        controllers = await account.get_controllers()
        token = await account.get_director_token(controllers[0])
    print("  ✅ Token 就绪")

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False),
        timeout=aiohttp.ClientTimeout(total=30),
    )
    try:
        director = C4DirectorCompat(ip=ip, token=token, session=session)
        all_items = await director.get_all_item_info()
        by_id = {}
        for it in all_items:
            try:
                by_id[int(it.get("id"))] = it
            except (TypeError, ValueError):
                continue

        relays = [it for it in all_items
                  if it.get("proxy") == "****_****_Relay"]
        print("  ****_****_Relay 条目 {} 个".format(len(relays)))

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[1] /properties 端点对照")
        print("=" * 74)

        samples = []
        if relays:
            samples.append(("继电器", relays[0]))
        light = next((it for it in all_items
                      if it.get("proxy") == "light_v2"
                      and int(it.get("type", 0)) == 7), None)
        if light:
            samples.append(("灯光(对照)", light))
        gateway = next((it for it in all_items
                        if it.get("proxy") == "****_****_Gateway"), None)
        if gateway:
            samples.append(("网关", gateway))

        for label, item in samples:
            iid = int(item["id"])
            print()
            print("  --- {} [{}] {} ---".format(label, iid, item.get("name")))
            for endpoint in ("properties", "variables", "commands", "network"):
                uri = "/api/v1/items/{}/{}".format(iid, endpoint)
                try:
                    data = await get_json(director, uri)
                    if isinstance(data, list) and not data:
                        print("     {}: (空列表)".format(endpoint))
                    else:
                        print("     {}:".format(endpoint))
                        print("       " + dump(data, 1000))
                except Exception as exc:  # noqa: BLE001
                    print("     {}: ❌ {}".format(endpoint, str(exc)[:90]))

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[2] 网关条目完整信息")
        print("=" * 74)
        if gateway is None:
            print("  ⚠️  未找到 ****_****_Gateway 条目")
        else:
            gid = int(gateway["id"])
            print("  [{}] {}  room={}".format(gid, gateway.get("name"),
                                              gateway.get("roomName")))
            print("  完整 JSON:")
            print("    " + dump(gateway, 1600))

            for endpoint in ("variables", "commands", "properties", "bindings"):
                uri = "/api/v1/items/{}/{}".format(gid, endpoint)
                try:
                    data = await get_json(director, uri)
                    print("  >> {}".format(endpoint))
                    if isinstance(data, list) and not data:
                        print("       (空列表)")
                    elif isinstance(data, list):
                        for entry in data[:60]:
                            if isinstance(entry, dict):
                                name = entry.get("varName") or entry.get("command")
                                print("       {}  = {!r}".format(
                                    str(name), entry.get("value", entry.get("label"))))
                            else:
                                print("       {}".format(entry))
                    else:
                        print("       " + dump(data, 900))
                except Exception as exc:  # noqa: BLE001
                    print("  >> {}: ❌ {}".format(endpoint, str(exc)[:90]))

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[3] 全部 **** 相关条目")
        print("=" * 74)
        insona = [it for it in all_items
                  if "****" in str(it.get("proxy", ""))
                  or "****" in str(it.get("filename", ""))]
        print("  共 {} 个".format(len(insona)))
        for it in sorted(insona, key=lambda x: int(x.get("id", 0))):
            iid = int(it["id"])
            uris = it.get("URIs") if isinstance(it.get("URIs"), dict) else {}
            print("    [{:<5}] t={:<2} {:<22} proxy={:<28} 端点={}".format(
                iid, it.get("type"), str(it.get("name"))[:22],
                str(it.get("proxy"))[:28], ",".join(sorted(uris.keys()))))

        # 继电器全部名称
        print()
        print("  ****_****_Relay 全部条目名称:")
        names = sorted(str(it.get("name")) for it in relays)
        print("    " + ", ".join(names))

        # -------------------------------------------------------------
        if try_commands:
            print()
            print("=" * 74)
            print("[4] 候选命令试探（会真正下发）")
            print("=" * 74)
            target = None
            if relay_id:
                target = next((it for it in relays if int(it["id"]) == relay_id), None)
            if target is None:
                target = relays[0] if relays else None
            if target is None:
                print("  ⚠️  没有可试探的继电器条目")
            else:
                tid = int(target["id"])
                print("  目标: [{}] {}（先读 properties 作为基线）".format(
                    tid, target.get("name")))
                try:
                    before = await get_json(
                        director, "/api/v1/items/{}/properties".format(tid))
                    print("    基线 properties: " + dump(before, 600))
                except Exception as exc:  # noqa: BLE001
                    print("    基线读取失败: {}".format(str(exc)[:80]))

                for command, params in CANDIDATE_COMMANDS:
                    try:
                        await director.send_command(tid, command, params)
                        print("    {:<14} params={:<24} -> ✅ 被接受".format(
                            command, json.dumps(params)))
                    except Exception as exc:  # noqa: BLE001
                        print("    {:<14} params={:<24} -> ❌ {}".format(
                            command, json.dumps(params), str(exc)[:70]))

                await asyncio.sleep(3)
                try:
                    after = await get_json(
                        director, "/api/v1/items/{}/properties".format(tid))
                    print("    试探后 properties: " + dump(after, 600))
                except Exception as exc:  # noqa: BLE001
                    print("    试探后读取失败: {}".format(str(exc)[:80]))
                print()
                print("  注意：若上述命令全部被拒绝，说明该驱动未向 Director 暴露控制接口。")

        print()
        print("=" * 74)
        print("  探测完成，请把以上输出完整贴回")
        print("=" * 74)
        return True
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(description="**** 继电器控制入口探测")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--ip")
    parser.add_argument("--user", "-u")
    parser.add_argument("--pass", "-p", dest="password")
    parser.add_argument("--try-commands", action="store_true",
                        help="真的下发候选命令试探（默认只读）")
    parser.add_argument("--relay-id", type=int, default=None,
                        help="指定试探的继电器 item id")
    args = parser.parse_args()

    ip, username, password, token = args.ip, args.user, args.password, ""
    if not (ip and username and password):
        path = find_config(args.config)
        if not path:
            print("❌ 未找到配置文件")
            sys.exit(1)
        print("已加载配置: {}".format(path))
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        c4 = cfg.get("control4", {}) or {}
        ip = ip or c4.get("ip", "")
        username = username or c4.get("username", "")
        password = password or c4.get("password", "")
        token = c4.get("director_token", "") or ""

    if not ip or (not token and (not username or not password)):
        print("❌ 缺少 ip 或认证信息")
        sys.exit(1)

    ok = asyncio.run(probe(ip, username, password, token,
                           args.try_commands, args.relay_id))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
