"""Separate device HTTP and authenticated management HTTP applications."""
import hmac
import json
import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from .protocol import IntakeError, registration_response, strict_json
from .store import Store

LOG = logging.getLogger("airmx_gateway")
STORE = web.AppKey("store", Store)
CONFIG = web.AppKey("config", object)


@dataclass(frozen=True)
class Config:
    admin_token: str
    device_allowed_cidrs: tuple[str, ...] = ()
    bind_host: str = "127.0.0.1"
    device_port: int = 8080
    admin_port: int = 8086
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883

    def __post_init__(self):
        if len(self.admin_token) < 32 or not self.admin_token.isascii() or not self.admin_token.isprintable():
            raise ValueError("management_token_invalid")
        for network in self.device_allowed_cidrs:
            ipaddress.ip_network(network)

    def device_peer_allowed(self, value: str | None):
        try:
            address = ipaddress.ip_address(value)
        except (TypeError, ValueError):
            return False
        return any(address in ipaddress.ip_network(net) for net in self.device_allowed_cidrs)


@web.middleware
async def device_trace(request, handler):
    """2026-11-13 诊断用：只记录方法+路径+path 参数，不记录 params/sig/key。"""
    try:
        return await handler(request)
    finally:
        try:
            qpath = request.query.get("path", "")
            LOG.info("device_request %s %s qpath=%s", request.method, request.path, qpath)
        except Exception:
            LOG.info("device_request %s %s", request.method, request.path)


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except IntakeError as exc:
        status = 403 if exc.code in {"enrollment_closed", "peer_not_allowed", "device_not_allowed"} else 400
        return web.json_response({"error": exc.code}, status=status)
    except web.HTTPException as exc:
        return web.json_response({"error": "request_rejected", "status": exc.status}, status=exc.status)
    except Exception:
        # Query strings / keys / exception details must never reach logs.
        LOG.error("request_failed")
        return web.json_response({"error": "internal_error"}, status=500)


@web.middleware
async def no_cache(request, handler):
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@web.middleware
async def authenticate(request, handler):
    if request.path != "/healthz":
        provided = request.headers.get("Authorization", "")
        wanted = "Bearer " + request.app[CONFIG].admin_token
        if not provided.isascii() or len(provided) > 512 or not hmac.compare_digest(provided, wanted):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def health(request):
    return web.json_response({"service": "airmx-homekit-gateway", "version": "0.1.0", "control_enabled": False})


async def diagnostic(request):
    store = request.app[STORE]
    return web.json_response({"broker_connected": store.broker_connected,
                             "enrollment": store.window_status(), "counters": dict(store.counters),
                             "control_enabled": False,
                             "status_note": "online requires a fresh verified report, not just broker reachability"})


async def devices(request):
    return web.json_response({"devices": request.app[STORE].devices(), "control_enabled": False})


async def open_enrollment(request):
    body = strict_json(await request.read())
    if set(body) != {"devices", "duration_seconds"}:
        raise IntakeError("invalid_enrollment_request")
    result = request.app[STORE].open_window(body["devices"], body["duration_seconds"])
    return web.json_response(result)


async def close_enrollment(request):
    request.app[STORE].close_window()
    return web.json_response({"open": False})


async def delete_device(request):
    device_id = request.match_info.get("device_id", "")
    try:
        device_id = int(device_id)
    except (TypeError, ValueError):
        raise IntakeError("invalid_device_id") from None
    result = request.app[STORE].delete_device(device_id)
    return web.json_response({"ok": True, **result})


async def device_key(request):
    """诊断用途：返回设备签名密钥，供局域网内桥接服务使用。"""
    try:
        device_id = int(request.match_info.get("device_id", ""))
    except (TypeError, ValueError):
        raise IntakeError("invalid_device_id") from None
    key = request.app[STORE].device_key(device_id)
    return web.json_response({"device_id": device_id, "key": key})


async def handle_pairing(request):
    """一键配对：无需预先知道 MAC，按型号放开窗口。"""
    body = strict_json(await request.read())
    models = body.get("models", [11, 20])
    duration = body.get("duration_seconds", 600)
    if not isinstance(models, list) or not models:
        raise IntakeError("invalid_enrollment_request")
    entries = [{"wifi_mac": "*", "model_type": m} for m in models]
    result = request.app[STORE].open_window(entries, duration)
    return web.json_response({
        **result,
        "mode": "pairing",
        "hint": "现在给设备断电重启，它会自行注册；成功后 GET /api/v1/devices 可见",
    })


async def no_control(request):
    return web.json_response({"error": "control_disabled_in_diagnostic_build"}, status=403)


async def device_root(request):
    return web.Response(text="AIRMX diagnostic gateway\n")


async def device_time(request):
    if not request.app[CONFIG].device_peer_allowed(request.remote):
        raise IntakeError("peer_not_allowed")
    return web.json_response({"time": int(request.app[STORE].clock())})


async def register(request):
    if not request.app[CONFIG].device_peer_allowed(request.remote):
        raise IntakeError("peer_not_allowed")
    if request.query.get("path") != "aw/GET/genId":
        raise IntakeError("unsupported_registration_path")
    if len(request.query.getall("params", [])) != 1 or len(request.query.getall("path", [])) != 1:
        raise IntakeError("duplicate_field")
    # Registration includes a device key. Access logging is disabled in the runner.
    params = strict_json(request.query.get("params", ""))
    identity = request.app[STORE].register(params, request.remote)
    # 固件对 JSON 文本做的是字符串匹配，标准 json_response 会在冒号/逗号后加空格
    # （{"status": 200, "data": 10001}），导致匹配失败并无限重试 genId。
    # 这里手动返回紧凑 JSON，与真机验证过的格式完全一致。
    body = json.dumps(registration_response(*identity), separators=(",", ":")).encode()
    return web.Response(body=body, content_type="application/json")


async def unsupported_wash(request):
    # Do not acknowledge a maintenance workflow that we have not implemented.
    return web.json_response({"status": 501, "error": "maintenance_not_implemented"}, status=501)


def make_admin_app(store: Store, config: Config):
    app = web.Application(client_max_size=16384, middlewares=[no_cache, errors, authenticate])
    app[STORE] = store
    app[CONFIG] = config
    app.add_routes([web.get("/healthz", health), web.get("/api/v1/diagnostics", diagnostic),
                    web.get("/api/v1/devices", devices), web.post("/api/v1/enrollment", open_enrollment),
                    web.delete("/api/v1/enrollment", close_enrollment),
                    web.post("/api/v1/pairing", handle_pairing),
                    web.delete("/api/v1/devices/{device_id}", delete_device),
                    web.get("/api/v1/devices/{device_id}/key", device_key),
                    web.post("/api/v1/devices/{device_id}/commands", no_control)])
    return app


def make_device_app(store: Store, config: Config):
    app = web.Application(client_max_size=16384, middlewares=[device_trace, no_cache, errors])
    app[STORE] = store
    app[CONFIG] = config
    app.add_routes([web.get("/healthz", health), web.get("/", device_root),
                    web.get("/gettime", device_time), web.get("/aw", register),
                    web.get("/check/airwater/washCleanNotify", unsupported_wash)])
    return app
