"""映射逻辑单测：不需要设备、不需要 MQTT。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hap import mapping  # noqa: E402
from hap.profiles import A3S, A5, UNKNOWN, profile_for  # noqa: E402


# ---------------- 状态解析 ----------------

def test_parse_internal_sensor():
    snap = mapping.parse_state({
        "power": True, "mode": 0, "fan_speed": 40, "target_humidity": 45,
        "humidity": 56.09, "temperature": 21.95, "water_level": 45,
        "child_lock": False, "anion": False, "need_clean": False,
        "external_sensor_online": False, "wifi_signal": -60,
    })
    assert snap.power is True
    assert snap.humidity == 56.09
    assert snap.humidity_source == "internal"
    assert snap.temperature == 21.95


def test_external_sensor_wins_when_online():
    snap = mapping.parse_state({
        "power": 1, "mode": 1,
        "humidity": 56.0, "temperature": 22.0,
        "external_humidity": 48.5, "external_temperature": 24.1,
        "external_sensor_online": True,
    })
    assert snap.humidity == 48.5
    assert snap.humidity_source == "external"
    assert snap.temperature == 24.1


def test_sentinel_99999_is_missing():
    snap = mapping.parse_state({"power": 1, "mode": 0, "raw": {"h0": 99999, "t0": 99999}})
    assert snap.humidity is None
    assert snap.temperature is None


def test_raw_device_payload_scaled():
    snap = mapping.parse_state({"raw": {"power": 1, "mode": 2, "h0": 6241, "t0": 2438, "cadr": 30}})
    assert snap.humidity == 62.41
    assert snap.temperature == 24.38
    assert snap.fan_raw == 30


# ---------------- 模式 ----------------

def test_mode_roundtrip():
    assert mapping.mode_to_target_state(mapping.MODE_AUTO) == mapping.HK_TARGET_AUTO
    assert mapping.mode_to_target_state(mapping.MODE_MANUAL) == mapping.HK_TARGET_HUMIDIFY
    assert mapping.mode_to_target_state(mapping.MODE_SLEEP) == mapping.HK_TARGET_HUMIDIFY
    assert mapping.mode_to_target_state(mapping.MODE_MALFUNCTION) == mapping.HK_TARGET_HUMIDIFY
    assert mapping.target_state_to_mode(mapping.HK_TARGET_AUTO) == mapping.MODE_AUTO
    assert mapping.target_state_to_mode(mapping.HK_TARGET_HUMIDIFY) == mapping.MODE_MANUAL


def test_dehumidify_request_degrades_to_manual():
    # 机型只会加湿，除湿请求不能变成"什么都不做"
    assert mapping.target_state_to_mode(mapping.HK_TARGET_DEHUMIDIFY) == mapping.MODE_MANUAL


# ---------------- 运行状态 ----------------

def test_current_state_power_off():
    snap = mapping.parse_state({"power": 0, "mode": 0})
    assert mapping.current_state(snap) == mapping.HK_STATE_INACTIVE


def test_current_state_humidifying_vs_idle():
    working = mapping.parse_state({"power": 1, "mode": 0, "humidity": 40.0, "target_humidity": 55})
    assert mapping.current_state(working) == mapping.HK_STATE_HUMIDIFYING
    reached = mapping.parse_state({"power": 1, "mode": 0, "humidity": 55.0, "target_humidity": 55})
    assert mapping.current_state(reached) == mapping.HK_STATE_IDLE


def test_malfunction_reports_inactive():
    snap = mapping.parse_state({"power": 1, "mode": 5})
    assert mapping.current_state(snap) == mapping.HK_STATE_INACTIVE


# ---------------- 风量 ----------------

def test_a3s_fan_range():
    """§3.9 真机：cadr 0–100 百分比，量化偶数。"""
    assert A3S.fan.from_percent(0) == 0
    assert A3S.fan.from_percent(100) == 100
    assert A3S.fan.from_percent(50) == 50
    assert A3S.fan.to_percent(0) == 0.0
    assert A3S.fan.to_percent(100) == 100.0
    assert A3S.fan.from_percent(150) == 100
    assert A3S.fan.from_percent(-20) == 0


def test_a5_discrete_steps():
    """§3.8 真机：cadr 1–7 档，8 及以上被忽略。"""
    assert A5.fan.from_percent(0) == 1
    assert A5.fan.from_percent(100) == 7
    assert A5.fan.from_percent(50) == 4
    assert A5.fan.to_percent(1) == 0.0
    assert A5.fan.to_percent(7) == 100.0
    assert A5.fan.from_percent(200) == 7


def test_a3s_fan_never_odd():
    """设备把 cadr 量化到偶数，映射侧必须先量化，否则滑块会来回跳。"""
    for pct in range(0, 101):
        raw = A3S.fan.from_percent(pct)
        assert raw % 2 == 0, f"{pct}% -> {raw}（奇数会被设备改掉）"
        assert 0 <= raw <= 100


def test_water_scaling():
    """water 是 0–120，HAP WaterLevel 只到 100。"""
    from hap.mapping import water_to_percent
    assert water_to_percent(A3S, 120) == 100.0
    assert water_to_percent(A3S, 60) == 50.0
    assert water_to_percent(A3S, 0) == 0.0
    assert water_to_percent(A3S, 150) == 100.0
    assert water_to_percent(A3S, None) is None


def test_capability_flags():
    """能力标志必须与交接单 §3.7–3.10 的双设备实测一致。"""
    assert A3S.has_anion is True and A3S.has_heater is False
    assert A5.has_anion is False and A5.has_heater is True
    # A5 机身无探头，但温湿度来自分体蓝牙计 → 温度服务仍要建
    assert A3S.can_report_temperature is True and A5.can_report_temperature is True
    assert A3S.temperature_from_external_only is False
    assert A5.temperature_from_external_only is True
    # cleanNotify 被忽略、调风量需先切手动
    assert A3S.has_clean_notify is True and A5.has_clean_notify is False
    assert A5.requires_manual_for_fan is True and A3S.requires_manual_for_fan is False
    assert UNKNOWN.has_anion is False and UNKNOWN.has_heater is False


def test_short_names_unique():
    names = {A3S.short_name, A5.short_name, UNKNOWN.short_name}
    assert len(names) == 3, f"配件名后缀重名: {names}"


def test_capability_summary():
    from hap.profiles import capability_summary
    s3 = capability_summary(A3S)
    s5 = capability_summary(A5)
    assert "负离子" in s3 and "无加热" in s3
    assert "无负离子" in s5 and "有加热" in s5


def test_profile_lookup():
    assert profile_for(20).name.startswith("AirWater A3")
    assert profile_for(21).name.startswith("AirWater A3")
    assert profile_for(11).name.startswith("AirWater A5")
    assert profile_for(None).name.startswith("未知")


# ---------------- 目标湿度 ----------------

def test_target_humidity_clamp():
    assert mapping.clamp_target_humidity(10) == 30.0
    assert mapping.clamp_target_humidity(90) == 80.0
    assert mapping.clamp_target_humidity(45) == 45.0
    assert mapping.clamp_target_humidity(None) is None
    # A5 实测区间 30–70，超过 70 要钳住
    assert mapping.clamp_target_humidity(75, A5) == 70.0
    assert mapping.clamp_target_humidity(75, A3S) == 75.0
