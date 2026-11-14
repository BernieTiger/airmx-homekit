"""AIRMX bridge（25888）的异步 HTTP 客户端。

HomeKit 层不直接碰 MQTT 和签名：签名/节流/整包合并都在 bridge 里做，
这里只当"状态读取 + 指令投递"的传输层。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

LOG = logging.getLogger("airmx.hap.client")


class BridgeError(RuntimeError):
    """bridge 调用失败。带 status 便于调用方精确判断，而不是靠字符串匹配。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BridgeClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "BridgeClient":
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._session = aiohttp.ClientSession(timeout=self._timeout, headers=headers)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> Any:
        if self._session is None:
            raise BridgeError("客户端尚未启动")
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(method, url, json=json_body) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise BridgeError(
                        f"{method} {path} -> {resp.status}: {text[:200]}",
                        status=resp.status,
                    )
                if not text:
                    return {}
                return json.loads(text)
        except aiohttp.ClientError as exc:
            raise BridgeError(f"{method} {path} 连接失败: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise BridgeError(f"{method} {path} 超时") from exc

    async def health(self) -> dict:
        return await self._request("GET", "/health")

    async def devices(self) -> list[dict]:
        payload = await self._request("GET", "/devices")
        return list(payload.get("devices") or [])

    async def state(self, device_id: int) -> dict | None:
        """返回 None 表示设备还没有上报过（bridge 回 503 no_state_yet）。"""
        try:
            return await self._request("GET", f"/devices/{device_id}/state")
        except BridgeError as exc:
            # 503 = bridge 还没收到过这台设备的上报，属于正常启动阶段
            if exc.status == 503:
                return None
            raise

    async def control(self, device_id: int, data: dict) -> dict:
        return await self._request("POST", f"/devices/{device_id}/control", data)

    async def settings(self, device_id: int, data: dict) -> dict:
        return await self._request("POST", f"/devices/{device_id}/settings", data)

    async def get_settings(self, device_id: int) -> dict:
        payload = await self._request("GET", f"/devices/{device_id}/settings")
        return dict(payload.get("settings") or {})

    async def refresh(self, device_id: int) -> dict:
        return await self._request("POST", f"/devices/{device_id}/refresh")
