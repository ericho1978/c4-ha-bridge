#!/usr/bin/env python3
"""
C4-HA Bridge 现场校验脚本（在 HA 主机上运行，需能访问 C4 控制器）。

默认【只读】，不会动作任何设备：
  1. 连接 Control4（账户认证或配置里的 token）
  2. 按 proxy + type=7 发现设备，打印分类统计
  3. 校验批量变量接口是否可用（决定轮询效率）
  4. 打印代表性设备的原始变量与解析后状态（确认映射正确）

加 --apply 才会真的下发控制命令（会开灯、动窗帘、改空调模式）：
  5. 逐个实测 灯 / 窗帘 / 空调 / 继电器，记录前后状态，最后尽量恢复原状态

用法：
    python3 test_c4_control.py
    python3 test_c4_control.py --apply
    python3 test_c4_control.py --apply --light 472 --blind 493 --thermostat 26
"""
import argparse
import asyncio
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

CONFIG_CANDIDATES = ["config/config.yaml", "config.yaml", "/app/config/config.yaml"]


def find_config(explicit=None):
    for path in ([explicit] if explicit else []) + CONFIG_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def line(title=""):
    print()
    if title:
        print("-" * 74)
        print(title)
        print("-" * 74)


class _StubMqtt:
    """仅用于构造设备对象，不真正发布。"""

    async def publish(self, *args, **kwargs):
        return None

    async def subscribe(self, *args, **kwargs):
        return None


async def run(ip, username, password, saved_token, apply_changes, pick, settle,
              only=None):
    import aiohttp
    from c4_compat import C4AccountCompat, C4DirectorCompat
    from devices import resolve_device_class

    from config import DEFAULT_PROXY_MAP

    line("1. 连接 Control4")
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False),
        timeout=aiohttp.ClientTimeout(total=30),
    )
    try:
        token = saved_token
        if token:
            print("  使用配置文件中的 director_token")
            director = C4DirectorCompat(ip=ip, token=token, session=session)
            try:
                await director.get_all_item_info()
                print("  ✅ token 有效")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  token 无效（{exc}），改用账户认证")
                token = ""
        if not token:
            account = C4AccountCompat(username, password, session=None)
            await account.authenticate()
            controllers = await account.get_controllers()
            if not controllers:
                print("  ❌ 未找到控制器")
                return 1
            for c in controllers:
                print(f"  控制器: {c['name']} / {c['common_name']}")
            token = await account.get_director_token(controllers[0])
            director = C4DirectorCompat(ip=ip, token=token, session=session)
        print("  ✅ 已连接 Director")

        # -------------------------------------------------------------
        line("2. 设备发现（type=7 + proxy）")
        # -------------------------------------------------------------
        all_items = await director.get_all_item_info()
        proxies = [it for it in all_items if int(it.get("type", 0)) == 7]
        print(f"  条目总数 {len(all_items)}，其中 type=7 代理 {len(proxies)}")

        by_component = {}
        picked = {}
        skipped = {}
        for it in proxies:
            proxy = str(it.get("proxy") or "")
            cls = resolve_device_class(proxy, DEFAULT_PROXY_MAP)
            if cls is None:
                skipped[proxy] = skipped.get(proxy, 0) + 1
                continue
            comp = cls.ha_component
            by_component.setdefault(comp, []).append(it)

        for comp in sorted(by_component):
            devices = by_component[comp]
            print(f"  {comp:9s} {len(devices):4d} 个   例: "
                  + ", ".join(str(d.get("name")) for d in devices[:3]))
        if skipped:
            top = sorted(skipped.items(), key=lambda x: -x[1])[:8]
            print("  跳过（未支持 proxy）: "
                  + ", ".join(f"{p}×{c}" for p, c in top))

        total = sum(len(v) for v in by_component.values())
        print(f"  ── 可桥接设备合计: {total}")

        # 选代表设备
        item_name = {}
        for it in all_items:
            try:
                item_name[int(it.get("id"))] = str(it.get("name") or "")
            except (TypeError, ValueError):
                continue

        for comp, key in (("light", "light"), ("cover", "blind"),
                          ("climate", "thermostat"), ("switch", "relay")):
            devices = by_component.get(comp, [])
            if not devices:
                continue
            want = pick.get(key)
            chosen = None
            if want:
                chosen = next((d for d in devices if int(d["id"]) == want), None)
                if chosen is None:
                    print(f"  ⚠️  指定的 {key} id={want} 不属于 {comp} 类")
            if chosen is None and comp == "light":
                # 灯光优先选调光型（父驱动名含 Dimmer），便于顺带验证调光
                chosen = next(
                    (d for d in devices
                     if "dimmer" in item_name.get(
                         int(d.get("parentId") or 0), "").lower()),
                    None,
                )
            picked[key] = chosen or devices[0]

        # -------------------------------------------------------------
        line("3. 批量变量接口校验（影响轮询效率）")
        # -------------------------------------------------------------
        sample_vars = set()
        for comp, items in by_component.items():
            cls = resolve_device_class(str(items[0].get("proxy")), DEFAULT_PROXY_MAP)
            if cls:
                sample_vars |= set(cls.STATE_VARS)
        names = sorted(sample_vars)
        print(f"  变量名 {len(names)} 个: {', '.join(names)}")

        batch_ok = False
        try:
            data = await director.get_all_items_variables(names)
            print(f"  一次批量请求 -> 返回 {len(data)} 个设备的数据")
            batch_ok = bool(data)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 批量接口失败: {exc}")
        if not batch_ok:
            try:
                data = await director.get_all_items_variables(["LIGHT_STATE"])
                print(f"  单变量批量（LIGHT_STATE）-> {len(data)} 个设备")
                batch_ok = bool(data)
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ 单变量批量也失败: {exc}")

        # 一致性抽查
        if batch_ok and picked:
            print("  与逐设备读取一致性抽查:")
            for key, item in picked.items():
                iid = int(item["id"])
                try:
                    per = await director.get_item_variables_map(iid)
                except Exception as exc:  # noqa: BLE001
                    print(f"    [{iid}] 逐设备读取失败: {exc}")
                    continue
                common = {k: v for k, v in per.items() if k in names}
                print(f"    [{iid}] {item.get('name')}: {common}")

        # -------------------------------------------------------------
        line("4. 代表设备原始变量与解析状态")
        # -------------------------------------------------------------
        parsed = {}
        for key, item in picked.items():
            iid = int(item["id"])
            cls = resolve_device_class(str(item.get("proxy")), DEFAULT_PROXY_MAP)
            print(f"\n  [{iid}] {item.get('name')}  proxy={item.get('proxy')} "
                  f"room={item.get('roomName')}")

            raw = await director.get_item_variables_map(iid)
            interesting = {k: raw.get(k) for k in cls.STATE_VARS if k in raw}
            print(f"    原始变量: {json.dumps(interesting, ensure_ascii=False)}")

            cmds = await director.get_item_commands(iid)
            cmd_names = [c.get("command") for c in cmds if isinstance(c, dict)]
            print(f"    可用命令: {', '.join(str(c) for c in cmd_names)}")

            # 用真实设备类解析（走正常构造流程）
            dev = cls(
                item_id=iid,
                name=str(item.get("name")),
                c4=director,
                mqtt=_StubMqtt(),
                topic_prefix="c4-ha",
                discovery_prefix="homeassistant",
                room=str(item.get("roomName") or ""),
                proxy=str(item.get("proxy") or ""),
                raw=item,
            )
            dev.update_from_vars(raw)
            parsed[key] = (dev, raw, cmd_names)
            print(f"    解析后状态: {json.dumps(dev._state, ensure_ascii=False)}")

        if not apply_changes:
            line("只读模式结束")
            print("  未下发任何控制命令。")
            print("  要实测控制，请运行：python3 test_c4_control.py --apply")
            return 0

        # -------------------------------------------------------------
        line("5. 控制实测（--apply）")
        # -------------------------------------------------------------
        targets = only or {"light", "blind", "thermostat", "relay"}
        print(f"  ⚠️  将会真实操作设备，完成后会尽量恢复原状态")
        print(f"  本次实测范围: {', '.join(sorted(targets))}")
        print(f"  下发后等待 {settle}s 再回读")

        # ---- 灯 ----
        if "light" in targets and "light" in parsed:
            dev, _raw, light_cmds = parsed["light"]
            before = dict(dev._state)
            target = "OFF" if before.get("state") == "ON" else "ON"
            print(f"\n  [灯 {dev.item_id}] {dev.full_name}")
            print(f"    可调光: {dev.is_dimmer}")
            print(f"    原状态: {before}  -> 发送 {target}")
            try:
                await director.send_command(dev.item_id, target, {})
                await asyncio.sleep(settle)
                after = await director.get_item_variables_map(dev.item_id)
                print(f"    回读 LIGHT_STATE = {after.get('LIGHT_STATE')}")
                ok = (str(after.get("LIGHT_STATE")) in ("1", "True", "true")) == (target == "ON")
                print(f"    {'✅ 状态已按预期变化' if ok else '❌ 状态未变化，请检查'}")

                # 调光设备再测一次亮度
                if dev.is_dimmer and "SET_LEVEL" in light_cmds:
                    print("    测试调光 SET_LEVEL 70 ...")
                    await director.send_command(dev.item_id, "SET_LEVEL", {"LEVEL": 70})
                    await asyncio.sleep(settle)
                    after = await director.get_item_variables_map(dev.item_id)
                    lvl = after.get("LIGHT_LEVEL")
                    print(f"    回读 LIGHT_LEVEL = {lvl}"
                          f"  {'✅' if str(lvl) == '70' else '⚠️ 与 70 不一致'}")

                # 恢复
                restore = "ON" if before.get("state") == "ON" else "OFF"
                await director.send_command(dev.item_id, restore, {})
                print(f"    已恢复为 {restore}")
            except Exception as exc:  # noqa: BLE001
                print(f"    ❌ 控制失败: {exc}")

        # ---- 窗帘 ----
        if "blind" in targets and "cover" in parsed:
            dev, _raw, _cmds = parsed["cover"]
            before = dict(dev._state)
            print(f"\n  [窗帘 {dev.item_id}] {dev.full_name}")
            print(f"    原状态: {before}")
            for label, form in (("写法A", "A"), ("写法B", "B")):
                params = ({"LEVEL_TARGET": "LEVEL_TARGET_OPEN"}
                          if form == "A" else {})
                command = ("SET_LEVEL_TARGET" if form == "A"
                           else "SET_LEVEL_TARGET:LEVEL_TARGET_OPEN")
                try:
                    await director.send_command(dev.item_id, command, params)
                    print(f"    {label}: command={command} params={params} -> 已被接受")
                except Exception as exc:  # noqa: BLE001
                    print(f"    {label}: command={command} params={params} -> 被拒绝: {exc}")

            try:
                await asyncio.sleep(settle)
                after = await director.get_item_variables_map(dev.item_id)
                keys = ("Open", "Fully Open", "Fully Closed", "Level", "Movement")
                print("    回读: " + json.dumps(
                    {k: after.get(k) for k in keys}, ensure_ascii=False))
                await director.send_command(dev.item_id, "STOP", {})
                print("    已发送 STOP")
            except Exception as exc:  # noqa: BLE001
                print(f"    ⚠️  {exc}")

        # ---- 空调 ----
        if "thermostat" in targets and "climate" in parsed:
            dev, raw, _cmds = parsed["climate"]
            before = dict(dev._state)
            print(f"\n  [空调 {dev.item_id}] {dev.full_name}")
            print(f"    原状态: {before}")
            print(f"    模式表: {dev._ha_modes}  反向: {dev._ha_to_c4}")
            print(f"    风速表: {dev._ha_fan_modes}")
            orig_mode = raw.get("HVAC_MODE")
            try:
                test_mode = dev._ha_to_c4.get("cool")
                if test_mode:
                    await director.send_command(
                        dev.item_id, "SET_MODE_HVAC", {"MODE": test_mode})
                    await asyncio.sleep(settle)
                    after = await director.get_item_variables_map(dev.item_id)
                    print(f"    设模式 Cool -> 回读 HVAC_MODE = {after.get('HVAC_MODE')}")
                target = 25
                param = "CELSIUS" if dev.use_celsius else "FAHRENHEIT"
                cmd = ("SET_SETPOINT_HEAT" if str(orig_mode).lower() == "heat"
                       else "SET_SETPOINT_COOL")
                await director.send_command(dev.item_id, cmd, {param: target})
                await asyncio.sleep(settle)
                after = await director.get_item_variables_map(dev.item_id)
                key = f"COOL_SETPOINT_{'C' if dev.use_celsius else 'F'}"
                print(f"    设 {cmd} {target} -> 回读 {key} = {after.get(key)}")
                if orig_mode:
                    await director.send_command(
                        dev.item_id, "SET_MODE_HVAC", {"MODE": orig_mode})
                    print(f"    已恢复原标题模式 {orig_mode}")
            except Exception as exc:  # noqa: BLE001
                print(f"    ❌ 控制失败: {exc}")

        # ---- 继电器 ----
        if "relay" in targets and "relay" in parsed:
            dev, _raw, _cmds = parsed["relay"]
            before = dict(dev._state)
            print(f"\n  [继电器 {dev.item_id}] {dev.full_name}")
            print(f"    原状态: {before}（不做开关实测，避免影响供电设备）")

        line("实测结束")
        return 0
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(description="C4-HA Bridge 现场校验")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--ip")
    parser.add_argument("--user", "-u")
    parser.add_argument("--pass", "-p", dest="password")
    parser.add_argument("--apply", action="store_true",
                        help="真的下发控制命令（默认只读）")
    parser.add_argument("--light", type=int, help="指定灯光 item id")
    parser.add_argument("--blind", type=int, help="指定窗帘 item id")
    parser.add_argument("--thermostat", type=int, help="指定空调 item id")
    parser.add_argument("--relay", type=int, help="指定继电器 item id")
    parser.add_argument("--settle", type=int, default=5,
                        help="下发命令后等待几秒再回读（默认 5）")
    parser.add_argument("--only", default=None,
                        help="只实测指定类型（默认全部）。"
                             "可用值：light, cover/blind, climate/thermostat, "
                             "switch/relay；逗号分隔。客户在使用时建议只测单项。")
    args = parser.parse_args()

    ip, username, password, token = args.ip, args.user, args.password, ""
    if not (ip and username and password):
        path = find_config(args.config)
        if not path:
            print("❌ 未找到配置文件")
            sys.exit(1)
        print(f"已加载配置: {path}")
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

    pick = {
        "light": args.light, "blind": args.blind,
        "thermostat": args.thermostat, "relay": args.relay,
    }

    only = None
    if args.only:
        # 同时接受 HA 组件名与内部逻辑名，避免记混
        alias = {
            "light": "light",
            "cover": "blind", "blind": "blind", "curtain": "blind",
            "climate": "thermostat", "thermostat": "thermostat",
            "ac": "thermostat",
            "switch": "relay", "relay": "relay",
        }
        raw = [x.strip().lower() for x in args.only.split(",") if x.strip()]
        unknown = [x for x in raw if x not in alias]
        if unknown:
            print(f"❌ --only 含未知类型: {', '.join(unknown)}")
            print("   可用值: light, cover/blind, climate/thermostat, switch/relay")
            sys.exit(1)
        only = {alias[x] for x in raw}
        if not only:
            print("❌ --only 为空")
            sys.exit(1)

    ok = asyncio.run(run(ip, username, password, token, args.apply, pick,
                         args.settle, only))
    sys.exit(ok)


if __name__ == "__main__":
    main()
