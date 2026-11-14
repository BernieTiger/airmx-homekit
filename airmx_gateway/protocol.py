"""AirWater intake codecs. No network operations and no command publishers."""
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

MAX_PAYLOAD = 16384
STATUS_FIELDS = frozenset("power mode cadr lock uv WUD waterflood powerHeatStatus h0 h t t0 water bleSignal wifiSignal wetFilm gooseOnline delayOffTimer hThreshold isNeedClean anion electrolysis electrolysisStatus electrolysisLevel nightLight cleanNotify".split())
SETTING_FIELDS = frozenset("hThreshold powerHeat pirLock strongModeThreshold autoShakeEnable fanTimer fanStart fanStop fanRepeat silentTimer silentStart silentStop silentRepeat clearTimer clearStart clearStop clearRepeat cleanNotify reportInterval logLevel electrolysis electrolysisLevel nightLight".split())
ELECTROLYSIS_PROFILE = {
    "electrolysisLevel4OffTime": 1800, "electrolysisLevel2OpenTime": 600,
    "electrolysisLevel3OpenTime": 1200, "electrolysisLevel4OpenTime": 1800,
    "electrolysisLevel2OffTime": 600, "electrolysisLevel1OpenTime": 60,
    "electrolysisLevel3OffTime": 1200, "electrolysisLevel1OffTime": 120,
}


class IntakeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntakeError("duplicate_field")
        result[key] = value
    return result


def strict_json(raw: bytes | str) -> dict[str, Any]:
    try:
        if isinstance(raw, bytes):
            if len(raw) > MAX_PAYLOAD:
                raise IntakeError("payload_too_large")
            raw = raw.decode("utf-8", errors="strict")
        if len(raw.encode("utf-8")) > MAX_PAYLOAD:
            raise IntakeError("payload_too_large")
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(IntakeError("invalid_number")))
    except IntakeError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise IntakeError("invalid_json") from exc
    if not isinstance(value, dict):
        raise IntakeError("object_required")
    def validate(item, depth=0):
        if depth > 20:
            raise IntakeError("json_too_deep")
        if isinstance(item, str):
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeError:
                raise IntakeError("invalid_unicode") from None
        elif isinstance(item, dict):
            for key, child in item.items():
                validate(key, depth + 1)
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
    validate(value)
    return value


def normalize_mac(value: str) -> str:
    if not isinstance(value, str):
        raise IntakeError("invalid_mac")
    compact = value.replace(":", "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise IntakeError("invalid_mac")
    return compact


def wire_mac_from_wifi(wifi_mac: str) -> str:
    return bytes.fromhex(normalize_mac(wifi_mac))[::-1].hex()


def registration_identity(wire_mac: str) -> tuple[str, int]:
    raw = bytes.fromhex(normalize_mac(wire_mac))
    wifi_mac = raw[::-1].hex()
    device_id = int.from_bytes(raw[:2], "little")
    if device_id == 0:
        raise IntakeError("zero_device_id")
    return wifi_mac, device_id


def registration_response(device_id: int, model_type: int) -> dict:
    # 2026-11-14 二次修正：改用【紧凑】对象形式。
    # 之前失败的两次是：对象形式但带空格（json_response 默认加空格），以及裸数字。
    # 参考实现是手写紧凑字符串，固件很可能做严格文本匹配，因此这里配合
    # server.py 的 separators=(",", ":") 输出 {"status":200,"data":{"awId":N}}。
    data = {"awId": device_id}
    if model_type == 21:
        # Runtime electrolysis parameters must not be sent without validation.
        raise IntakeError("model_profile_unverified")
    return {"status": 200, "data": data}


def topic_device_id(topic: str) -> int:
    match = re.fullmatch(r"airwater/01/0/1/1/([1-9][0-9]{0,9})", topic)
    if not match:
        raise IntakeError("unknown_topic")
    value = int(match.group(1))
    if value > 0xffffffff:
        raise IntakeError("invalid_device_id")
    return value


@dataclass(frozen=True)
class VerifiedReport:
    device_id: int
    command_id: int
    device_time: int
    fields: dict[str, int | str]
    digest: str


def verify_report(topic: str, payload: bytes, key: str, now: int,
                  max_clock_skew: int = 300) -> VerifiedReport:
    device_id = topic_device_id(topic)
    obj = strict_json(payload)
    text = payload.decode("utf-8")
    signature = obj.get("sig")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", signature):
        raise IntakeError("invalid_signature")
    # Preserve the raw unsigned bytes. Do not decode/re-encode to verify.
    suffix = re.search(r',"sig":"([0-9a-fA-F]{32})"\}$', text)
    if not suffix or not text.startswith("{") or suffix.end() != len(text):
        raise IntakeError("signature_format_unverified")
    expected = hashlib.md5((text[1:suffix.start()] + "," + key).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise IntakeError("invalid_signature")
    command = obj.get("cmdId")
    if type(command) is not int or command not in (1001, 1003):
        raise IntakeError("unsupported_command")
    stamp = obj.get("time")
    if type(stamp) is not int or abs(stamp - now) > max_clock_skew:
        raise IntakeError("device_clock_unverified")
    data = obj.get("data")
    if not isinstance(data, dict) or len(data) > 100:
        raise IntakeError("invalid_fields")
    fields = {}
    allowed = STATUS_FIELDS if command == 1001 else SETTING_FIELDS
    for name in allowed:
        if name in data:
            if type(data[name]) is not int or abs(data[name]) > 2**31 - 1:
                raise IntakeError("invalid_field_type")
            fields[name] = data[name]
    if command == 1001:
        if fields.get("power") not in (0, 1) or "mode" not in fields:
            raise IntakeError("incomplete_status")
        # Preserve unfamiliar modes, including 3/4/6; no automatic remapping.
        version = data.get("version")
        if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z._-]{1,40}", version):
            fields["version"] = version
    elif not fields:
        raise IntakeError("empty_settings")
    return VerifiedReport(device_id, command, stamp, fields, hashlib.sha256(payload).hexdigest())


def display_state(fields: dict) -> dict:
    online = fields.get("gooseOnline") == 1
    def valid_scaled(name):
        value = fields.get(name)
        if type(value) is not int or value == 99999:
            return None
        return value / 100
    use_remote_h = online and valid_scaled("h") is not None
    use_remote_t = online and valid_scaled("t") is not None
    return {
        "power": fields.get("power"), "mode": fields.get("mode"),
        "fan_raw": fields.get("cadr"), "water_percent": fields.get("water"),
        "target_humidity_percent": fields.get("hThreshold"),
        "humidity_percent": valid_scaled("h" if use_remote_h else "h0"),
        "temperature_celsius": valid_scaled("t" if use_remote_t else "t0"),
        "humidity_source": "external" if use_remote_h else "internal",
        "temperature_source": "external" if use_remote_t else "internal",
        "external_sensor_online": online,
        "needs_cleaning": fields.get("isNeedClean"),
        "firmware": fields.get("version"),
    }
