#!/usr/bin/env python3
"""
决定性探测：****_****_Relay 能否通过 Director REST API 控制。

已确认的事实：
  - 这 22 个 type=7 条目的 /variables 与 /commands 均为空
  - 但它们暴露了 /properties，其中 "Relay Status" 的 readonly = false（可写），
    取值 OPENED / CLOSED，当前值 OFF
  - 灯（472）同样有 readonly=false 的 "Light Status"，但它另有 variables/commands

本脚本实测两条可能的控制通道：
  [A] POST /commands 试探候选命令（ON/OFF/TOGGLE/CLOSE/OPEN/...）
  [B] 写属性试探：PUT / POST / PATCH /properties，多种 body 格式

默认【只读】，只打印基线与将要尝试的内容；
加 --apply 才真正下发，并在每次尝试后回读属性判断是否生效。

用法：
    python3 test_c4_probe6.py
    python3 test_c4_probe6.py --apply
    python3 test_c4_probe6.py --apply --relay-id 2578
"""
import argparse
import asyncio
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

CONFIG_CANDIDATES = ["config/config.yaml", "config.yaml", "/app/config/config.yaml"]

PROPERTY_NAME = "Relay Status"

CANDIDATE_COMMANDS = [
    ("ON", {}),
    ("OFF", {}),
    ("TOGGLE", {}),
    ("CLOSE", {}),
    ("OPEN", {}),
    ("PULSE", {}),
    ("SET_LEVEL", {"LEVEL": 0}),
    ("SET_PROPERTY", {"PROPERTY": PROPERTY_NAME, "VALUE": "CLOSED"}),
    ("C4:UpdateProperty", {"PROPERTY": PROPERTY_NAME, "VALUE": "CLOSED"}),
]

PROPERTY_WRITE_ATTEMPTS = [
    ("PUT", {"Relay Status": "CLOSED"}),
    ("PUT", [{"name": "Relay Status", "value": "CLOSED"}]),
    ("PUT", {"name": "Relay Status", "value": "CLOSED"}),
    ("POST", {"Relay Status": "CLOSED"}),
    ("POST", [{"name": "Relay Status", "value": "CLOSED"}]),
    ("POST", {"name": "Relay Status", "value": "CLOSED"}),
    ("PATCH", {"Relay Status": "CLOSED"}),
    ("PATCH", [{"name": "Relay Status", "value": "CLOSED"}]),
]


def find_config(explicit=None):
    for path in ([explicit] if explicit else []) + CONFIG_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def dump(obj, limit=2400):
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(obj)
    if len(text) > limit:
        text = text[:limit] + "\n... (已截断)"
    return text.replace("\n", "\n    ")


def brief(text, limit=120):
    text = str(text).replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


async def raw_request(session, ip, token, method, uri, body=None):
    """直接发原始 HTTP 请求，返回 (status, text)。"""
    url = "https://{}{}".format(ip, uri)
    headers = {"Authorization": "Bearer {}".format(token)}
    try:
        async with session.request(method, url, headers=headers, json=body) as resp:
            return resp.status, await resp.text()
    except Exception as exc:  # noqa: BLE001
        return None, "EXC: {}".format(exc)


def looks_ok(status, text):
    if status is None:
        return False
    if status not in (200, 201, 202, 204):
        return False
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return True
    if isinstance(data, dict) and data.get("error"):
        return False
    return True


async def get_properties(session, ip, token, item_id):
    status, text = await raw_request(
        session, ip, token, "GET", "/api/v1/items/{}/properties".format(item_id))
    if status != 200:
        return None, "HTTP {}: {}".format(status, brief(text))
    try:
        return json.loads(text), ""
    except ValueError:
        return None, "非 JSON: {}".format(brief(text))


def extract(props, name):
    if not isinstance(props, list):
        return None
    for p in props:
        if isinstance(p, dict) and p.get("name") == name:
            return p.get("value")
    return None


async def probe(ip, username, password, saved_token="",
                apply_changes=False, relay_id=None):
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

        relays = [it for it in all_items
                  if it.get("proxy") == "****_****_Relay"]
        if not relays:
            print("❌ 未找到 ****_****_Relay 条目")
            return 1

        target = None
        if relay_id:
            target = next((it for it in relays if int(it["id"]) == relay_id), None)
            if target is None:
                print("❌ 指定的 relay-id={} 不属于 ****_****_Relay".format(
                    relay_id))
                return 1
        else:
            target = relays[0]
        tid = int(target["id"])

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[1] 基线（目标 [{}] {}）".format(tid, target.get("name")))
        print("=" * 74)
        props, err = await get_properties(session, ip, token, tid)
        if props is None:
            print("  读取属性失败: {}".format(err))
            return 1
        print("  完整 properties:")
        print("    " + dump(props, 1800))

        baseline = extract(props, PROPERTY_NAME)
        print()
        print("  {} 当前值 = {!r}".format(PROPERTY_NAME, baseline))

        readonly_flag = None
        for p in props if isinstance(props, list) else []:
            if isinstance(p, dict) and p.get("name") == PROPERTY_NAME:
                readonly_flag = p.get("readonly")
        print("  {} readonly = {}".format(PROPERTY_NAME, readonly_flag))

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[A] POST /commands 候选命令试探")
        print("=" * 74)
        if not apply_changes:
            print("  （只读模式，未下发。以下为将要尝试的命令）")
            for command, params in CANDIDATE_COMMANDS:
                print("    {:<20} params={}".format(command, json.dumps(params)))
        else:
            for command, params in CANDIDATE_COMMANDS:
                status, text = await raw_request(
                    session, ip, token, "POST",
                    "/api/v1/items/{}/commands".format(tid),
                    {"async": True, "command": command, "tParams": params},
                )
                mark = "✅ 接受" if looks_ok(status, text) else "❌ 拒绝"
                print("    {:<20} params={:<46} HTTP {:<5} {}".format(
                    command, json.dumps(params), str(status), mark))
                if not looks_ok(status, text):
                    print("        返回: {}".format(brief(text, 110)))

            await asyncio.sleep(3)
            after, _ = await get_properties(session, ip, token, tid)
            now = extract(after, PROPERTY_NAME)
            print()
            print("  命令试探后 {} = {!r}（基线 {!r}）".format(
                PROPERTY_NAME, now, baseline))

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[B] 写属性试探")
        print("=" * 74)
        uri = "/api/v1/items/{}/properties".format(tid)
        if not apply_changes:
            print("  （只读模式，未下发。以下为将要尝试的方法与 body）")
            for method, body in PROPERTY_WRITE_ATTEMPTS:
                print("    {:<6} {} {}".format(
                    method, uri, json.dumps(body, ensure_ascii=False)))
        else:
            for method, body in PROPERTY_WRITE_ATTEMPTS:
                status, text = await raw_request(
                    session, ip, token, method, uri, body)
                ok = looks_ok(status, text)
                print("    {:<6} body={:<52} HTTP {:<5} {}".format(
                    method, json.dumps(body, ensure_ascii=False), str(status),
                    "✅ 接受" if ok else "❌ 拒绝"))
                if not ok:
                    print("        返回: {}".format(brief(text, 110)))

                if ok:
                    await asyncio.sleep(2)
                    after, _ = await get_properties(session, ip, token, tid)
                    now = extract(after, PROPERTY_NAME)
                    print("        → 回读 {} = {!r}".format(PROPERTY_NAME, now))
                    if now != baseline:
                        print("        🎯 属性值已变化，说明该写法可控制该继电器！")
                        break
                    print("        → 值未变化（接口接受但驱动未响应）")

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[C] 调光能力抽查：**** 调光型 的子代理是否有 LIGHT_LEVEL / SET_LEVEL")
        print("=" * 74)
        by_id = {}
        for it in all_items:
            try:
                by_id[int(it.get("id"))] = it
            except (TypeError, ValueError):
                continue

        parent_groups = {}
        for it in all_items:
            if int(it.get("type", 0)) != 7 or it.get("proxy") != "light_v2":
                continue
            parent = by_id.get(int(it.get("parentId") or 0), {})
            pname = str(parent.get("name") or "?")
            parent_groups.setdefault(pname, []).append(it)

        print("  light_v2 代理按其父节点（驱动）分组:")
        for pname, items in sorted(parent_groups.items(), key=lambda x: -len(x[1])):
            print("    {:<24} {} 个".format(pname, len(items)))

        def pick(keyword, n):
            out = []
            for pname, items in parent_groups.items():
                if keyword.lower() in pname.lower():
                    out.extend(items[:n])
            return out

        samples = pick("Dimmer", 6) + pick("Switch", 3)
        if not samples:
            print("  （无可抽样设备）")
        else:
            dimmers = 0
            for it in samples:
                sid = int(it["id"])
                try:
                    vdata = await director.get_item_variables(sid)
                    cdata = await director.get_item_commands(sid)
                except Exception as exc:  # noqa: BLE001
                    print("    [{}] 读取失败: {}".format(sid, brief(exc, 70)))
                    continue
                vnames = [str(v.get("varName")) for v in vdata
                          if isinstance(v, dict)]
                cnames = [str(c.get("command")) for c in cdata
                          if isinstance(c, dict)]
                parent_name = str(by_id.get(int(it.get("parentId") or 0), {})
                                  .get("name") or "?")
                has_level = "LIGHT_LEVEL" in vnames
                has_set = any("LEVEL" in c for c in cnames)
                if has_level or has_set:
                    dimmers += 1
                print("    [{}] {:<16} 驱动={:<16} 变量={} 命令={}{}".format(
                    sid, str(it.get("name"))[:16], parent_name[:16],
                    ",".join(vnames) or "无", ",".join(cnames) or "无",
                    "   ← 可调光" if (has_level and has_set) else ""))
            print()
            if dimmers:
                print("  结论：抽样中发现 {} 个可调光设备，需补充调光支持".format(dimmers))
            else:
                print("  结论：抽样中未发现可调光设备")

        # -------------------------------------------------------------
        print()
        print("=" * 74)
        print("[D] 结论判据")
        print("=" * 74)
        print("  · 若 [A] 与 [B] 全部被拒绝 → 该驱动未向 Director 暴露控制接口，")
        print("    这 22 路只能通过 **** 总线侧（面板 / **** App）控制。")
        print("  · 若 [B] 有写法被接受且属性值随之变化 → 可以用「写属性」方式桥接，")
        print("    状态则以 GET /properties 的 Relay Status 为准。")

        if apply_changes:
            # 尝试恢复
            try:
                cur, _ = await get_properties(session, ip, token, tid)
                now = extract(cur, PROPERTY_NAME)
                if now != baseline:
                    print()
                    print("  尝试恢复 {} 到基线 {!r} ...".format(PROPERTY_NAME, baseline))
                    for method, body in PROPERTY_WRITE_ATTEMPTS[:3]:
                        text_body = json.loads(
                            json.dumps(body).replace("CLOSED", str(baseline)))
                        status, _t = await raw_request(
                            session, ip, token, method, uri, text_body)
                        if looks_ok(status, _t):
                            print("    已用 {} 下发恢复值".format(method))
                            break
            except Exception as exc:  # noqa: BLE001
                print("  恢复失败: {}".format(exc))

        print()
        print("=" * 74)
        print("  探测完成，请把以上输出完整贴回")
        print("=" * 74)
        return 0
    finally:
        await session.close()


def main():
    parser = argparse.ArgumentParser(description="**** 继电器控制通道探测")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--ip")
    parser.add_argument("--user", "-u")
    parser.add_argument("--pass", "-p", dest="password")
    parser.add_argument("--apply", action="store_true",
                        help="真正下发命令与写属性（默认只读）")
    parser.add_argument("--relay-id", type=int, default=None,
                        help="指定试探的继电器 item id（默认第一个）")
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
                           args.apply, args.relay_id))
    sys.exit(ok)


if __name__ == "__main__":
    main()
