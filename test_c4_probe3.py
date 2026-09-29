#!/usr/bin/env python3
"""
Control4 设备能力普查 v3。

目标：确认各类设备的真实「变量名集合」与「命令集合」，用于编写映射表。

  [A] 按 proxy 分组，抽样统计变量集合与命令集合的组合分布
  [B] 对 light_v2 / blind / thermostatV2 各取一个代表，完整打印
      变量列表（varName/type/value）与命令原始 JSON

用法：
    python3 test_c4_probe3.py
"""
import argparse
import asyncio
import json
import os
import sys
from collections import Counter

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

CONFIG_CANDIDATES = ["config/config.yaml", "config.yaml", "/app/config/config.yaml"]

# 关心的 proxy 及其抽样数量
TARGETS = [
    ("light_v2", 50),
    ("blind", 20),
    ("thermostatV2", 15),
    ("relaysingle_relay_c4", 10),
    ("****_****_Relay", 10),
    ("keypad_proxy", 10),
]

FULL_DUMP_PROXIES = ["light_v2", "blind", "thermostatV2"]


def find_config(explicit=None):
    for path in ([explicit] if explicit else []) + CONFIG_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def dump(obj, limit=3000):
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > limit:
        text = text[:limit] + "\n... (已截断)"
    return text


async def get_json(director, uri):
    raw = await director.send_get_request(uri)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None


async def fetch_varnames(director, item_id):
    data = await get_json(director, "/api/v1/items/{}/variables".format(item_id))
    if not isinstance(data, list):
        return [], data
    names = []
    for v in data:
        if isinstance(v, dict):
            names.append(str(v.get("varName")))
    return sorted(names), data


async def fetch_commands(director, item_id):
    data = await get_json(director, "/api/v1/items/{}/commands".format(item_id))
    if not isinstance(data, list):
        return [], data
    names = []
    for c in data:
        if isinstance(c, dict):
            names.append(str(c.get("command")))
        else:
            names.append(str(c))
    return sorted(names), data


async def probe(ip, username, password, saved_token=""):
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

        # type=7 的代理设备
        proxies = [it for it in all_items if int(it.get("type", 0)) == 7]
        print("  type=7 代理设备共 {} 个".format(len(proxies)))

        by_proxy = {}
        for it in proxies:
            by_proxy.setdefault(it.get("proxy"), []).append(it)

        # =============================================================
        # [A] 能力组合普查
        # =============================================================
        print()
        print("=" * 74)
        print("[A] 各 proxy 的变量集合 / 命令集合组合分布")
        print("=" * 74)

        full_dump_ids = {}
        for proxy, limit in TARGETS:
            items = by_proxy.get(proxy, [])
            if not items:
                print()
                print("### {} —— 无设备".format(proxy))
                continue

            sample = items[:limit]
            print()
            print("### {} —— 共 {} 个，抽样 {}".format(
                proxy, len(items), len(sample)))

            var_combos = Counter()
            cmd_combos = Counter()
            errors = 0
            for it in sample:
                iid = int(it["id"])
                try:
                    varnames, _ = await fetch_varnames(director, iid)
                    cmdnames, _ = await fetch_commands(director, iid)
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        print("    ⚠️  [{}] 读取失败: {}".format(iid, str(e)[:70]))
                    continue

                var_combos[tuple(varnames)] += 1
                cmd_combos[tuple(cmdnames)] += 1
                if proxy in FULL_DUMP_PROXIES and proxy not in full_dump_ids:
                    full_dump_ids[proxy] = iid

            print("    变量集合组合（{} 种）:".format(len(var_combos)))
            for combo, cnt in var_combos.most_common(6):
                print("      [{} 个设备] {}".format(cnt, ", ".join(combo) or "(无)"))

            print("    命令集合组合（{} 种）:".format(len(cmd_combos)))
            for combo, cnt in cmd_combos.most_common(6):
                print("      [{} 个设备] {}".format(cnt, ", ".join(combo) or "(无)"))

            if errors:
                print("    （{} 个设备读取失败）".format(errors))

        # =============================================================
        # [B] 代表设备完整结构
        # =============================================================
        print()
        print("=" * 74)
        print("[B] 代表设备完整变量与命令")
        print("=" * 74)

        for proxy, iid in full_dump_ids.items():
            item = next((x for x in proxies if int(x["id"]) == iid), {})
            print()
            print("#" * 74)
            print("### {} —— [{}] {} (room={})".format(
                proxy, iid, item.get("name"), item.get("roomName")))
            print("#" * 74)

            try:
                _, var_data = await fetch_varnames(director, iid)
                print("  >> 全部变量:")
                if isinstance(var_data, list):
                    for v in var_data:
                        if not isinstance(v, dict):
                            continue
                        print("     {:34s} type={:8s} hidden={} value={!r}".format(
                            str(v.get("varName")),
                            str(v.get("type")),
                            v.get("hidden"),
                            v.get("value"),
                        ))
                else:
                    print("     " + dump(var_data, 800))
            except Exception as e:
                print("  ⚠️  变量读取失败: {}".format(str(e)[:90]))

            try:
                _, cmd_data = await fetch_commands(director, iid)
                print("  >> 全部命令（原始 JSON）:")
                print("     " + dump(cmd_data, 2200))
            except Exception as e:
                print("  ⚠️  命令读取失败: {}".format(str(e)[:90]))

        print()
        print("=" * 74)
        print("  普查完成，请把以上输出完整贴回")
        print("=" * 74)
        return True
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(description="Control4 能力普查 v3")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--ip")
    parser.add_argument("--user", "-u")
    parser.add_argument("--pass", "-p", dest="password")
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

    ok = asyncio.run(probe(ip, username, password, token))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
