#!/usr/bin/env python3
"""AIRMX AirWater 桥接服务（多设备版）

把加湿器的 MQTT 协议封装成 HTTP/JSON 接口，供 iOS App / 快捷指令调用。
支持多台设备（A3S / A5 等），签名密钥自动从网关获取。

接口：
  GET  /health                          健康检查
  GET  /devices                         设备列表
  GET  /devices/{id}/state              设备状态（换算后）
  POST /devices/{id}/control            控制 {"power":1,"mode":0,"cadr":40}
  GET  /devices/{id}/settings           当前设置
  POST /devices/{id}/settings           修改设置 {"hThreshold":50}
  POST /devices/{id}/refresh            立即索取状态

  兼容短接口（作用于主设备）：/state /control /settings /refresh

协议要点（逆向自 dext0r/airmx，已真机验证）：
  上行 airwater/01/0/1/1/<id>    下行 airwater/01/1/0/1/<id>
  sig = md5(compact_json({"cmdId","time","data"})[1:-1] + "," + sign_key)
  1000 控制 / 1001 状态上报 / 1002 设置 / 1003 设置回执 / 1008 查询
  设备是被动应答型：必须由服务端发 GET_STATUS 才会回报
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt
from aiohttp import web

LOG = logging.getLogger("bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MQTT_HOST = os.environ.get("AIRMX_MQTT_HOST", "mqtt")
MQTT_PORT = int(os.environ.get("AIRMX_MQTT_PORT", "1883"))
GATEWAY_URL = os.environ.get("AIRMX_GATEWAY_URL", "http://gateway:8086").rstrip("/")
TOKEN_FILE = os.environ.get("AIRMX_TOKEN_FILE", "/run/secrets/admin_token")
FALLBACK_KEY = os.environ.get("BRIDGE_SIGN_KEY", "").strip()
HTTP_PORT = int(os.environ.get("BRIDGE_PORT", "8088"))
STATE_FILE = os.environ.get("BRIDGE_STATE_FILE", "/data/bridge_state.json")
# 手动密钥覆盖（设备重置后会换密钥，而网关只在设备注册时才更新）
KEYS_FILE = os.environ.get("BRIDGE_KEYS_FILE", "/app/bridge/keys.json")
POLL_SECONDS = int(os.environ.get("BRIDGE_POLL_SECONDS", "60"))
KEY_REFRESH_SECONDS = int(os.environ.get("BRIDGE_KEY_REFRESH_SECONDS", "60"))
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=6)

CMD_CONTROL = 1000
CMD_STATUS_INFO = 1001
CMD_SET = 1002
CMD_SET_INFO = 1003
CMD_GET_STATUS = 1008

UP_PREFIX = "airwater/01/0/1/1/"

# SET 是全量覆盖语义：设备会把这 22 个字段中未传的项置 0，因此必须整包下发
SETTINGS_FIELDS = (
    "hThreshold", "water_type", "powerHeat", "strongModeThreshold",
    "autoShakeEnable", "pirLock",
    "fanTimer", "fanStart", "fanStop", "fanRepeat",
    "silentTimer", "silentStart", "silentStop", "silentRepeat",
    "clearTimer", "clearStart", "clearStop", "clearRepeat",
    "reportInterval", "logLevel", "pumpInstantOff", "cleanNotify",
)

DEFAULT_SETTINGS = {
    "hThreshold": 45, "water_type": 2,
    "powerHeat": 0, "strongModeThreshold": 0,
    "autoShakeEnable": 0, "pirLock": 1,
    "fanTimer": 0, "fanStart": 0, "fanStop": 0, "fanRepeat": 0,
    "silentTimer": 0, "silentStart": 0, "silentStop": 0, "silentRepeat": 0,
    "clearTimer": 0, "clearStart": 0, "clearStop": 0, "clearRepeat": 0,
    "reportInterval": 0, "logLevel": 0, "pumpInstantOff": 0, "cleanNotify": 1,
}

# 实测设备对两次 SET 有约 10 秒节流，间隔过短会被静默丢弃（连 1003 回执都没有）
SET_MIN_INTERVAL = 11.0
_MISSING = 99999


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def sign_command(cmd_id: int, data: dict, key: str) -> bytes:
    cmd = {"cmdId": cmd_id, "time": int(time.time()), "data": data}
    to_sign = _dump(cmd)[1:-1]
    cmd["sig"] = hashlib.md5(f"{to_sign},{key}".encode()).hexdigest()
    return _dump(cmd).encode()


def _num(value: Any, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def decode_status(raw: dict) -> dict:
    def scaled(key: str):
        v = _num(raw.get(key))
        if v is None or v == _MISSING or v <= 0:
            return None
        return round(v / 100, 2)

    mode = _num(raw.get("mode"), 0)
    return {
        "power": bool(_num(raw.get("power"), 0)),
        "mode": mode,
        "mode_label": {0: "manual", 1: "auto", 2: "sleep", 5: "malfunction"}.get(mode, "unknown"),
        "fan_speed": _num(raw.get("cadr"), 0),
        "target_humidity": _num(raw.get("hThreshold"), 0),
        "humidity": scaled("h0"),
        "temperature": scaled("t0"),
        "external_humidity": scaled("h"),
        "external_temperature": scaled("t"),
        "water_level": _num(raw.get("water"), 0),
        "child_lock": bool(_num(raw.get("lock"), 0)),
        "uv": bool(_num(raw.get("uv"), 0)),
        "anion": bool(_num(raw.get("anion"), 0)),
        "heater": bool(_num(raw.get("powerHeatStatus"), 0)),
        "need_clean": bool(_num(raw.get("isNeedClean"), 0)),
        "external_sensor_online": bool(_num(raw.get("gooseOnline"), 0)),
        "wifi_signal": _num(raw.get("wifiSignal"), 0),
        "firmware": raw.get("version"),
        "raw": raw,
    }


@dataclass
class DeviceState:
    device_id: int
    state: dict = field(default_factory=dict)
    state_at: float = 0.0
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    pending: dict = field(default_factory=dict)
    sent_at: float = 0.0
    task: asyncio.Task | None = None
    key: str = ""
    model_type: int | None = None
    last_error: str = ""

    @property
    def age(self) -> float | None:
        return round(time.time() - self.state_at, 1) if self.state_at else None

    @property
    def online(self) -> bool:
        return bool(self.state_at) and (time.time() - self.state_at) < max(180, POLL_SECONDS * 3)


class Bridge:
    def __init__(self) -> None:
        self.devices: dict[int, DeviceState] = {}
        self.primary_id: int | None = None
        self.connected = False
        self._client: mqtt.Client | None = None
        self._token: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------- 密钥 ----------
    def _apply_keys_file(self) -> int:
        """从挂载的 keys.json 读取手动指定的密钥（支持热更新）。

        设备重置后会生成新密钥，但它不会重新注册，网关里的密钥因此过期。
        这个文件用于手工覆盖，格式：{"0": "a4df...", "12345": "..."}
        """
        applied = 0
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return 0
        except Exception as exc:
            LOG.warning("读取 %s 失败: %s", KEYS_FILE, exc)
            return 0
        for raw_id, key in (data or {}).items():
            try:
                device_id = int(raw_id)
            except ValueError:
                continue
            if not isinstance(key, str) or not key:
                continue
            dev = self.devices.setdefault(device_id, DeviceState(device_id))
            if dev.key != key:
                LOG.info("密钥文件覆盖：设备 %s 使用指定密钥", device_id)
            dev.key = key
            applied += 1
        return applied


    def _load_token(self) -> str:
        if self._token:
            return self._token
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
                self._token = fh.read().strip()
        except Exception as exc:
            LOG.warning("读取管理令牌失败(%s)，将退回 BRIDGE_SIGN_KEY", exc)
        return self._token

    async def refresh_keys(self) -> None:
        """从网关拉取设备列表与各自的注册密钥。"""
        token = self._load_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=headers) as session:
                async with session.get(f"{GATEWAY_URL}/api/v1/devices") as resp:
                    payload = await resp.json()
                for item in payload.get("devices", []):
                    device_id = item.get("device_id")
                    model = item.get("model_type")
                    if not isinstance(device_id, int):
                        continue
                    dev = self.devices.setdefault(device_id, DeviceState(device_id))
                    dev.model_type = model
                    async with session.get(f"{GATEWAY_URL}/api/v1/devices/{device_id}/key") as kresp:
                        if kresp.status != 200:
                            LOG.warning("取设备 %s 密钥失败: %s", device_id, kresp.status)
                            continue
                        key = (await kresp.json()).get("key")
                    if key:
                        if dev.key != key:
                            LOG.info("同步设备 %s 的签名密钥（型号 %s）", device_id, model)
                        dev.key = key
                known_ids = {item.get("device_id") for item in payload.get("devices", [])}
                for stale in [d for d, dev in self.devices.items()
                              if d not in known_ids and not dev.state]:
                    LOG.info("移除已注销设备 %s", stale)
                    self.devices.pop(stale, None)
                if self.primary_id not in self.devices:
                    with_state = [d for d, dev in self.devices.items() if dev.state]
                    self.primary_id = (with_state or sorted(self.devices) or [None])[0]
        except Exception as exc:
            LOG.warning("从网关获取密钥失败: %s", exc)
        # 手动密钥文件优先级最高
        self._apply_keys_file()
        # 兜底：环境变量里配置的单设备密钥
        if FALLBACK_KEY:
            for dev in self.devices.values():
                dev.key = dev.key or FALLBACK_KEY

    async def _ensure_device(self, device_id: int) -> DeviceState:
        dev = self.devices.get(device_id)
        if dev is None:
            dev = DeviceState(device_id)
            self.devices[device_id] = dev
            await self.refresh_keys()
            dev = self.devices.get(device_id, dev)
        return dev

    # ---------- MQTT ----------
    def start(self) -> None:
        client = mqtt.Client(
            client_id="airmx-bridge",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        self._client = client
        LOG.info("连接 MQTT %s:%s", MQTT_HOST, MQTT_PORT)
        client.connect_async(MQTT_HOST, MQTT_PORT, 30)
        client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self.connected = True
        LOG.info("MQTT 已连接 (%s)", rc)
        client.subscribe(UP_PREFIX + "+", qos=1)
        # MQTT 回调在独立线程里，必须切回主事件循环
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._on_ready()))

    async def _on_ready(self) -> None:
        await self.refresh_keys()
        for device_id in list(self.devices):
            self.request_status(device_id)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self.connected = False
        LOG.warning("MQTT 断开 (%s)", rc)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        topic = msg.topic
        if not topic.startswith(UP_PREFIX):
            return
        try:
            device_id = int(topic.rsplit("/", 1)[-1])
        except ValueError:
            return
        dev = self.devices.setdefault(device_id, DeviceState(device_id))
        try:
            report = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            LOG.warning("无法解析上报: %r", msg.payload[:200])
            return
        cmd_id = report.get("cmdId")
        data = report.get("data") or {}
        if cmd_id == CMD_STATUS_INFO:
            dev.state = decode_status(data)
            dev.state_at = time.time()
            dev.last_error = ""
            if self.primary_id is None:
                self.primary_id = device_id
            LOG.info(
                "状态 id=%s power=%s mode=%s cadr=%s h=%s t=%s water=%s",
                device_id, dev.state.get("power"), dev.state.get("mode_label"),
                dev.state.get("fan_speed"), dev.state.get("humidity"),
                dev.state.get("temperature"), dev.state.get("water_level"),
            )
            self._persist()
        elif cmd_id == CMD_SET_INFO:
            for key in SETTINGS_FIELDS:
                if key in data:
                    dev.settings[key] = data[key]
            LOG.info("设置回执 id=%s hThreshold=%s pirLock=%s",
                     device_id, dev.settings.get("hThreshold"), dev.settings.get("pirLock"))
            self._persist()
        else:
            LOG.debug("id=%s cmdId=%s", device_id, cmd_id)

    # ---------- 持久化 ----------
    def _persist(self) -> None:
        try:
            payload = {
                "primary": self.primary_id,
                "devices": {
                    str(d): {
                        "state": dev.state, "at": dev.state_at,
                        "settings": dev.settings, "model": dev.model_type,
                    }
                    for d, dev in self.devices.items()
                },
            }
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except Exception as exc:
            LOG.debug("写状态缓存失败: %s", exc)

    def _load_persisted(self) -> None:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:
            LOG.warning("读状态缓存失败: %s", exc)
            return
        self.primary_id = payload.get("primary")
        for raw_id, item in (payload.get("devices") or {}).items():
            try:
                device_id = int(raw_id)
            except ValueError:
                continue
            dev = self.devices.setdefault(device_id, DeviceState(device_id))
            dev.state = item.get("state") or {}
            dev.state_at = float(item.get("at") or 0)
            dev.model_type = item.get("model")
            saved = item.get("settings") or {}
            for key in SETTINGS_FIELDS:
                if key in saved:
                    dev.settings[key] = saved[key]
        if self.devices:
            LOG.info("已载入缓存：%s 台设备，主设备 %s", len(self.devices), self.primary_id)

    # ---------- 指令 ----------
    def _publish(self, device_id: int, cmd_id: int, data: dict) -> tuple[bool, str]:
        self._apply_keys_file()
        dev = self.devices.get(device_id)
        if dev is None:
            return False, f"未知设备 {device_id}"
        if not dev.key:
            # 不再"借用"其他设备的密钥：多设备场景下会串号，
            # 且借用后 dev.key 被污染，refresh_keys 因"已有密钥"而不再纠正。
            return False, f"设备 {device_id} 缺少签名密钥"
        if not self._client or not self.connected:
            return False, "MQTT 未连接"
        topic = f"airwater/01/1/0/1/{device_id}"
        payload = sign_command(cmd_id, data, dev.key)
        info = self._client.publish(topic, payload, qos=1)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        LOG.info("下发 id=%s %s -> %s", device_id, payload.decode(), "成功" if ok else f"失败({info.rc})")
        return ok, payload.decode()

    def request_status(self, device_id: int) -> None:
        self._publish(
            device_id, CMD_GET_STATUS,
            {"cleanTime": 150, "water_type": 2, "frequencyTime": POLL_SECONDS, "durationTime": 10},
        )

    def control(self, device_id: int, data: dict) -> tuple[bool, str]:
        dev = self.devices.get(device_id)
        if dev is None:
            return False, f"未知设备 {device_id}"
        # CONTROL 同样是整包语义：缺失字段从当前状态回填，避免误关机
        merged = {
            "power": int(bool(dev.state.get("power", False))),
            "mode": _num(dev.state.get("mode"), 0),
            "cadr": _num(dev.state.get("fan_speed"), 0),
            "lock": int(bool(dev.state.get("child_lock", False))),
            "uv": int(bool(dev.state.get("uv", True))),
            "anion": int(bool(dev.state.get("anion", True))),
        }
        for key in ("power", "mode", "cadr", "lock", "uv", "anion"):
            if key in data:
                value = data[key]
                merged[key] = int(bool(value)) if key in ("power", "lock", "uv", "anion") else _num(value, merged[key])
        return self._publish(device_id, CMD_CONTROL, merged)

    def queue_settings(self, device_id: int, data: dict) -> tuple[bool, str]:
        dev = self.devices.get(device_id)
        if dev is None:
            return False, f"未知设备 {device_id}"
        changed = {}
        for key, value in data.items():
            if key in SETTINGS_FIELDS:
                changed[key] = value
            else:
                LOG.warning("忽略未知设置字段 %s", key)
        if not changed:
            return False, "没有可下发的设置字段"
        dev.pending.update(changed)
        if not dev.task or dev.task.done():
            dev.task = asyncio.create_task(self._flush_settings(device_id))
        return True, f"已受理，将合并下发 {sorted(changed)}"

    async def _flush_settings(self, device_id: int) -> None:
        dev = self.devices.get(device_id)
        if dev is None:
            return
        wait = SET_MIN_INTERVAL - (time.time() - dev.sent_at)
        if wait > 0:
            await asyncio.sleep(wait)
        payload = dict(dev.settings)
        payload.update(dev.pending)
        payload["water_type"] = payload.get("water_type", 2)
        dev.pending.clear()
        ok, _ = self._publish(device_id, CMD_SET, payload)
        if ok:
            dev.sent_at = time.time()
            dev.settings.update(payload)

    # ---------- 轮询 ----------
    async def poll_loop(self) -> None:
        last_keys = 0.0
        while True:
            await asyncio.sleep(max(10, min(POLL_SECONDS, 30)))
            if time.time() - last_keys > KEY_REFRESH_SECONDS:
                last_keys = time.time()
                await self.refresh_keys()
            if self.connected:
                for device_id in list(self.devices):
                    self.request_status(device_id)


BRIDGE = Bridge()

API_TOKEN = os.environ.get("BRIDGE_API_TOKEN", "").strip()


@web.middleware
async def require_token(request: web.Request, handler):
    """除 /health 外一律要求 Bearer 令牌。用于外网暴露时的最小防线。"""
    if request.path == "/health" or request.method == "OPTIONS":
        return await handler(request)
    if not API_TOKEN:
        return await handler(request)  # 未配置令牌时保持局域网内可用
    supplied = request.headers.get("Authorization", "")
    if supplied.startswith("Bearer "):
        supplied = supplied[7:].strip()
    if not supplied:
        supplied = request.headers.get("X-API-Key", "").strip()
    if not secrets.compare_digest(supplied, API_TOKEN):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)




# ---------- HTTP ----------
def _dev_or_404(device_id: int | None) -> DeviceState:
    if device_id is None:
        raise web.HTTPNotFound(text="no_device")
    dev = BRIDGE.devices.get(device_id)
    if dev is None:
        raise web.HTTPNotFound(text=f"unknown_device:{device_id}")
    return dev


def _summary(dev: DeviceState) -> dict:
    return {
        "device_id": dev.device_id,
        "model_type": dev.model_type,
        "online": dev.online,
        "age_seconds": dev.age,
        "has_key": bool(dev.key),
        "power": dev.state.get("power"),
        "mode": dev.state.get("mode_label"),
        "fan_speed": dev.state.get("fan_speed"),
        "humidity": dev.state.get("humidity"),
        "temperature": dev.state.get("temperature"),
        "water_level": dev.state.get("water_level"),
        "target_humidity": dev.state.get("target_humidity"),
    }


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "mqtt_connected": BRIDGE.connected,
        "device_count": len(BRIDGE.devices),
        "primary_id": BRIDGE.primary_id,
        "devices_with_key": sum(1 for d in BRIDGE.devices.values() if d.key),
        "gateway_url": GATEWAY_URL,
        "auth_required": bool(API_TOKEN),
    })


async def handle_devices(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "devices": [_summary(d) for d in sorted(BRIDGE.devices.values(), key=lambda x: x.device_id)],
    })


async def handle_state(request: web.Request) -> web.Response:
    raw_id = request.match_info.get("device_id")
    device_id = BRIDGE.primary_id if raw_id is None else int(raw_id)
    dev = _dev_or_404(device_id)
    if not dev.state:
        return web.json_response({"ok": False, "error": "no_state_yet",
                                  "hint": "设备尚未上报，可先 POST /devices/{id}/refresh"}, status=503)
    return web.json_response({
        "ok": True,
        "device_id": dev.device_id,
        "updated_at": datetime.fromtimestamp(dev.state_at).isoformat(timespec="seconds") if dev.state_at else None,
        "age_seconds": dev.age,
        "state": dev.state,
    })


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid_json") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="invalid_json")
    return body


async def handle_control(request: web.Request) -> web.Response:
    raw_id = request.match_info.get("device_id")
    device_id = BRIDGE.primary_id if raw_id is None else int(raw_id)
    body = await _json_body(request)
    ok, detail = BRIDGE.control(device_id, body)
    return web.json_response({"ok": ok, "device_id": device_id, "detail": detail},
                             status=200 if ok else 503)


async def handle_settings_post(request: web.Request) -> web.Response:
    raw_id = request.match_info.get("device_id")
    device_id = BRIDGE.primary_id if raw_id is None else int(raw_id)
    body = await _json_body(request)
    ok, detail = BRIDGE.queue_settings(device_id, body)
    dev = BRIDGE.devices.get(device_id)
    return web.json_response({
        "ok": ok, "device_id": device_id, "detail": detail,
        "effective_settings": dev.settings if dev else None,
    }, status=200 if ok else 400)


async def handle_settings_get(request: web.Request) -> web.Response:
    raw_id = request.match_info.get("device_id")
    device_id = BRIDGE.primary_id if raw_id is None else int(raw_id)
    dev = _dev_or_404(device_id)
    return web.json_response({
        "ok": True, "device_id": device_id,
        "settings": dev.settings, "pending": dev.pending,
        "note": "SET 为整包覆盖语义；设备不支持的字段会被钳制为 0",
    })


async def handle_delete_device(request: web.Request) -> web.Response:
    """删除桥接侧的设备记录（同时清理持久化状态）。

    用于设备重置后 ID 变更的场景：旧 ID 的缓存若不清理，会永远以
    offline 状态残留在设备列表里。注意：只删本地记录，不影响网关注册。
    """
    try:
        device_id = int(request.match_info["device_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(reason="bad_device_id")
    if device_id not in BRIDGE.devices:
        raise web.HTTPNotFound(reason="device_not_found")
    BRIDGE.devices.pop(device_id, None)
    BRIDGE._persist()
    LOG.info("已删除设备记录 %s", device_id)
    return web.json_response({"ok": True, "deleted": device_id})


async def handle_refresh(request: web.Request) -> web.Response:
    raw_id = request.match_info.get("device_id")
    device_id = BRIDGE.primary_id if raw_id is None else int(raw_id)
    BRIDGE.request_status(device_id)
    return web.json_response({"ok": True, "device_id": device_id,
                              "hint": "已请求设备上报，1-2 秒后读取状态"})


async def on_startup(app: web.Application) -> None:
    BRIDGE._loop = asyncio.get_running_loop()
    BRIDGE._load_persisted()
    BRIDGE._apply_keys_file()
    BRIDGE.start()
    app["poller"] = asyncio.create_task(BRIDGE.poll_loop())


async def on_cleanup(app: web.Application) -> None:
    task = app.get("poller")
    if task:
        task.cancel()
    if BRIDGE._client:
        BRIDGE._client.loop_stop()
        BRIDGE._client.disconnect()


def make_app() -> web.Application:
    app = web.Application(middlewares=[require_token])
    app.router.add_get("/health", handle_health)
    app.router.add_get("/devices", handle_devices)
    app.router.add_get("/devices/{device_id}/state", handle_state)
    app.router.add_post("/devices/{device_id}/control", handle_control)
    app.router.add_get("/devices/{device_id}/settings", handle_settings_get)
    app.router.add_post("/devices/{device_id}/settings", handle_settings_post)
    app.router.add_post("/devices/{device_id}/refresh", handle_refresh)
    app.router.add_delete("/devices/{device_id}", handle_delete_device)
    # 兼容短接口（作用于主设备）
    app.router.add_get("/state", handle_state)
    app.router.add_post("/control", handle_control)
    app.router.add_get("/settings", handle_settings_get)
    app.router.add_post("/settings", handle_settings_post)
    app.router.add_post("/refresh", handle_refresh)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=HTTP_PORT, access_log=None)
