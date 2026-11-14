"""AIRMX 机型能力表。

只写已经在真机或 APK 协议模型里得到证据的东西，其余一律标 unverified。

证据来源：
- 真机 A3（type 20）实测：power/mode/cadr/hThreshold/lock/anion/water/h0/t0 可读写；
  uv 写入恒回 1、powerHeat 恒 0、autoShakeEnable 恒 0（机型不支持）。
- APK 协议模型（gateway/airmx_gateway/protocol.py）里存在但未在真机验证的字段：
  nightLight / electrolysis / delayOffTimer / WUD / waterflood / wetFilm。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FanProfile:
    """风量区间。A3S 族是 20–100 线性，A5 族是 0–7 离散档位。

    quantize：设备会把 cadr 量化到整数倍。A3S 实测量化到**偶数**，
    所以这里必须跟着量化，否则 Home App 拖到 25% 设备回 24%，滑块会跳。
    """

    minimum: int
    maximum: int
    discrete: bool = False
    quantize: int = 1

    def _q(self, value: float) -> int:
        if self.quantize > 1:
            value = round(value / self.quantize) * self.quantize
        else:
            value = round(value)
        return int(max(self.minimum, min(self.maximum, value)))

    def to_percent(self, raw: int | None) -> float | None:
        if raw is None:
            return None
        span = max(1, self.maximum - self.minimum)
        return round(max(0.0, min(100.0, (raw - self.minimum) * 100.0 / span)), 1)

    def from_percent(self, percent: float | None) -> int | None:
        if percent is None:
            return None
        p = max(0.0, min(100.0, float(percent)))
        span = self.maximum - self.minimum
        return self._q(self.minimum + p * span / 100.0)


@dataclass(frozen=True)
class Profile:
    name: str
    model_types: tuple[int, ...]
    fan: FanProfile
    # 配件显示名后缀。两台设备同屋时必须能一眼分开，否则 Home App 里会重名。
    short_name: str = "加湿器"
    # 能力开关：只影响"是否建这个服务"，不影响已建服务的行为。
    # 依据：交接单 §3.7–3.10 双设备真机实测（2026-11-14）。
    has_anion: bool = False
    has_heater: bool = False
    # 能否报温度。注意：A5 机身**没有**内置温湿度传感器，但标配一个
    # 分体式蓝牙温湿度计（goose），配对后通过 h/t 上报房间温湿度。
    # 所以这里问的是"这台设备有没有可能给出温度读数"，不是"机身里有没有探头"。
    can_report_temperature: bool = True
    # 温度只能来自外部分体计（A5）。用于日志提示与文档，不参与服务创建。
    temperature_from_external_only: bool = False
    # A5 忽略 cleanNotify（回执缺失/回 0），做出来就是死开关。
    has_clean_notify: bool = True
    # A5 在自动模式下忽略手动风量，调风速必须先切手动。
    requires_manual_for_fan: bool = False
    # 目标湿度可用区间（A5 实测 30–70，A3S 未逐点验证）
    humidity_min: int = 30
    humidity_max: int = 80
    # uv 在协议里是"能力标志"而非可写开关：真机写 0 仍回 1。
    uv_writable: bool = False
    # 水位满量程。交接单 §3.5：water 0–120，不是 0–100。
    water_max: int = 120
    # 以下字段在 APK 协议模型里存在，但没在真机验证过写入效果。
    unverified_fields: frozenset[str] = field(default_factory=frozenset)


A3S = Profile(
    name="AirWater A3 / A3S",
    model_types=(20, 21),
    # §3.9 真机：control.cadr 0–100，百分比，量化到偶数（45→44）
    fan=FanProfile(minimum=0, maximum=100, discrete=False, quantize=2),
    short_name="A3S",
    has_anion=True,
    has_heater=False,
    can_report_temperature=True,
    has_clean_notify=True,
    requires_manual_for_fan=False,
    humidity_min=30,
    humidity_max=80,
    uv_writable=False,
    water_max=120,
    unverified_fields=frozenset({"nightLight", "electrolysis", "electrolysisLevel", "delayOffTimer"}),
)

A5 = Profile(
    name="AirWater A5",
    model_types=(10, 11),
    # §3.8 真机：control.cadr 1–7 档位，8 及以上被忽略
    fan=FanProfile(minimum=1, maximum=7, discrete=True),
    short_name="A5",
    has_anion=False,
    has_heater=True,
    # 机身无传感器（h0/t0=99999），温湿度只能来自分体蓝牙计（h/t）
    can_report_temperature=True,
    temperature_from_external_only=True,
    # cleanNotify 被设备忽略
    has_clean_notify=False,
    # 自动模式下 cadr 不生效，必须先 mode=0
    requires_manual_for_fan=True,
    humidity_min=30,
    humidity_max=70,
    uv_writable=False,
    water_max=120,
    unverified_fields=frozenset({"nightLight", "electrolysis", "electrolysisLevel", "delayOffTimer"}),
)

UNKNOWN = Profile(
    name="未知机型（按 A3S 处理）",
    model_types=(),
    fan=FanProfile(minimum=20, maximum=100, discrete=False, quantize=2),
    short_name="未知",
    # 未知机型不给任何能力开关：宁可少一个按钮，也不要多一个按不动的假开关
    has_anion=False,
    has_heater=False,
    # 没验证过的机型不承诺温度，避免挂一个永远在"不可用"的传感器
    can_report_temperature=False,
)

_BY_TYPE = {t: p for p in (A3S, A5) for t in p.model_types}


def profile_for(model_type: int | None) -> Profile:
    if model_type is None:
        return UNKNOWN
    return _BY_TYPE.get(model_type, UNKNOWN)


def capability_summary(profile: Profile) -> str:
    """给日志用的一行能力摘要，方便确认"这台设备到底暴露了什么"。"""
    bits = [profile.short_name, f"风量{profile.fan.minimum}-{profile.fan.maximum}"]
    if profile.fan.quantize > 1:
        bits.append(f"取{profile.fan.quantize}的倍数")
    bits.append("负离子" if profile.has_anion else "无负离子")
    bits.append("有加热" if profile.has_heater else "无加热")
    if profile.temperature_from_external_only:
        bits.append("温湿度来自分体计")
    if not profile.has_clean_notify:
        bits.append("cleanNotify无效")
    if profile.requires_manual_for_fan:
        bits.append("调风量需手动模式")
    return " / ".join(bits)
