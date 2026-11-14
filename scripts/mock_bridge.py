#!/usr/bin/env python3
"""假 bridge：在没有设备、没有 NAS 的情况下预演 HomeKit 全链路。

用途：
  1. 先在本机确认"家庭 App 能配对、能开关"，再去动 NAS。
  2. 复现某些边界（离线、无状态、低水位、故障态）。

用法：
    python scripts/mock_bridge.py                 # 正常在线设备
    python scripts/mock_bridge.py --offline       # 模拟设备离线
    python scripts/mock_bridge.py --no-state      # 模拟从未上报
    python scripts/mock_bridge.py --model 11      # 模拟 A5（风量 0-7）

然后另开一个终端：
    AIRMX_BRIDGE_URL=http://127.0.0.1:25888 python -m hap
"""
from __future__ import annotations

import argparse
import asyncio
import copy

from aiohttp import web

DEVICE_ID = 10001

BASE_STATE = {
    "power": True, "mode": 0, "fan_speed": 40, "target_humidity": 45,
    "humidity": 56.09, "temperature": 21.95, "water_level": 45,
    "child_lock": False, "anion": False, "uv": True, "need_clean": False,
    "external_sensor_online": False, "wifi_signal": -60, "firmware": "00.01.20",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=25888)
    ap.add_argument("--model", type=int, default=20)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-state", action="store_true")
    ap.add_argument("--no-key", action="store_true")
    ap.add_argument("--stale", type=int, default=0, help="让 /state 报告 N 秒前的旧数据")
    args = ap.parse_args()

    state = copy.deepcopy(BASE_STATE)
    published: list[dict] = []

    app = web.Application()

    async def health(_):
        return web.json_response({"mqtt_connected": not args.offline, "device_count": 1,
                                  "primary_id": DEVICE_ID})

    async def devices(_):
        return web.json_response({"ok": True, "devices": [{
            "device_id": DEVICE_ID, "model_type": args.model,
            "online": not args.offline, "has_key": not args.no_key,
        }]})

    async def get_state(_):
        if args.no_state:
            return web.json_response({"ok": False, "error": "no_state_yet"}, status=503)
        return web.json_response({"ok": True, "device_id": DEVICE_ID,
                                  "age_seconds": args.stale, "state": state})

    async def control(request):
        body = await request.json()
        published.append(body)
        for src, dst in (("power", "power"), ("mode", "mode"), ("cadr", "fan_speed"),
                         ("lock", "child_lock"), ("anion", "anion")):
            if src in body:
                state[dst] = bool(body[src]) if src in ("power", "lock", "anion") else body[src]
        print(f"[mock] CONTROL {body} -> {state}")
        return web.json_response({"ok": True, "detail": "accepted"})

    async def settings(request):
        body = await request.json()
        published.append(body)
        if "hThreshold" in body:
            state["target_humidity"] = body["hThreshold"]
        if "cleanNotify" in body:
            pass
        print(f"[mock] SET {body}")
        return web.json_response({"ok": True, "effective_settings": body})

    async def get_settings(_):
        return web.json_response({"ok": True, "settings": {"hThreshold": state["target_humidity"],
                                                          "water_type": 2}})

    async def refresh(_):
        published.append({"refresh": True})
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    app.router.add_get("/devices", devices)
    app.router.add_get("/devices/{id}/state", get_state)
    app.router.add_post("/devices/{id}/control", control)
    app.router.add_get("/devices/{id}/settings", get_settings)
    app.router.add_post("/devices/{id}/settings", settings)
    app.router.add_post("/devices/{id}/refresh", refresh)

    print(f"[mock] listening 127.0.0.1:{args.port} model={args.model} "
          f"offline={args.offline} no_state={args.no_state}")
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
