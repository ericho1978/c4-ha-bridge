#!/usr/bin/env python3
"""
列出可桥接设备清单，用于挑选测试设备或配置 include_ids / exclude_ids。

特性：
  - 一次批量请求即可标出哪些灯是可调光（拉 varnames=LIGHT_LEVEL）
  - 支持按名称 / 房间关键词过滤
  - 支持导出 CSV，便于在表格里筛选

用法：
    python3 test_c4_list.py                          # 列出全部灯光
    python3 test_c4_list.py --component light
    python3 test_c4_list.py --match 公卫,客卫,卫生间   # 按名称或房间模糊匹配
    python3 test_c4_list.py --rooms                  # 只看房间清单
    python3 test_c4_list.py --csv /tmp/c4_devices.csv
"""
import argparse
import asyncio
import csv
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

CONFIG_CANDIDATES = ["config/config.yaml", "config.yaml", "/app/config/config.yaml"]

COMPONENT_LABEL = {
    "light": "灯光",
    "cover": "窗帘",
    "climate": "空调",
    "switch": "继电",
}


def find_config(explicit=None):
    for path in ([explicit] if explicit else []) + CONFIG_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


async def probe(ip, username, password, saved_token="", component=None,
                matches=None, rooms_only=False, csv_path=None,
                dimmer_only=False):
    import aiohttp
    from c4_compat import C4AccountCompat, C4DirectorCompat
    from config import DEFAULT_PROXY_MAP
    from devices import resolve_device_class

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

        # 一次性拿到所有带 LIGHT_LEVEL 的设备 id（即可调光设备）
        dimmers = set()
        try:
            data = await director.get_all_items_variables(["LIGHT_LEVEL"])
            dimmers = set(data.keys())
            print("  可调光设备（含 LIGHT_LEVEL）共 {} 个".format(len(dimmers)))
        except Exception as exc:  # noqa: BLE001
            print("  ⚠️  读取 LIGHT_LEVEL 失败，调光列将为空: {}".format(
                str(exc)[:70]))

        # 组装可桥接设备
        devices = []
        for it in all_items:
            if int(it.get("type", 0)) != 7:
                continue
            proxy = str(it.get("proxy") or "")
            cls = resolve_device_class(proxy, DEFAULT_PROXY_MAP)
            if cls is None:
                continue
            try:
                iid = int(it["id"])
            except (TypeError, ValueError):
                continue
            parent = by_id.get(int(it.get("parentId") or 0), {})
            devices.append({
                "id": iid,
                "component": cls.ha_component,
                "comp_label": COMPONENT_LABEL.get(cls.ha_component, cls.ha_component),
                "room": str(it.get("roomName") or ""),
                "name": str(it.get("name") or ""),
                "driver": str(parent.get("name") or ""),
                "proxy": proxy,
                "dimmer": "是" if iid in dimmers else "",
            })

        print("  可桥接设备合计 {} 个".format(len(devices)))

        # ---------------- 房间清单 ----------------
        if rooms_only:
            rooms = {}
            for d in devices:
                rooms.setdefault(d["room"] or "(无房间)", []).append(d)
            print()
            print("=" * 74)
            print("房间清单（{} 个）".format(len(rooms)))
            print("=" * 74)
            for room in sorted(rooms):
                items = rooms[room]
                comps = {}
                for d in items:
                    comps[d["comp_label"]] = comps.get(d["comp_label"], 0) + 1
                detail = " ".join("{}×{}".format(k, v)
                                  for k, v in sorted(comps.items()))
                light_names = [d["name"] for d in items if d["component"] == "light"]
                print("  {:<16} 共{:>4}个   {}".format(room, len(items), detail))
                if light_names:
                    print("     灯: {}".format(", ".join(light_names[:12])
                                             + (" ..." if len(light_names) > 12 else "")))
            return 0

        # ---------------- 过滤 ----------------
        selected = devices
        if dimmer_only:
            selected = [d for d in selected if d["dimmer"]]
        if component:
            wanted = {x.strip().lower() for x in component.split(",") if x.strip()}
            selected = [d for d in selected if d["component"] in wanted]
        if matches:
            keys = [k.strip().lower() for k in matches if k.strip()]
            selected = [
                d for d in selected
                if any(k in d["name"].lower() or k in d["room"].lower()
                       for k in keys)
            ]

        selected.sort(key=lambda d: (d["component"], d["room"], d["id"]))

        print()
        print("=" * 74)
        title = "设备清单"
        if dimmer_only:
            title += "  仅可调光"
        if component:
            title += "  类型={}".format(component)
        if matches:
            title += "  匹配={}".format(",".join(matches))
        print("{}（{} 个）".format(title, len(selected)))
        print("=" * 74)
        print("  {:>6}  {:<4} {:<14} {:<22} {:<20} {}".format(
            "id", "类型", "房间", "名称", "驱动", "调光"))
        shown = 0
        for d in selected:
            shown += 1
            print("  {:>6}  {:<4} {:<14} {:<22} {:<20} {}".format(
                d["id"], d["comp_label"], d["room"][:14], d["name"][:22],
                d["driver"][:20], d["dimmer"]))
        if not selected:
            print("  （无匹配设备）")
            print()
            print("  提示：用 --rooms 看看有哪些房间名，再按房间关键词匹配")

        # ---------------- 导出 CSV ----------------
        if csv_path:
            try:
                with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(
                        f, fieldnames=["id", "component", "room", "name",
                                       "driver", "proxy", "dimmer"])
                    writer.writeheader()
                    for d in devices:
                        writer.writerow(d)
                print()
                print("  已导出 CSV: {}（{} 行）".format(csv_path, len(devices)))
            except Exception as exc:  # noqa: BLE001
                print("  ⚠️  导出 CSV 失败: {}".format(exc))

        print()
        return 0
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(description="列出可桥接设备清单")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--ip")
    parser.add_argument("--user", "-u")
    parser.add_argument("--pass", "-p", dest="password")
    parser.add_argument("--component", default=None,
                        help="按类型过滤：light,cover,climate,switch（逗号分隔）")
    parser.add_argument("--match", default=None,
                        help="按名称或房间模糊匹配，逗号分隔多个关键词")
    parser.add_argument("--rooms", action="store_true",
                        help="只列房间清单")
    parser.add_argument("--dimmer-only", action="store_true", dest="dimmer_only",
                        help="只列可调光设备")
    parser.add_argument("--csv", default=None, dest="csv_path",
                        help="导出全部设备到 CSV")
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

    matches = None
    if args.match:
        matches = [x for x in args.match.replace("，", ",").split(",") if x.strip()]

    ok = asyncio.run(probe(ip, username, password, token, args.component,
                           matches, args.rooms, args.csv_path,
                           args.dimmer_only))
    sys.exit(ok)


if __name__ == "__main__":
    main()
