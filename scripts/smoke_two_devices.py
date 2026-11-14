#!/usr/bin/env python3
"""双设备冒烟：A3S + A5 同屋，验证"按硬件能力决定暴露什么"。

重点不是"两个都跑起来"，而是：
  1. 两台设备不能重名（否则 Home App 里分不清）
  2. AID 必须不同且稳定（顺序变了 iOS 会当成新配件）
  3. A5 不能出现「负离子」——它没有这个硬件，做出来就是假开关
  4. A3S 不能出现「电辅热」——同理
  5. 序列号不能撞（两台都可能落在 MQTT 槽位 0）

运行：
    python scripts/smoke_two_devices.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AIRMX_HAP_PROFILE", "full")

from pyhap.accessory import Bridge  # noqa: E402
from pyhap.accessory_driver import AccessoryDriver  # noqa: E402

from hap.__main__ import BRIDGE_NAME, attach  # noqa: E402

UUID_SWITCH = "49"

# 模拟 bridge /devices 的返回：id=0 是 A3S，17493 是 A5
DEVICES = [
    {"device_id": 10001, "model_type": 20, "online": True, "has_key": True},
    {"device_id": 10002, "model_type": 11, "online": True, "has_key": True},
]


GOOSE_PAIRED = False


class FakeClient:
    async def state(self, device_id):
        if device_id == 10002 and GOOSE_PAIRED:
            # 分体蓝牙温湿度计已配对：读数走 h/t，h0/t0 仍是 99999
            return {"ok": True, "age_seconds": 1, "state": {
                "power": True, "mode": 1, "fan_speed": 5, "target_humidity": 50,
                "humidity": 45.12, "temperature": 23.1, "water_level": 60,
                "external_humidity": 45.12, "external_temperature": 23.1,
                "external_sensor_online": True, "need_clean": False, "anion": False}}
        if device_id == 10002:
            # A5 无内置温湿度传感器：h0/t0 = 99999 → null
            return {"ok": True, "age_seconds": 1, "state": {
                "power": True, "mode": 1, "fan_speed": 4, "target_humidity": 50,
                "humidity": None, "temperature": None, "water_level": 60,
                "need_clean": False, "anion": False, "external_sensor_online": False}}
        return {"ok": True, "age_seconds": 1, "state": {
            "power": True, "mode": 0, "fan_speed": 40, "target_humidity": 45,
            "humidity": 56.0, "temperature": 22.0, "water_level": 60,
            "need_clean": False, "anion": False, "external_sensor_online": False}}

    async def get_settings(self, device_id):
        return {"hThreshold": 45, "pirLock": 1, "cleanNotify": 1, "powerHeat": 0}

    async def control(self, *a, **k):
        return {}

    async def settings(self, *a, **k):
        return {}

    async def refresh(self, device_id):
        return {}


def switches_of(blob: dict, aid: int) -> set[str]:
    names = set()
    for acc in blob["accessories"]:
        if acc["aid"] != aid:
            continue
        for svc in acc["services"]:
            if svc["type"] != UUID_SWITCH:
                continue
            for ch in svc["characteristics"]:
                if ch["type"] == "23":
                    names.add(ch.get("value"))
    return names


def info_of(blob: dict, aid: int) -> dict:
    out = {}
    for acc in blob["accessories"]:
        if acc["aid"] != aid:
            continue
        for svc in acc["services"]:
            if svc["type"] != "3E":
                continue
            for ch in svc["characteristics"]:
                out[ch["type"]] = ch.get("value")
    return out


async def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="airmx-two-")
    driver = AccessoryDriver(
        port=51829, persist_file=f"{tmpdir}/hap.state",
        pincode=b"031-45-154", advertise=False,
        loop=asyncio.get_running_loop(),
    )
    root = Bridge(driver, BRIDGE_NAME)
    mounted: dict[int, object] = {}
    client = FakeClient()

    for item in sorted(DEVICES, key=lambda d: d["device_id"]):
        attach(driver, root, item, client, mounted, multi=True)

    driver.add_accessory(root)
    blob = driver.get_accessories()

    aids = sorted(a["aid"] for a in blob["accessories"] if a["aid"] != 1)
    assert len(aids) == 2, f"应有 2 个设备配件，实际 {aids}"
    print(f"· 两台设备各占一个配件：AID={aids}")

    names = [info_of(blob, aid).get("23") for aid in aids]
    assert len(set(names)) == 2, f"设备重名：{names}"
    print(f"· 名称唯一：{names}")

    serials = [info_of(blob, aid).get("30") for aid in aids]
    assert len(set(serials)) == 2, f"序列号撞了：{serials}"
    print(f"· 序列号唯一：{serials}")

    # AID 小的那个是 device_id=0 → A3S
    a3s, a5 = aids
    s3 = switches_of(blob, a3s)
    s5 = switches_of(blob, a5)

    assert "负离子" in s3, f"A3S 应有负离子，实际 {s3}"
    assert "负离子" not in s5, f"A5 不该有负离子（无此硬件），实际 {s5}"
    assert "电辅热" not in s3, f"A3S 不该有电辅热（无加热模块），实际 {s3}"
    assert "电辅热" in s5, f"A5 应有电辅热，实际 {s5}"
    # §3.8：A5 忽略 cleanNotify，建了就是死开关
    assert "清洁提醒" in s3 and "清洁提醒" not in s5, f"清洁提醒门控错误: A3S={s3} A5={s5}"
    assert {"睡眠模式", "接近感应"} <= s3 & s5

    print(f"· A3S 开关：{sorted(s3)}")
    print(f"· A5  开关：{sorted(s5)}")

    # A5 无内置温湿度传感器 → 不该有 TemperatureSensor 服务
    def has_service(aid, t):
        for acc in blob["accessories"]:
            if acc["aid"] == aid:
                return any(s["type"] == t for s in acc["services"])
        return False

    # A5 机身没探头，但配上分体蓝牙计后会有读数 → 服务必须存在
    assert has_service(a3s, "8A"), "A3S 应有温度传感器"
    assert has_service(a5, "8A"), "A5 也需要温度服务（分体计配对后要用）"
    print("· 温度服务：两台都有（A5 依赖分体蓝牙计）")
    print("· 能力门控正确")

    # ---- 分体蓝牙温湿度计配对后的路径 ----
    global GOOSE_PAIRED
    GOOSE_PAIRED = True
    a5_acc = mounted[10002]
    await a5_acc.async_refresh()
    hum = a5_acc.main_service.get_characteristic("CurrentRelativeHumidity").get_value()
    t_ch = a5_acc.get_service("TemperatureSensor").get_characteristic("CurrentTemperature")
    active = a5_acc.get_service("TemperatureSensor").get_characteristic("StatusActive")
    assert hum == 45, f"分体计湿度未落到 HomeKit: {hum}"
    assert abs(t_ch.get_value() - 23.1) <= 0.1, f"分体计温度未落到 HomeKit: {t_ch.get_value()}"
    assert active.get_value() is True, "有读数时 StatusActive 应为 true"
    print(f"· 分体计已配对：湿度 {hum}% / 温度 {t_ch.get_value()}℃ / StatusActive=true")

    print("\n双设备冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
