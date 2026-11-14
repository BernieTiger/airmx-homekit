"""AirWater 加湿器的 HomeKit 配件。

只暴露"bridge 确实可读写"的能力，避免做出按下去没反应的假开关。
每个 setter 都做三件事：乐观更新（让 Home App 立刻响应）→ 投递指令 →
回读设备真实状态做校正（失败则回滚）。

服务布局（**同一个配件，多服务 → 只有一张磁贴**）：
  HumidifierDehumidifier  主服务，承载 7 个可操作/可读项：
                          开关 / 自动-手动 / 目标湿度 / 风量 / 童锁 / 水位 / 当前湿度
  TemperatureSensor       当前温度（HumidifierDehumidifier 规范里没有温度特征，必须独立挂）
  FilterMaintenance       清洁提醒（只读，不产生按钮）

可选附加开关（由 AIRMX_HAP_PROFILE 控制，默认 minimal = 不加）：
  Switch "睡眠模式"        设备 mode=2，HomeKit 三态塞不下"静音"
  Switch "负离子"
  Switch "清洁提醒"        SET.cleanNotify

为什么默认不加：HAP 没有"把功能放进配件设置页"的机制，配件设置页是系统固定项
（名称/房间/收藏/自动化/状态/移除），无法注入自定义开关。所以每个附加功能
只能是一个独立服务，而服务一多，Home App 展开后就是一排按钮。宁可少给。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_HUMIDIFIER

from . import mapping
from .bridge_client import BridgeClient, BridgeError
from .mapping import Snapshot
from .profiles import Profile

LOG = logging.getLogger("airmx.hap.accessory")

# Home App 连续改动时把多条指令合并成一次下发，避免 power+mode 互相覆盖
CONTROL_DEBOUNCE = 0.4
# 设置类写入的静默窗口。设备对两次 SET 有约 10 秒节流，间隔过短会被静默丢弃，
# 而 Home App 拖动湿度滑块会连续产生几十次调用，必须在这里收敛成一次。
SETTINGS_DEBOUNCE = 1.2
# 指令下发后多久回读一次设备真实状态
RECONCILE_DELAY = 2.5
# 设备 SET 节流 ~10s，bridge 每 60s 轮询。给目标湿度 45 秒的确认窗口，
# 期间一直显示用户拖到的值，不被旧数据弹回。
THRESHOLD_CONFIRM_TIMEOUT = 45.0
# bridge 写节流约 10 秒。补偿指令必须落在窗口之外，否则会被静默吞掉。
COMPENSATE_DELAY = 12.0
# bridge 每 60 秒轮询设备；超过这个秒数没有新上报就判定设备离线，
# 避免 bridge 还端着旧缓存、Home App 却显示"一切正常"。
STALE_SECONDS = 200


class HumidifierAccessory(Accessory):
    category = CATEGORY_HUMIDIFIER

    def __init__(
        self,
        driver,
        display_name: str,
        device_id: int,
        client: BridgeClient,
        profile: Profile,
        expose_sleep: bool = True,
        expose_anion: bool = True,
        expose_clean_notify: bool = True,
        expose_pir_lock: bool = False,
    ) -> None:
        super().__init__(driver, display_name)
        self.device_id = device_id
        self.client = client
        self.profile = profile
        self.snapshot: Snapshot | None = None
        self.online = False

        self._last_non_sleep_mode = mapping.MODE_MANUAL
        self._pending_control: dict[str, Any] = {}
        self._control_task: asyncio.Task | None = None
        self._pending_settings: dict[str, Any] = {}
        self._settings_task: asyncio.Task | None = None
        # 写入进行中时暂停用设备快照覆盖 UI，否则 Home App 会先跳回旧值再跳回来
        self._hold_until = 0.0
        # 设置项（pirLock/cleanNotify/powerHeat）不在 /state 里，要单独从 bridge 取
        self._settings: dict[str, Any] = {}
        self._settings_age = 0
        # 目标湿度的"乐观值"：设备 SET 有 ~10 秒节流，bridge 轮询又是 60 秒，
        # 在设备确认之前用设备旧值覆盖滑块，用户就会看到"拖完又弹回去"。
        # 服务名对照表：iOS 会在配对后把次级服务改写成「开关 N」，
        # 这里每轮同步都把名字写回去（详见 _restore_names）。
        self._desired_names: list[tuple[Any, str]] = []
        self._pending_threshold: float | None = None
        self._pending_threshold_until = 0.0
        self._humidity_missing = False
        # 最近若干次下发记录。容器日志在 DSM 上看不到，
        # 没有这个就只能靠猜"我们到底发了没有"。
        self._recent_calls: deque[dict] = deque(maxlen=12)

        self._build(display_name, expose_sleep, expose_anion, expose_clean_notify,
                    expose_pir_lock)

    # ---------------- 服务装配 ----------------

    def _build(self, name: str, sleep: bool, anion: bool, clean_notify: bool,
               pir_lock: bool = False) -> None:
        main = self.add_preload_service(
            "HumidifierDehumidifier",
            chars=[
                "Name",
                "ConfiguredName",
                "RotationSpeed",
                "WaterLevel",
                "LockPhysicalControls",
                "RelativeHumidityHumidifierThreshold",
            ],
        )
        main.configure_char("Name", value=name)
        main.configure_char("ConfiguredName", value=name)
        main.configure_char("Active", setter_callback=self._set_active)
        main.configure_char(
            "TargetHumidifierDehumidifierState", setter_callback=self._set_target_state
        )
        # 规范给这个特征三个值：0 自动 / 1 加湿 / 2 除湿。
        # 只删「除湿」：本机型没有除湿能力，列出来就是个骗人的菜单项。
        # 「自动」保留 —— 实测设备在自动下确实会按目标湿度调节风量
        # （A3S：目标 32→风量14；目标 80→风量 100）。
        # 注意 iOS 在自动档会隐藏目标湿度滑块，这是苹果的设计：
        # 想改目标需先切回「加湿」档，改完再切自动。
        main.get_characteristic("TargetHumidifierDehumidifierState").override_properties(
            valid_values={"HumidifierOrDehumidifier": 0, "Humidifier": 1}
        )
        main.configure_char(
            "RelativeHumidityHumidifierThreshold",
            value=50,
            setter_callback=self._set_target_humidity,
        )
        main.configure_char("RotationSpeed", value=50, setter_callback=self._set_rotation_speed)
        main.configure_char("CurrentRelativeHumidity", value=0)
        main.configure_char("CurrentHumidifierDehumidifierState", value=mapping.HK_STATE_INACTIVE)
        main.configure_char("WaterLevel", value=0)
        main.configure_char("LockPhysicalControls", setter_callback=self._set_lock)
        self.main_service = main
        self.set_primary_service(main)

        # ⚠ 布局已冻结（2026-11-14 定稿）：服务的注册顺序 = IID 顺序 = Home App 的卡片顺序。
        # 改动这里会让已配对设备全部"未响应"，必须重新配对才能恢复。
        # 目标顺序：主服务 → 风量 → 温度 → 湿度 → 水位 → 各开关。
        # iOS 不渲染主服务上的 RotationSpeed，想要真正的风量滑块只能单独挂 Fanv2。
        # 它的开关键接设备开关机，避免"风扇关了但加湿器还在转"的自相矛盾状态。
        fan = self.add_preload_service(
            "Fanv2", chars=["Name", "ConfiguredName", "RotationSpeed"])
        fan.configure_char("Name", value="风量")
        fan.configure_char("ConfiguredName", value="风量")
        fan.configure_char("Active", setter_callback=self._set_active)
        fan.configure_char("RotationSpeed", value=50, setter_callback=self._set_rotation_speed)
        self.fan_service = fan



        # A5 机身没有探头，但配对上分体蓝牙温湿度计后会有读数，
        # 所以服务照建，只是没数据时把 StatusActive 置 false（Home App 显示不可用）。
        if self.profile.can_report_temperature:
            temp = self.add_preload_service(
                "TemperatureSensor", chars=["StatusActive", "Name", "ConfiguredName"])
            temp.configure_char("Name", value="温度")
            temp.configure_char("ConfiguredName", value="温度")
            temp.configure_char("CurrentTemperature", value=20)
            temp.configure_char("StatusActive", value=True)

        # 主服务里的 CurrentRelativeHumidity 在 iOS 上只画图形、不给数字，
        # 单独挂一个湿度传感器，Home 才会显示「74%」这样的读数。
        hum = self.add_preload_service(
            "HumiditySensor", chars=["Name", "ConfiguredName", "StatusActive"])
        hum.configure_char("Name", value="湿度")
        hum.configure_char("ConfiguredName", value="湿度")
        hum.configure_char("CurrentRelativeHumidity", value=0)
        hum.configure_char("StatusActive", value=True)
        self.humidity_service = hum

        # HAP 没有"水位传感器"这种服务；能承载百分比读数的只有
        # FilterMaintenance.FilterLifeLevel。这里借用它的槽位显示水位，
        # FilterChangeIndication 保留给"需清洁"，仍可在配件设置里订阅通知。
        filt = self.add_preload_service(
            "FilterMaintenance", chars=["Name", "ConfiguredName", "FilterLifeLevel"])
        filt.configure_char("Name", value="水位")
        filt.configure_char("ConfiguredName", value="水位")
        filt.configure_char("FilterLifeLevel", value=0)
        filt.configure_char("FilterChangeIndication", value=mapping.HK_FILTER_OK)

        if sleep:
            self.sleep_service = self.add_preload_service(
                "Switch", chars=["Name", "ConfiguredName"])
            self.sleep_service.configure_char("Name", value="睡眠模式")
            self.sleep_service.configure_char("ConfiguredName", value="睡眠模式")
            self.sleep_service.configure_char("On", setter_callback=self._set_sleep)

        # A5 没有负离子硬件（交接单 §3.6 真机回执：anion 恒为 0）。
        # 只建服务不看能力，就会造出一个按了没反应的假开关。
        if anion and self.profile.has_anion:
            self.anion_service = self.add_preload_service(
                "Switch", chars=["Name", "ConfiguredName"])
            self.anion_service.configure_char("Name", value="负离子")
            self.anion_service.configure_char("ConfiguredName", value="负离子")
            self.anion_service.configure_char("On", setter_callback=self._set_anion)
        elif anion:
            LOG.warning("机型 %s 无负离子硬件，忽略 AIRMX_EXPOSE_ANION", self.profile.name)

        # A5 忽略 cleanNotify（回执缺失），建了就是死开关
        if clean_notify and self.profile.has_clean_notify:
            self.clean_service = self.add_preload_service(
                "Switch", chars=["Name", "ConfiguredName"])
            self.clean_service.configure_char("Name", value="清洁提醒")
            self.clean_service.configure_char("ConfiguredName", value="清洁提醒")
            self.clean_service.configure_char("On", value=True,
                                              setter_callback=self._set_clean_notify)
        elif clean_notify:
            LOG.info("机型 %s 忽略 cleanNotify，不建清洁提醒开关", self.profile.name)

        if pir_lock:
            self.pir_service = self.add_preload_service(
                "Switch", chars=["Name", "ConfiguredName"])
            self.pir_service.configure_char("Name", value="接近感应")
            self.pir_service.configure_char("ConfiguredName", value="接近感应")
            self.pir_service.configure_char("On", value=True,
                                            setter_callback=self._set_pir_lock)

        # 电辅热只有 A5 有硬件；A3S 加了就是死开关，所以按机型能力决定
        if self.profile.has_heater:
            self.heater_service = self.add_preload_service(
                "Switch", chars=["Name", "ConfiguredName"])
            self.heater_service.configure_char("Name", value="电辅热")
            self.heater_service.configure_char("ConfiguredName", value="电辅热")
            self.heater_service.configure_char("On", value=False,
                                               setter_callback=self._set_heater)

        self._collect_names()

        # 配件设置页里能显示的信息：厂商 / 型号 / 序列号 / 固件版本
        self.set_info_service(
            manufacturer="AIRMX",
            model=self.profile.name,
            serial_number=f"{self.profile.short_name}-{self.device_id}",
            firmware_revision="unknown",
        )

    # ---------------- 可用性 ----------------

    @property
    def available(self) -> bool:
        # 设备离线时让 Home App 直接显示"无响应"，而不是展示过期数值
        return self.online and self.snapshot is not None

    def _collect_names(self) -> None:
        """登记需要纠偏的次级服务名。

        **必须排除主服务和 AccessoryInformation**：
        用户在家庭 App 里重命名配件时，iOS 会写这两个服务的名字。
        如果这里也去"纠偏"，就会每 8 秒把用户的改名强行改回去。
        只有 Switch/Temperature/Humidity/Fan 这类次级服务才需要防 iOS 的「开关 N」。
        """
        protected = {id(self.main_service)}
        try:
            protected.add(id(self.get_service("AccessoryInformation")))
        except (KeyError, ValueError):
            pass
        for serv in self.services:
            if id(serv) in protected:
                continue
            try:
                char = serv.get_characteristic("ConfiguredName")
            except (KeyError, ValueError):
                continue
            name = char.get_value()
            if name:
                self._desired_names.append((serv, name))

    def _restore_names(self) -> None:
        """iOS 会把同类型次级服务改写成「开关 N」，这里强行写回。

        是否真的能改变 Home App 的显示未知，但至少能让「它有没有改」这件事
        变得可观测——日志里出现「被改写」就说明它确实在改。
        """
        for serv, wanted in self._desired_names:
            char = serv.get_characteristic("ConfiguredName")
            if char.get_value() != wanted:
                LOG.info("id=%s 服务名被改写为 %r，恢复为 %r",
                         self.device_id, char.get_value(), wanted)
                char.set_value(wanted)

    # ---------------- 状态同步 ----------------

    async def async_refresh(self) -> None:
        try:
            payload = await self.client.state(self.device_id)
        except BridgeError as exc:
            LOG.warning("id=%s 读状态失败: %s", self.device_id, exc)
            self.online = False
            return

        if not payload or not payload.get("ok", True):
            LOG.info("id=%s 尚无上报（bridge 无缓存）", self.device_id)
            self.online = False
            return

        age = payload.get("age_seconds")
        if isinstance(age, (int, float)) and age > STALE_SECONDS:
            LOG.warning("id=%s 上报已过期 %.0f 秒，判定离线", self.device_id, age)
            self.online = False
            return

        self.online = True
        self.snapshot = snapshot = mapping.parse_state(payload)
        if time.time() < self._hold_until:
            LOG.debug("id=%s 写入进行中，暂不用快照覆盖 UI", self.device_id)
            return
        self._apply(snapshot)

    async def async_refresh_settings(self, force: bool = False) -> None:
        """设置项（pirLock/cleanNotify/powerHeat）不在 /state 里，要单独取。

        首次必取；之后每 12 轮（约 96 秒）复核一次，避免别处改过之后 UI 说谎。
        """
        self._settings_age += 1
        if not force and self._settings and self._settings_age % 12:
            self._apply_settings()
            return
        try:
            self._settings = await self.client.get_settings(self.device_id)
        except BridgeError as exc:
            LOG.debug("id=%s 读设置失败: %s", self.device_id, exc)
            return
        self._apply_settings()

    def _apply_settings(self) -> None:
        if not self._settings:
            return
        if hasattr(self, "pir_service") and "pirLock" in self._settings:
            self.pir_service.get_characteristic("On").set_value(
                1 if self._settings["pirLock"] else 0
            )
        if hasattr(self, "clean_service") and "cleanNotify" in self._settings:
            self.clean_service.get_characteristic("On").set_value(
                1 if self._settings["cleanNotify"] else 0
            )
        if hasattr(self, "heater_service") and "powerHeat" in self._settings:
            self.heater_service.get_characteristic("On").set_value(
                1 if self._settings["powerHeat"] else 0
            )

    def _in(self, name: str, value) -> None:
        """记录 HomeKit 送进来的写请求。没有这个就分不清"没发"和"没执行"。"""
        self._record("入站", {"setter": name, "value": value})

    def _record(self, kind: str, payload: dict, note: str = "") -> None:
        self._recent_calls.append({
            "时间": time.strftime("%H:%M:%S"),
            "类型": kind,
            "内容": payload,
            "备注": note,
        })

    def _apply(self, snap: Snapshot) -> None:
        m = self.main_service
        m.get_characteristic("Active").set_value(1 if snap.power else 0)
        m.get_characteristic("CurrentHumidifierDehumidifierState").set_value(
            mapping.current_state(snap)
        )
        m.get_characteristic("TargetHumidifierDehumidifierState").set_value(
            mapping.mode_to_target_state(snap.mode)
        )

        if snap.humidity is not None:
            m.get_characteristic("CurrentRelativeHumidity").set_value(snap.humidity)
            self._humidity_missing = False
        elif not getattr(self, "_humidity_missing", False):
            # humdity 是 HumidifierDehumidifier 的必需特征，不能不填；
            # A5 无内置传感器时只能停在 0%，必须说清原因，否则会被当成故障。
            self._humidity_missing = True
            LOG.warning(
                "id=%s 湿度无读数。HomeKit 会显示 0%%，"
                "请勿据此建自动化。%s",
                self.device_id,
                "该机型温湿度来自分体蓝牙计，请先配对（放在注水口中央，灯亮即成功）。"
                if self.profile.temperature_from_external_only
                else "检查设备传感器或外部分体计。",
            )
        if snap.target_humidity is not None:
            target = mapping.clamp_target_humidity(snap.target_humidity, self.profile)
            if self._pending_threshold is not None:
                if abs(target - self._pending_threshold) <= 0.5:
                    LOG.info("id=%s 目标湿度已被设备确认 %.0f%%", self.device_id, target)
                    self._pending_threshold = None
                elif time.time() < self._pending_threshold_until:
                    # 设备还没采纳，继续显示用户拖到的值
                    target = self._pending_threshold
                else:
                    LOG.warning("id=%s 目标湿度 %.0f%% 超时未被设备采纳，回落为 %.0f%%",
                                self.device_id, self._pending_threshold, target)
                    self._pending_threshold = None
            m.get_characteristic("RelativeHumidityHumidifierThreshold").set_value(target)
        pct = mapping.fan_to_percent(self.profile, snap.fan_raw)
        if pct is not None:
            m.get_characteristic("RotationSpeed").set_value(pct)
        if snap.water_level is not None:
            water_pct = mapping.water_to_percent(self.profile, snap.water_level)
            m.get_characteristic("WaterLevel").set_value(water_pct)
            self.get_service("FilterMaintenance").get_characteristic(
                "FilterLifeLevel").set_value(water_pct)
        m.get_characteristic("LockPhysicalControls").set_value(1 if snap.child_lock else 0)

        temp = None
        if self.profile.can_report_temperature:
            try:
                temp = self.get_service("TemperatureSensor")
            except (KeyError, ValueError):
                temp = None
        if temp is not None:
            if snap.temperature is not None:
                temp.get_characteristic("CurrentTemperature").set_value(snap.temperature)
            # 传感器是否在出数：没读数就让 Home App 标红，而不是继续显示旧温度
            temp.get_characteristic("StatusActive").set_value(snap.temperature is not None)

        if snap.humidity is not None:
            self.humidity_service.get_characteristic("CurrentRelativeHumidity").set_value(
                snap.humidity
            )
        self.humidity_service.get_characteristic("StatusActive").set_value(
            snap.humidity is not None
        )

        self.fan_service.get_characteristic("Active").set_value(1 if snap.power else 0)
        if pct is not None:
            self.fan_service.get_characteristic("RotationSpeed").set_value(pct)

        if snap.firmware:
            self.get_service("AccessoryInformation").get_characteristic(
                "FirmwareRevision"
            ).set_value(str(snap.firmware))

        self.get_service("FilterMaintenance").get_characteristic(
            "FilterChangeIndication"
        ).set_value(mapping.HK_FILTER_CHANGE if snap.need_clean else mapping.HK_FILTER_OK)

        if hasattr(self, "sleep_service"):
            self.sleep_service.get_characteristic("On").set_value(
                1 if snap.mode == mapping.MODE_SLEEP else 0
            )
        if hasattr(self, "anion_service"):
            self.anion_service.get_characteristic("On").set_value(1 if snap.anion else 0)

        if snap.mode != mapping.MODE_SLEEP:
            self._last_non_sleep_mode = snap.mode

        if snap.mode == mapping.MODE_MALFUNCTION:
            LOG.warning("id=%s 设备报故障态 mode=5", self.device_id)
        LOG.debug(
            "id=%s power=%s mode=%s cadr=%s h=%s t=%s water=%s",
            self.device_id, snap.power, snap.mode, snap.fan_raw,
            snap.humidity, snap.temperature, snap.water_level,
        )

    # ---------------- 可观测性 ----------------

    def status_dict(self) -> dict:
        """给 /status 用的只读快照。

        没有它的时候，任何异常都只能靠 mDNS、状态文件和用户截图反推，
        排查成本极高（2026-11-14 配对丢失就是这样查了 40 分钟）。
        """
        snap = self.snapshot
        m = self.main_service

        def _val(serv, char):
            try:
                return serv.get_characteristic(char).get_value()
            except (KeyError, ValueError, AttributeError):
                return None

        return {
            "device_id": self.device_id,
            "name": self.display_name,
            "profile": self.profile.short_name,
            "online": self.online,
            "available": self.available,
            "snapshot": None if snap is None else {
                "power": snap.power,
                "mode": snap.mode,
                "fan_raw": snap.fan_raw,
                "target_humidity": snap.target_humidity,
                "humidity": snap.humidity,
                "humidity_source": snap.humidity_source,
                "temperature": snap.temperature,
                "temperature_source": snap.temperature_source,
                "water_level": snap.water_level,
                "child_lock": snap.child_lock,
                "need_clean": snap.need_clean,
                "external_sensor_online": snap.external_sensor_online,
                "firmware": snap.firmware,
            },
            "characteristics": {
                "Active": _val(m, "Active"),
                "TargetState": _val(m, "TargetHumidifierDehumidifierState"),
                "CurrentState": _val(m, "CurrentHumidifierDehumidifierState"),
                "TargetHumidity": _val(m, "RelativeHumidityHumidifierThreshold"),
                "CurrentHumidity": _val(m, "CurrentRelativeHumidity"),
                "RotationSpeed": _val(m, "RotationSpeed"),
                "WaterLevel": _val(m, "WaterLevel"),
                "Lock": _val(m, "LockPhysicalControls"),
                "HumiditySensor": _val(self.humidity_service, "CurrentRelativeHumidity"),
                "FilterLifeLevel": _val(self.get_service("FilterMaintenance"), "FilterLifeLevel"),
                "FanActive": _val(self.fan_service, "Active"),
            },
            "pending_threshold": self._pending_threshold,
            "recent_calls": list(self._recent_calls),
            "services": [serv.display_name for serv in self.services],
            # 关键诊断：iOS 是否把我们设的 ConfiguredName 改写成了「开关 N」
            "service_names": [
                {
                    "服务": serv.display_name,
                    "Name": _val(serv, "Name"),
                    "ConfiguredName": _val(serv, "ConfiguredName"),
                }
                for serv in self.services
            ],
            "settings": self._settings,
        }

    # ---------------- 指令投递 ----------------

    def _queue(self, **fields) -> None:
        self._hold_until = time.time() + 8.0
        self._pending_control.update(fields)
        if self._control_task and not self._control_task.done():
            return
        self._control_task = asyncio.ensure_future(self._flush_control())

    async def _flush_control(self) -> None:
        await asyncio.sleep(CONTROL_DEBOUNCE)
        payload = dict(self._pending_control)
        self._pending_control.clear()
        if not payload:
            return
        try:
            await self.client.control(self.device_id, payload)
            self._record("control", payload, "成功")
            LOG.info("id=%s 下发控制 %s", self.device_id, payload)
        except BridgeError as exc:
            self._record("control", payload, f"失败: {exc}")
            LOG.error("id=%s 控制失败: %s", self.device_id, exc)
        await asyncio.sleep(RECONCILE_DELAY)
        try:
            await self.client.refresh(self.device_id)
            await asyncio.sleep(1.0)
        except BridgeError as exc:
            LOG.debug("id=%s 请求上报失败: %s", self.device_id, exc)
        self._hold_until = 0.0
        await self.async_refresh()

    def _post_settings(self, **fields) -> None:
        # 不再用 _hold_until 冻结整个配件：那会让湿度/温度一起停更 16 秒。
        # 目标湿度改由 _pending_threshold 单独保护。
        self._pending_settings.update(fields)
        if self._settings_task and not self._settings_task.done():
            return
        self._settings_task = asyncio.ensure_future(self._flush_settings())

    async def _flush_settings(self) -> None:
        await asyncio.sleep(SETTINGS_DEBOUNCE)
        payload = dict(self._pending_settings)
        self._pending_settings.clear()
        if not payload:
            return
        sent_at = time.time()
        try:
            await self.client.settings(self.device_id, payload)
            self._record("settings", payload, "成功")
            LOG.info("id=%s 下发设置 %s", self.device_id, payload)
        except BridgeError as exc:
            self._record("settings", payload, f"失败: {exc}")
            LOG.error("id=%s 设置失败: %s", self.device_id, exc)

        # 设备固件的行为：自动档下改目标湿度**不会重算风量**，必须重发一次 mode
        # 才会按新目标重新决定转速（实测 14% → 100%）。少了这一步，
        # 用 Siri 说"开到最大"就只是改了数字，风扇纹丝不动。
        if "hThreshold" in payload and self.snapshot is not None \
                and self.snapshot.mode == mapping.MODE_AUTO:
            wait = sent_at + COMPENSATE_DELAY - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            await self._bounce_auto_mode()

        await asyncio.sleep(RECONCILE_DELAY + 2)
        await self.async_refresh()

    async def _bounce_auto_mode(self) -> None:
        """强制设备重算风量。

        实测（2026-11-14，A3S）：
          · 自动档下改目标湿度 → 设备只改数字，**不重算风量**
          · 原地重发 mode=1（1→1）→ 设备**完全无视**
          · 真正的 0→1 切换 → 立刻重算（风量 100 → 14）
        所以只能走一次"手动 → 自动"的来回。中间约 11 秒设备处于手动，
        期间沿用上一次 cadr，不会停机。
        """
        try:
            await self.client.control(self.device_id, {"mode": mapping.MODE_MANUAL})
            self._record("control", {"mode": 0}, "重算补偿·切手动")
            await asyncio.sleep(COMPENSATE_DELAY)
            await self.client.control(self.device_id, {"mode": mapping.MODE_AUTO})
            self._record("control", {"mode": 1}, "重算补偿·切回自动")
            LOG.info("id=%s 已执行 0→1 重算补偿", self.device_id)
            # 兜底：万一切回自动那条被丢，设备会停在手动，再补一次
            await asyncio.sleep(8)
            await self.async_refresh()
            if self.snapshot is not None and self.snapshot.mode != mapping.MODE_AUTO:
                LOG.warning("id=%s 重算后仍非自动，重试一次", self.device_id)
                await asyncio.sleep(COMPENSATE_DELAY)
                await self.client.control(self.device_id, {"mode": mapping.MODE_AUTO})
                self._record("control", {"mode": 1}, "重算补偿·重试")
        except BridgeError as exc:
            self._record("control", {"mode": "bounce"}, f"补偿失败: {exc}")
            LOG.warning("id=%s 重算补偿失败: %s", self.device_id, exc)

    # ---------------- setter ----------------

    def _set_active(self, value) -> None:
        self._in("Active", value)
        self._queue(power=int(bool(value)))

    def _set_target_state(self, value) -> None:
        self._in("TargetState", value)
        target = int(value)
        if target == mapping.HK_TARGET_DEHUMIDIFY:
            # 本机型只会加湿；HomeKit 送来的"除湿"按手动加湿处理
            LOG.warning("id=%s HomeKit 请求除湿态，已按手动加湿处理", self.device_id)
        self._queue(mode=mapping.target_state_to_mode(target))

    def _set_target_humidity(self, value) -> None:
        self._in("TargetHumidity", value)
        clamped = mapping.clamp_target_humidity(float(value), self.profile)
        self._pending_threshold = clamped
        self._pending_threshold_until = time.time() + THRESHOLD_CONFIRM_TIMEOUT
        self.main_service.get_characteristic(
            "RelativeHumidityHumidifierThreshold"
        ).set_value(clamped)
        self._post_settings(hThreshold=int(round(clamped)))

    def _set_rotation_speed(self, value) -> None:
        self._in("RotationSpeed", value)
        raw = mapping.percent_to_fan(self.profile, float(value))
        if raw is None:
            return
        if self.profile.requires_manual_for_fan:
            # A5 在自动/睡眠模式下忽略 cadr，必须同一条指令里切手动，否则拖了没反应
            mode = self.snapshot.mode if self.snapshot else mapping.MODE_AUTO
            if mode != mapping.MODE_MANUAL:
                LOG.info("id=%s 调风量前先切手动（当前 mode=%s）", self.device_id, mode)
                self._queue(mode=mapping.MODE_MANUAL, cadr=raw)
                return
        self._queue(cadr=raw)

    def _set_lock(self, value) -> None:
        self._in("Lock", value)
        self._queue(lock=int(bool(value)))

    def _set_anion(self, value) -> None:
        self._in("Anion", value)
        self._queue(anion=int(bool(value)))

    def _set_sleep(self, value) -> None:
        self._in("Sleep", value)
        if bool(value):
            self._queue(mode=mapping.MODE_SLEEP)
        else:
            self._queue(mode=self._last_non_sleep_mode)

    def _set_clean_notify(self, value) -> None:
        self._in("CleanNotify", value)
        self._post_settings(cleanNotify=1 if bool(value) else 0)

    def _set_pir_lock(self, value) -> None:
        self._in("PirLock", value)
        self._post_settings(pirLock=1 if bool(value) else 0)

    def _set_heater(self, value) -> None:
        self._in("Heater", value)
        self._post_settings(powerHeat=1 if bool(value) else 0)
