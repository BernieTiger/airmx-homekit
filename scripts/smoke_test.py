#!/usr/bin/env python3
"""端到端冒烟测试（不需要真机、不需要 NAS、不需要 MQTT）。

起一个假的 bridge（HTTP），把真实的 HAP 配件挂上去，验证：
  1. 所有服务都挂在**同一个 AID** 下 → Home App 只会出现一张磁贴
  2. 主服务特征齐全且 UUID 正确
  3. 设备状态能正确落到 HomeKit 特征
  4. Home App 的写入能变成正确的控制指令
  5. 目标湿度写入走 settings 而不是 control，且连续拖动被防抖收敛

运行：
    python scripts/smoke_test.py                # 默认 minimal 档
    python scripts/smoke_test.py --profile full
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from pyhap.accessory import Bridge  # noqa: E402
from pyhap.accessory_driver import AccessoryDriver  # noqa: E402

from hap.accessory import HumidifierAccessory  # noqa: E402
from hap.bridge_client import BridgeClient  # noqa: E402
from hap.profiles import A3S  # noqa: E402

RECEIVED: list[tuple[str, dict]] = []

STATE = {
    "ok": True,
    "device_id": 10001,
    "state": {
        "power": True, "mode": 0, "fan_speed": 40, "target_humidity": 45,
        "humidity": 56.09, "temperature": 21.95, "water_level": 45,
        "child_lock": False, "anion": False, "uv": True, "need_clean": True,
        "external_sensor_online": False, "wifi_signal": -60, "firmware": "00.01.20",
    },
}

# 档位 → (sleep, anion, clean_notify, pir_lock)
FLAGS = {
    "minimal": (False, False, False, False),
    "standard": (True, False, False, False),
    "full": (True, True, True, True),
}
EXPECTED_SWITCHES = {
    "minimal": set(),
    "standard": {"睡眠模式"},
    "full": {"睡眠模式", "负离子", "清洁提醒", "接近感应"},
}

UUID_HUMIDIFIER = "BD"
UUID_TEMP = "8A"
UUID_FILTER = "BA"
UUID_SWITCH = "49"
UUID_HUMIDITY_SENSOR = "82"
UUID_FAN = "B7"


def make_bridge_app() -> web.Application:
    app = web.Application()

    async def health(_):
        return web.json_response({"mqtt_connected": True, "device_count": 1, "primary_id": 10001})

    async def devices(_):
        return web.json_response({"ok": True, "devices": [
            {"device_id": 10001, "model_type": 20, "online": True, "has_key": True}]})

    async def state(_):
        return web.json_response(STATE)

    async def control(request):
        body = await request.json()
        RECEIVED.append(("control", body))
        if "power" in body:
            STATE["state"]["power"] = bool(body["power"])
        if "mode" in body:
            STATE["state"]["mode"] = body["mode"]
        if "cadr" in body:
            STATE["state"]["fan_speed"] = body["cadr"]
        if "lock" in body:
            STATE["state"]["child_lock"] = bool(body["lock"])
        return web.json_response({"ok": True, "detail": "queued"})

    async def settings(request):
        body = await request.json()
        RECEIVED.append(("settings", body))
        if "hThreshold" in body:
            STATE["state"]["target_humidity"] = body["hThreshold"]
        return web.json_response({"ok": True, "effective_settings": body})

    async def get_settings(_):
        return web.json_response({"ok": True, "settings": {
            "hThreshold": 45, "pirLock": 1, "cleanNotify": 1, "powerHeat": 0}})

    async def refresh(_):
        RECEIVED.append(("refresh", {}))
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    app.router.add_get("/devices", devices)
    app.router.add_get("/devices/{id}/state", state)
    app.router.add_post("/devices/{id}/control", control)
    app.router.add_get("/devices/{id}/settings", get_settings)
    app.router.add_post("/devices/{id}/settings", settings)
    app.router.add_post("/devices/{id}/refresh", refresh)
    return app


def services_of(blob: dict, aid: int) -> list[dict]:
    for acc in blob["accessories"]:
        if acc["aid"] == aid:
            return acc["services"]
    return []


def aid_with(blob: dict, svc_type: str) -> int | None:
    for acc in blob["accessories"]:
        for svc in acc["services"]:
            if svc["type"] == svc_type:
                return acc["aid"]
    return None


def switch_names(services: list[dict]) -> set[str]:
    names = set()
    for svc in services:
        if svc["type"] != UUID_SWITCH:
            continue
        for ch in svc["characteristics"]:
            if ch["type"] == "23":
                names.add(ch.get("value"))
    return names


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="minimal", choices=sorted(FLAGS))
    args = ap.parse_args()
    sleep, anion, clean, pir = FLAGS[args.profile]

    runner = web.AppRunner(make_bridge_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 25888)
    await site.start()

    tmpdir = tempfile.mkdtemp(prefix="airmx-hap-")
    driver = AccessoryDriver(
        port=51826,
        persist_file=f"{tmpdir}/hap.state",
        pincode=b"031-45-154",
        advertise=False,
        loop=asyncio.get_running_loop(),
    )
    root = Bridge(driver, "秒新加湿器")

    async with BridgeClient("http://127.0.0.1:25888") as client:
        acc = HumidifierAccessory(
            driver, "秒新加湿器", 10001, client, A3S,
            expose_sleep=sleep, expose_anion=anion, expose_clean_notify=clean,
            expose_pir_lock=pir,
        )
        root.add_accessory(acc)
        driver.add_accessory(root)
        await driver.async_start()

        try:
            blob = driver.get_accessories()

            # ---- 1. 结构性断言：所有服务同一 AID ----
            dev_aid = aid_with(blob, UUID_HUMIDIFIER)
            assert dev_aid is not None, "缺少 HumidifierDehumidifier"
            for t in (UUID_TEMP, UUID_FILTER, UUID_HUMIDITY_SENSOR, UUID_FAN):
                assert aid_with(blob, t) == dev_aid, f"服务 {t} 不在同一配件下，会多出一张磁贴"
            if EXPECTED_SWITCHES[args.profile]:
                assert aid_with(blob, UUID_SWITCH) == dev_aid, "开关不在同一配件下，会多出独立磁贴"
            svcs = services_of(blob, dev_aid)
            print(f"· [{args.profile}] 全部 {len(svcs)} 个服务同属 AID={dev_aid} → 只有一张磁贴")

            # ---- 2. 主服务特征 ----
            humidifier = next(s for s in svcs if s["type"] == UUID_HUMIDIFIER)
            got = {c["type"] for c in humidifier["characteristics"]}
            need = {"B0", "B3", "B4", "10", "CA", "29", "B5", "A7"}
            missing = need - got
            assert not missing, f"主服务缺特征: {missing}"
            target = next(c for c in humidifier["characteristics"] if c["type"] == "B4")
            assert target.get("valid-values") == [0, 1], \
                f"除湿没被剔除: valid-values={target.get('valid-values')}"
            print(f"· 主服务 {len(got)} 个特征；模式合法值 {target.get('valid-values')}（自动/加湿，无除湿）")



            # ---- 3. 附加开关数量符合档位 ----
            names = switch_names(svcs)
            assert names == EXPECTED_SWITCHES[args.profile], \
                f"开关应为 {EXPECTED_SWITCHES[args.profile]}，实际 {names}"
            print(f"· 附加开关：{sorted(names) if names else '无（零按钮）'}")

            # ---- 4. 读状态 ----
            await acc.async_refresh()
            m = acc.main_service
            assert m.get_characteristic("Active").get_value() == 1
            assert m.get_characteristic("CurrentRelativeHumidity").get_value() == 56
            assert abs(m.get_characteristic("RelativeHumidityHumidifierThreshold").get_value() - 45) < 0.01
            t = acc.get_service("TemperatureSensor").get_characteristic(
                "CurrentTemperature").get_value()
            assert abs(t - 21.95) <= 0.1, f"温度落位异常: {t}"
            # 设备 water=45 是 0–120 量程 → 37.5%
            assert m.get_characteristic("WaterLevel").get_value() == 37.5
            assert acc.get_service("FilterMaintenance").get_characteristic(
                "FilterChangeIndication").get_value() == 1
            pct = m.get_characteristic("RotationSpeed").get_value()
            assert abs(pct - 40.0) < 0.1, f"cadr=40 → 40%（A3S 0–100 百分比），实际 {pct}"
            fw = acc.get_service("AccessoryInformation").get_characteristic(
                "FirmwareRevision").get_value()
            assert fw == "00.01.20", f"配件信息里的固件版本未更新: {fw}"
            await acc.async_refresh_settings(force=True)
            print(f"· 状态落位：开 / 56% / 目标45% / {t}℃ / 水位37.5%(45/120) / 风量40% / 需清洁")
            hum_v = acc.get_service("HumiditySensor").get_characteristic(
                "CurrentRelativeHumidity").get_value()
            fan_act = acc.get_service("Fanv2").get_characteristic("Active").get_value()
            fan_spd = acc.get_service("Fanv2").get_characteristic("RotationSpeed").get_value()
            assert hum_v == 56, f"湿度传感器未同步: {hum_v}"
            assert fan_act == 1 and abs(fan_spd - 40) < 0.1, f"风扇未同步: {fan_act}/{fan_spd}"
            print(f"· 新增服务：湿度传感器 {hum_v}% / 风扇 Active={fan_act} 风量={fan_spd}%")

            # 用户在家庭 App 里改配件名后，纠偏逻辑不能把它改回去
            main_name = acc.main_service.get_characteristic("ConfiguredName")
            main_name.set_value("我的加湿器")
            acc._restore_names()
            assert main_name.get_value() == "我的加湿器", "主服务名被纠偏逻辑覆盖了，用户改名会被回滚"
            # 次级服务仍然要被纠偏
            if hasattr(acc, "sleep_service"):
                sleep_name = acc.sleep_service.get_characteristic("ConfiguredName")
                sleep_name.set_value("开关 2")
                acc._restore_names()
                assert sleep_name.get_value() == "睡眠模式", "次级服务名没有恢复"
            print("· 改名保护：主服务名保留用户修改，次级服务名仍被纠偏")
            serial = acc.get_service("AccessoryInformation").get_characteristic(
                "SerialNumber").get_value()
            print(f"· 配件设置页信息：AIRMX / {acc.profile.name} / {serial} / 固件 {fw}")

            # ---- 5. 控制写入 ----
            RECEIVED.clear()
            m.get_characteristic("Active").client_update_value(0)
            await asyncio.sleep(5.0)
            assert ("control", {"power": 0}) in RECEIVED, f"未收到关机指令: {RECEIVED}"
            print("· 关机 → {'power': 0}")

            RECEIVED.clear()
            m.get_characteristic("RotationSpeed").client_update_value(100)
            await asyncio.sleep(5.0)
            ctrl = [r for r in RECEIVED if r[0] == "control"]
            assert ctrl and ctrl[0][1].get("cadr") == 100, f"风量映射错误: {RECEIVED}"
            print("· 风量 100% → cadr=100")

            RECEIVED.clear()
            m.get_characteristic("LockPhysicalControls").client_update_value(1)
            await asyncio.sleep(5.0)
            ctrl = [r for r in RECEIVED if r[0] == "control"]
            assert ctrl and ctrl[0][1].get("lock") == 1, f"童锁映射错误: {RECEIVED}"
            print("· 童锁 → {'lock': 1}")

            # ---- 6. 设置写入 + 防抖 ----
            RECEIVED.clear()
            for v in (52, 53, 54, 55):
                m.get_characteristic("RelativeHumidityHumidifierThreshold").client_update_value(v)
                await asyncio.sleep(0.1)
            await asyncio.sleep(7.0)
            sets = [r for r in RECEIVED if r[0] == "settings"]
            assert len(sets) == 1, f"拖动被放大成 {len(sets)} 次 SET: {sets}"
            assert sets[0][1].get("hThreshold") == 55, f"未取最后一次值: {sets}"
            print("· 连续拖 4 次 → 收敛为 1 次 SET，取值 55")

            # ---- 7. 睡眠开关（仅 standard / full）----
            if sleep:
                RECEIVED.clear()
                acc.sleep_service.get_characteristic("On").client_update_value(1)
                await asyncio.sleep(5.0)
                ctrl = [r for r in RECEIVED if r[0] == "control"]
                assert ctrl and ctrl[0][1].get("mode") == 2, f"睡眠未映射为 mode=2: {RECEIVED}"
                print("· 睡眠开关 → mode=2")

            print(f"\n档位 {args.profile} 冒烟全部通过")
            return 0
        finally:
            await driver.async_stop()
            await runner.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
