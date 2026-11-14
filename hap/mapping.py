"""HomeKit 语义 ↔ AirWater 协议的纯函数映射。

这个模块不做 I/O，全部可单测。所有"官方 App 有、HomeKit 没有"的降级决策
都集中在这里，读这一个文件就能知道丢了什么。
"""
from __future__ import annotations

from dataclasses import dataclass

from .profiles import Profile

# ---- 设备 mode 枚举（真机实测 + APK 协议模型）----
MODE_MANUAL = 0
MODE_AUTO = 1
MODE_SLEEP = 2
MODE_MALFUNCTION = 5

MODE_LABELS = {
    MODE_MANUAL: "manual",
    MODE_AUTO: "auto",
    MODE_SLEEP: "sleep",
    MODE_MALFUNCTION: "malfunction",
}

# ---- HomeKit TargetHumidifierDehumidifierState ----
HK_TARGET_AUTO = 0          # Humidifier or Dehumidifier
HK_TARGET_HUMIDIFY = 1      # Humidifier
HK_TARGET_DEHUMIDIFY = 2    # Dehumidifier（本机型不存在，不暴露）

# ---- HomeKit CurrentHumidifierDehumidifierState ----
HK_STATE_INACTIVE = 0
HK_STATE_IDLE = 1
HK_STATE_HUMIDIFYING = 2

# ---- HomeKit FilterChangeIndication ----
HK_FILTER_OK = 0
HK_FILTER_CHANGE = 1

# 设备用 99999 表示"该传感器不存在"
_MISSING = 99999

# 目标湿度安全区间：真机只验证过 40/45；低于 30 或高于 80 没有依据
TARGET_HUMIDITY_MIN = 30.0
TARGET_HUMIDITY_MAX = 80.0


@dataclass(frozen=True)
class Snapshot:
    """一次上报归一化后的只读快照。"""

    power: bool
    mode: int
    fan_raw: int | None
    target_humidity: float | None
    humidity: float | None
    humidity_source: str
    temperature: float | None
    temperature_source: str
    water_level: float | None
    child_lock: bool
    anion: bool
    uv: bool
    need_clean: bool
    external_sensor_online: bool
    wifi_signal: int | None
    firmware: str | None
    raw: dict


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scaled(value) -> float | None:
    """h0/t0/h/t 是 ×100 定点数；99999 与 <=0 一律视为无效。"""
    v = _int(value)
    if v is None or v == _MISSING or v <= 0:
        return None
    return round(v / 100.0, 2)


def parse_state(payload: dict) -> Snapshot:
    """把 bridge 的 /state 响应转成快照。

    bridge 已经做了一层归一化（power/mode/fan_speed/...），这里兼容两种输入：
    归一化后的 dict，或设备原始 data dict。
    """
    if "state" in payload and isinstance(payload["state"], dict):
        payload = payload["state"]
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else payload

    power = bool(payload.get("power", raw.get("power", 0)))
    mode = _int(payload.get("mode", raw.get("mode")))
    if mode is None:
        mode = MODE_MANUAL

    fan = _int(payload.get("fan_speed", raw.get("cadr")))
    target = payload.get("target_humidity", raw.get("hThreshold"))
    target = float(target) if _int(target) is not None else None

    internal_h = payload.get("humidity")
    internal_h = internal_h if internal_h is not None else _scaled(raw.get("h0"))
    internal_t = payload.get("temperature")
    internal_t = internal_t if internal_t is not None else _scaled(raw.get("t0"))

    external_h = payload.get("external_humidity")
    external_h = external_h if external_h is not None else _scaled(raw.get("h"))
    external_t = payload.get("external_temperature")
    external_t = external_t if external_t is not None else _scaled(raw.get("t"))

    online = payload.get("external_sensor_online")
    if online is None:
        online = _int(raw.get("gooseOnline")) == 1
    online = bool(online)

    # 与网关 display_state 保持一致：分体湿度计在线时优先用它的读数
    if online and external_h is not None:
        humidity, h_src = external_h, "external"
    else:
        humidity, h_src = internal_h, "internal"
    if online and external_t is not None:
        temperature, t_src = external_t, "external"
    else:
        temperature, t_src = internal_t, "internal"

    water = payload.get("water_level", raw.get("water"))
    water = float(water) if _int(water) is not None else None

    return Snapshot(
        power=power,
        mode=mode,
        fan_raw=fan,
        target_humidity=target,
        humidity=humidity,
        humidity_source=h_src,
        temperature=temperature,
        temperature_source=t_src,
        water_level=water,
        child_lock=bool(payload.get("child_lock", _int(raw.get("lock")))),
        anion=bool(payload.get("anion", _int(raw.get("anion")))),
        uv=bool(payload.get("uv", _int(raw.get("uv")))),
        need_clean=bool(payload.get("need_clean", _int(raw.get("isNeedClean")))),
        external_sensor_online=online,
        wifi_signal=_int(payload.get("wifi_signal", raw.get("wifiSignal"))),
        firmware=payload.get("firmware", raw.get("version")),
        raw=raw,
    )


# ---------------- 模式 ----------------

def mode_to_target_state(mode: int) -> int:
    """设备 mode → HomeKit 目标状态。

    HomeKit 只有 auto / humidify / dehumidify 三态，没有"静音"和"强力"。
    静音走独立开关；强力暂按手动处理（协议上是否独立枚举值未验证）。
    """
    if mode == MODE_AUTO:
        return HK_TARGET_AUTO
    return HK_TARGET_HUMIDIFY


def target_state_to_mode(target: int) -> int:
    if int(target) == HK_TARGET_AUTO:
        return MODE_AUTO
    return MODE_MANUAL


def current_state(snapshot: Snapshot) -> int:
    if not snapshot.power:
        return HK_STATE_INACTIVE
    if snapshot.mode == MODE_MALFUNCTION:
        return HK_STATE_INACTIVE
    # 湿度已达目标（留 2% 回差）视为待机，否则视为正在加湿
    if (
        snapshot.humidity is not None
        and snapshot.target_humidity is not None
        and snapshot.humidity >= snapshot.target_humidity - 2
    ):
        return HK_STATE_IDLE
    return HK_STATE_HUMIDIFYING


# ---------------- 风量 ----------------

def fan_to_percent(profile: Profile, raw: int | None) -> float | None:
    return profile.fan.to_percent(raw)


def percent_to_fan(profile: Profile, percent: float | None) -> int | None:
    return profile.fan.from_percent(percent)


def water_to_percent(profile: Profile, raw: int | None) -> float | None:
    """设备 water 是 0–120（交接单 §3.5），HAP 的 WaterLevel 只到 0–100。"""
    if raw is None:
        return None
    full = profile.water_max or 100
    return round(max(0.0, min(100.0, raw * 100.0 / full)), 1)


def clamp_target_humidity(value: float | None, profile: Profile | None = None) -> float | None:
    """目标湿度按机型区间钳制：A5 实测 30–70，A3S 用 30–80（未逐点验证）。"""
    if value is None:
        return None
    lo = profile.humidity_min if profile is not None else TARGET_HUMIDITY_MIN
    hi = profile.humidity_max if profile is not None else TARGET_HUMIDITY_MAX
    return float(max(lo, min(hi, float(value))))
