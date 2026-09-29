"""
Control4 账户认证 + Director REST 客户端。

不使用 pyControl4，直接用 aiohttp 实现，避免其版本 / 依赖兼容问题：
  - pyControl4 1.x 内部用同步 `with async_timeout.timeout()`，与
    async_timeout >= 4.0 不兼容（AttributeError: __enter__）
  - pyControl4 2.x 需要 Python 3.10+（内部使用 asyncio.timeout）

对外暴露 C4AccountCompat / C4DirectorCompat 两个类，
接口与 2.x 风格一致，上层代码无需感知实现细节。
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ---- Control4 云端 API 端点（与官方 App 一致）----
AUTHENTICATION_ENDPOINT = "https://apis.control4.com/authentication/v1/rest"
CONTROLLER_AUTHORIZATION_ENDPOINT = (
    "https://apis.control4.com/authentication/v1/rest/authorization"
)
GET_CONTROLLERS_ENDPOINT = "https://apis.control4.com/account/v3/rest/accounts"
APPLICATION_KEY = "78f6791373d61bea49fdb9fb8897f1f3af193f11"

DEFAULT_TIMEOUT = 20


def _check_response_for_error(text: str) -> None:
    """Control4 出错时返回 JSON 里带 error 字段，这里统一抛异常。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("Control4 API 错误: {}".format(data["error"]))


def _json(data: Any) -> Any:
    """把响应（字符串 / bytes / 已解析对象）统一转成 Python 对象。"""
    if isinstance(data, (list, dict)):
        return data
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return data
    return data


class C4AccountCompat:
    """Control4 云端账户认证。

    流程：
      1. POST /authentication/v1/rest        -> 拿账户 token
      2. GET  /account/v3/rest/accounts      -> 拿控制器列表
      3. POST /authentication/v1/rest/authorization -> 拿 Director token
    """

    api_flavor = "direct-rest (aiohttp)"

    def __init__(self, username: str, password: str, session: Any = None):
        self.username = username
        self.password = password
        self._session = session
        self.account_bearer_token: Optional[str] = None

    @asynccontextmanager
    async def _session_ctx(self):
        """云端接口是正式证书，保持 SSL 校验。"""
        if self._session is not None:
            yield self._session
        else:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
            ) as session:
                yield session

    async def authenticate(self) -> str:
        """登录并获取账户级 token。"""
        payload = {
            "clientInfo": {
                "device": {
                    "deviceName": "c4-ha-bridge",
                    "deviceUUID": "0000000000000000",
                    "make": "c4-ha-bridge",
                    "model": "c4-ha-bridge",
                    "os": "Android",
                    "osVersion": "10",
                },
                "userInfo": {
                    "applicationKey": APPLICATION_KEY,
                    "password": self.password,
                    "userName": self.username,
                },
            }
        }

        async with self._session_ctx() as session:
            async with session.post(
                AUTHENTICATION_ENDPOINT, json=payload
            ) as resp:
                text = await resp.text()

        _check_response_for_error(text)
        data = _json(text)
        token = None
        if isinstance(data, dict):
            token = (data.get("authToken") or {}).get("token")

        if not token:
            raise RuntimeError(
                "账户认证失败：未获取到 token。请检查用户名/密码是否正确。"
            )

        self.account_bearer_token = token
        return token

    async def get_controllers(self) -> List[Dict[str, Any]]:
        """返回统一的控制器列表：common_name / uuid / name / model / raw"""
        if not self.account_bearer_token:
            raise RuntimeError("尚未认证，请先调用 authenticate()")

        headers = {"Authorization": "Bearer {}".format(self.account_bearer_token)}
        async with self._session_ctx() as session:
            async with session.get(GET_CONTROLLERS_ENDPOINT, headers=headers) as resp:
                text = await resp.text()

        _check_response_for_error(text)
        data = _json(text)

        # 实际返回可能是 {"account": {...}} 或 {"account": [{...}]}
        raw = data.get("account") if isinstance(data, dict) else data
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []

        controllers: List[Dict[str, Any]] = []
        for ctrl in raw:
            if not isinstance(ctrl, dict):
                continue
            common_name = (
                ctrl.get("controllerCommonName")
                or ctrl.get("commonName")
                or ctrl.get("controller_common_name")
            )
            controllers.append(
                {
                    "common_name": common_name,
                    "uuid": ctrl.get("uuid") or common_name,
                    "name": ctrl.get("name") or common_name or "unknown",
                    "model": ctrl.get("model", ""),
                    "raw": ctrl,
                }
            )
        return controllers

    async def get_director_token(self, controller: Dict[str, Any]) -> str:
        """用控制器 commonName 换取 Director 本地访问 token。"""
        if not self.account_bearer_token:
            raise RuntimeError("尚未认证，请先调用 authenticate()")

        raw = controller.get("raw") or {}
        common_name = (
            raw.get("controllerCommonName")
            or controller.get("common_name")
            or controller.get("uuid")
        )
        if not common_name:
            raise RuntimeError("控制器缺少 commonName，无法获取 Director Token")

        payload = {
            "serviceInfo": {
                "commonName": common_name,
                "services": "director",
            }
        }
        headers = {"Authorization": "Bearer {}".format(self.account_bearer_token)}

        async with self._session_ctx() as session:
            async with session.post(
                CONTROLLER_AUTHORIZATION_ENDPOINT, headers=headers, json=payload
            ) as resp:
                text = await resp.text()

        _check_response_for_error(text)
        data = _json(text)
        token = None
        if isinstance(data, dict):
            token = (data.get("authToken") or {}).get("token")

        if not token:
            raise RuntimeError("未获取到 Director Token")
        return token


class C4DirectorCompat:
    """Control4 控制器本地 REST 客户端（自签名证书，关闭校验）。"""

    api_flavor = "direct-rest (aiohttp)"

    def __init__(self, ip: str, token: str, session: Any = None):
        self.ip = ip
        self.director_bearer_token = token
        self.base_url = "https://{}".format(ip)
        self.headers = {"Authorization": "Bearer {}".format(token)}
        self._session = session

    @asynccontextmanager
    async def _session_ctx(self):
        if self._session is not None:
            yield self._session
        else:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as session:
                yield session

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    async def send_get_request(self, uri: str) -> str:
        async with self._session_ctx() as session:
            async with session.get(self.base_url + uri, headers=self.headers) as resp:
                text = await resp.text()
        _check_response_for_error(text)
        return text

    async def send_post_request(
        self, uri: str, command: str, params: Dict[str, Any], is_async: bool = True
    ) -> str:
        body = {"async": is_async, "command": command, "tParams": params}
        async with self._session_ctx() as session:
            async with session.post(
                self.base_url + uri, headers=self.headers, json=body
            ) as resp:
                text = await resp.text()
        _check_response_for_error(text)
        return text

    # ------------------------------------------------------------------
    # 设备信息
    # ------------------------------------------------------------------
    async def get_all_item_info(self) -> List[Dict[str, Any]]:
        data = _json(await self.send_get_request("/api/v1/items"))
        return data if isinstance(data, list) else []

    async def get_item_info(self, item_id: int) -> List[Dict[str, Any]]:
        data = _json(
            await self.send_get_request("/api/v1/items/{}".format(item_id))
        )
        return data if isinstance(data, list) else []

    async def get_item_variables(self, item_id: int) -> List[Dict[str, Any]]:
        data = _json(
            await self.send_get_request(
                "/api/v1/items/{}/variables".format(item_id)
            )
        )
        return data if isinstance(data, list) else []

    async def get_item_commands(self, item_id: int) -> List[Dict[str, Any]]:
        data = _json(
            await self.send_get_request(
                "/api/v1/items/{}/commands".format(item_id)
            )
        )
        return data if isinstance(data, list) else []

    async def get_item_variable_value(self, item_id: int, var_name: Any) -> Any:
        """读取指定变量，空值与 'Undefined' 统一返回 None。"""
        if isinstance(var_name, (tuple, list, set)):
            var_name = ",".join(var_name)

        data = _json(
            await self.send_get_request(
                "/api/v1/items/{}/variables?varnames={}".format(item_id, var_name)
            )
        )
        if not isinstance(data, list) or not data:
            raise ValueError(
                "变量 {} 在设备 {} 上不存在（返回为空）".format(var_name, item_id)
            )
        value = data[0].get("value")
        if value is None or value == "Undefined":
            return None
        return value

    async def get_all_item_variable_value(self, var_name: Any) -> List[Dict[str, Any]]:
        if isinstance(var_name, (tuple, list, set)):
            var_name = ",".join(var_name)

        data = _json(
            await self.send_get_request(
                "/api/v1/items/variables?varnames={}".format(var_name)
            )
        )
        if not isinstance(data, list):
            return []
        for item in data:
            if isinstance(item, dict) and item.get("value") == "Undefined":
                item["value"] = None
        return data

    async def get_item_variables_map(self, item_id: int) -> Dict[str, Any]:
        """读取单个设备的全部变量，返回 {varName: value}。"""
        data = await self.get_item_variables(item_id)
        out: Dict[str, Any] = {}
        for v in data:
            if isinstance(v, dict) and v.get("varName"):
                out[str(v["varName"])] = v.get("value")
        return out

    async def get_all_items_variables(self, varnames: Any) -> Dict[int, Dict[str, Any]]:
        """批量读取：一次请求取回全系统上这些变量的值。

        对应 /api/v1/items/variables?varnames=A,B（注意没有 item id 段），
        返回 {item_id: {varName: value}}。一次请求即可刷新所有设备状态。
        """
        if isinstance(varnames, (tuple, list, set)):
            varnames = ",".join(varnames)
        if not varnames:
            return {}

        data = _json(
            await self.send_get_request(
                "/api/v1/items/variables?varnames={}".format(varnames)
            )
        )
        out: Dict[int, Dict[str, Any]] = {}
        if not isinstance(data, list):
            return out

        for entry in data:
            if not isinstance(entry, dict):
                continue
            iid = entry.get("id")
            name = entry.get("varName")
            if iid is None or not name:
                continue
            try:
                iid = int(iid)
            except (TypeError, ValueError):
                continue
            value = entry.get("value")
            if value == "Undefined":
                value = None
            out.setdefault(iid, {})[str(name)] = value
        return out

    async def send_command(
        self, item_id: int, command: str, params: Optional[Dict[str, Any]] = None
    ) -> str:
        """便捷方法：向指定设备发送命令。"""
        return await self.send_post_request(
            "/api/v1/items/{}/commands".format(item_id),
            command,
            params or {},
        )
