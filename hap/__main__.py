"""AIRMX HomeKit 桥接进程入口。

拓扑：
    加湿器 ──HTTP/MQTT──> 网关(25886/1883) ──> bridge(25888) ──HTTP──> 本进程(HAP 51826) ──> 家庭 App

本进程只做 HomeKit 语义转换，不碰设备协议。bridge 掉线时配件显示"无响应"，
恢复后自动回归。

启动：
    python -m hap
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver

from .accessory import HumidifierAccessory
from .bridge_client import BridgeClient, BridgeError
from .profiles import capability_summary, profile_for

LOG = logging.getLogger("airmx.hap")

BRIDGE_URL = os.environ.get("AIRMX_BRIDGE_URL", "http://127.0.0.1:25888")
BRIDGE_TOKEN = os.environ.get("AIRMX_BRIDGE_TOKEN", "")
BRIDGE_TOKEN_FILE = os.environ.get("AIRMX_BRIDGE_TOKEN_FILE", "")


def _read_secret() -> str:
    """优先用文件型密钥，与本项目其它服务的 *_FILE 约定保持一致。"""
    if BRIDGE_TOKEN_FILE:
        try:
            with open(BRIDGE_TOKEN_FILE, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            LOG.error("读取 %s 失败: %s", BRIDGE_TOKEN_FILE, exc)
    return BRIDGE_TOKEN.strip()
HAP_PORT = int(os.environ.get("AIRMX_HAP_PORT", "51826"))
HAP_PINCODE = os.environ.get("AIRMX_HAP_PINCODE", "031-45-154")
HAP_STATE = os.environ.get("AIRMX_HAP_STATE", "/data/hap.state")
BRIDGE_NAME = os.environ.get("AIRMX_HAP_BRIDGE_NAME", "秒新加湿器")
# 多设备时的磁贴名前缀。实测「秒新加湿器 A3S」在首页被截断成「秒新加湿器 A…」，
# 所以默认用短前缀 + 机型，总长控制在 7 个字以内。
NAME_PREFIX = os.environ.get("AIRMX_HAP_NAME_PREFIX", "加湿器")
POLL_SECONDS = float(os.environ.get("AIRMX_HAP_POLL_SECONDS", "8"))
# 只读状态接口。容器日志页在 DSM 上打不开，没有它就只能靠猜。
STATUS_PORT = int(os.environ.get("AIRMX_HAP_STATUS_PORT", "51827"))

# ---- 界面档位 ----
# HAP 没有"把功能塞进配件设置页"的机制，每个附加功能只能是一个独立服务，
# 服务一多展开后就是一排按钮。所以提供档位，默认最省。
#   minimal  : 1 张磁贴，0 个附加按钮（睡眠/负离子/清洁提醒都不做）
#   standard : + 睡眠模式（唯一的"模式"类附加项，日常会用）
#   full     : + 负离子 + 清洁提醒开关 + 接近感应
#              （电辅热不在此列：只有 A5 有硬件，按机型能力自动决定）
PROFILE_SWITCHES = {
    "minimal": {"sleep": False, "anion": False, "clean": False, "pir": False},
    "standard": {"sleep": True, "anion": False, "clean": False, "pir": False},
    "full": {"sleep": True, "anion": True, "clean": True, "pir": True},
}
HAP_PROFILE = os.environ.get("AIRMX_HAP_PROFILE", "minimal").strip().lower()
if HAP_PROFILE not in PROFILE_SWITCHES:
    raise SystemExit(f"AIRMX_HAP_PROFILE 只支持 {sorted(PROFILE_SWITCHES)}，收到 {HAP_PROFILE!r}")


def _switches() -> dict[str, bool]:
    """档位给默认值，单独的环境变量可覆盖（空 = 不覆盖）。"""
    chosen = dict(PROFILE_SWITCHES[HAP_PROFILE])
    for env, key in (("AIRMX_EXPOSE_SLEEP", "sleep"),
                     ("AIRMX_EXPOSE_ANION", "anion"),
                     ("AIRMX_EXPOSE_CLEAN_NOTIFY", "clean"),
                     ("AIRMX_EXPOSE_PIR", "pir")):
        raw = os.environ.get(env, "").strip()
        if raw in ("0", "1"):
            chosen[key] = raw == "1"
    return chosen

DEVICE_FILTER = {
    int(x) for x in os.environ.get("AIRMX_DEVICES", "").replace(" ", "").split(",") if x.isdigit()
}


def _prepare_state_dir() -> None:
    """修正 /data 属主，然后从 root 降权到运行用户。

    pyhap 的配对信息写在 /data/hap.state。宿主目录或命名卷的属主不是
    运行用户时，写盘会静默失败，表现为「每次重启都要重新配对」。
    这里以 root 启动、chown、再降权，避免长期以 root 跑。
    """
    target = int(os.environ.get("AIRMX_HAP_UID", "10001"))
    if os.geteuid() != 0:
        return
    state_dir = os.path.dirname(HAP_STATE) or "/data"
    try:
        os.makedirs(state_dir, exist_ok=True)
        os.chown(state_dir, target, target)
        for name in os.listdir(state_dir):
            os.chown(os.path.join(state_dir, name), target, target)
        LOG.info("已修正 %s 属主为 uid=%s", state_dir, target)
    except OSError as exc:
        LOG.warning("修正 %s 属主失败: %s", state_dir, exc)
    try:
        os.setgroups([])
        os.setgid(target)
        os.setuid(target)
        LOG.info("已降权到 uid=%s", target)
    except OSError as exc:
        LOG.warning("降权失败，继续以 uid=%s 运行: %s", os.geteuid(), exc)


async def _start_status_server(mounted, token: str, paired_clients) -> object:
    """只读状态接口：GET /status（Bearer token）。"""
    from aiohttp import web

    async def handler(request):
        if token and request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response({
            "ok": True,
            "hap_port": HAP_PORT,
            "paired_clients": paired_clients(),
            "accessories": [a.status_dict() for a in mounted.values()],
        })

    async def dashboard(request):
        """给人看的看板：Home App 没有水位服务，这里补齐。

        用 URL 里的 token 鉴权，服务端渲染 + meta refresh，手机上一行书签即可。
        """
        if token and request.query.get("token") != token:
            return web.Response(text="需要 ?token=...", status=401)

        rows = []
        for acc in mounted.values():
            st = acc.status_dict()
            snap = st["snapshot"] or {}
            ch = st["characteristics"]

            def cell(v, unit=""):
                return "—" if v is None else f"{v}{unit}"

            rows.append(f"""
        <section class="card">
          <h2>{st['name']}<span class="{'on' if st['online'] else 'off'}">
            {'在线' if st['online'] else '离线'}</span></h2>
          <div class="grid">
            <div><b>{cell(snap.get('humidity'), '%')}</b><i>当前湿度</i></div>
            <div><b>{cell(snap.get('temperature'), '℃')}</b><i>温度</i></div>
            <div><b>{cell(ch.get('TargetHumidity'), '%')}</b><i>目标湿度</i></div>
            <div class="hi"><b>{cell(ch.get('WaterLevel'), '%')}</b><i>水位</i></div>
            <div><b>{cell(ch.get('RotationSpeed'), '%')}</b><i>风量</i></div>
            <div><b>{'开' if snap.get('power') else '关'}</b><i>电源</i></div>
            <div><b>{(snap.get('mode_label') or ('手动' if snap.get('mode') == 0 else '自动' if snap.get('mode') == 1 else '睡眠' if snap.get('mode') == 2 else str(snap.get('mode'))))}</b><i>模式</i></div>
            <div><b>{'是' if snap.get('need_clean') else '否'}</b><i>需清洁</i></div>
          </div>
        </section>""")

        html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5;url=/?token={token}">
<title>秒新加湿器</title>
<style>
 body{{margin:0;padding:16px;background:#0b0d10;color:#e8eaed;
      font:-apple-system,"PingFang SC",sans-serif}}
 h1{{font-size:15px;font-weight:500;color:#8b949e;margin:0 0 14px 2px}}
 .card{{background:#161a1f;border-radius:18px;padding:16px 18px;margin-bottom:14px}}
 h2{{font-size:16px;margin:0 0 14px;font-weight:600;display:flex;
     justify-content:space-between;align-items:center}}
 .on{{font-size:11px;color:#3ddc84;background:#12351f;padding:3px 9px;border-radius:20px}}
 .off{{font-size:11px;color:#ff7b72;background:#3a1a19;padding:3px 9px;border-radius:20px}}
 .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 6px}}
 .grid div{{text-align:center}}
 .grid b{{display:block;font-size:17px;font-weight:600}}
 .grid i{{display:block;font-size:11px;color:#7d8590;font-style:normal;margin-top:3px}}
 .hi b{{color:#4dd0f0}}
 footer{{color:#5c636d;font-size:11px;text-align:center;margin-top:8px}}
</style></head><body>
<h1>秒新加湿器 · 每 5 秒刷新</h1>{''.join(rows)}
<footer>水位 HomeKit 不支持，此处直接读设备数据</footer>
</body></html>"""
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", dashboard)
    app.router.add_get("/status", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", STATUS_PORT)
    await site.start()
    LOG.info("状态接口 http://0.0.0.0:%s/status", STATUS_PORT)
    return runner


async def wait_for_bridge(client: BridgeClient, attempts: int = 30) -> bool:
    for i in range(attempts):
        try:
            info = await client.health()
            LOG.info("bridge 就绪: %s", info)
            return True
        except BridgeError as exc:
            if i == 0:
                LOG.info("等待 bridge: %s", exc)
            await asyncio.sleep(2)
    LOG.error("bridge 在 %s 秒内未就绪", attempts * 2)
    return False


def attach(driver, root: Bridge, item: dict, client: BridgeClient,
           mounted: dict[int, HumidifierAccessory], multi: bool = False):
    """把一个设备挂成 HomeKit 配件；已挂载或不符合过滤条件则跳过。

    multi=True 时把机型后缀拼进名称，否则两台设备的磁贴会重名。
    """
    device_id = item.get("device_id")
    if not isinstance(device_id, int):
        return None
    if device_id in mounted:
        return None
    if DEVICE_FILTER and device_id not in DEVICE_FILTER:
        LOG.info("id=%s 不在 AIRMX_DEVICES 白名单内，跳过", device_id)
        return None

    profile = profile_for(item.get("model_type"))
    name = f"{NAME_PREFIX}{profile.short_name}" if multi else BRIDGE_NAME
    flags = _switches()
    acc = HumidifierAccessory(
        driver,
        display_name=name,
        device_id=device_id,
        client=client,
        profile=profile,
        expose_sleep=flags["sleep"],
        expose_anion=flags["anion"],
        expose_clean_notify=flags["clean"],
        expose_pir_lock=flags["pir"],
    )
    root.add_accessory(acc)
    mounted[device_id] = acc
    # ⚠ 这里绝对不能调 config_changed()：它会把内存里的默认 state（含随机 MAC）
    # 立刻写盘，而 driver.async_start() 才去读盘 —— 等于每次启动都覆盖掉已保存的
    # 配对信息，表现为"重启后 iOS 全部无响应、必须重新配对"。
    # 配置变更的通知统一放在 async_start() 之后做。
    LOG.info("已挂载 id=%s 型号=%s →「%s」能力：%s",
             device_id, item.get("model_type"), name, capability_summary(profile))
    LOG.info("    档位=%s 附加开关=%s", HAP_PROFILE,
             [k for k, v in flags.items() if v] or "无")
    return acc


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AIRMX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _prepare_state_dir()

    async with BridgeClient(BRIDGE_URL, _read_secret()) as client:
        await wait_for_bridge(client)

        # 必须显式传入当前 loop：否则 pyhap 会自建一个 loop，
        # setter 回调与轮询任务会跨循环执行（表现为 "attached to a different loop"）。
        driver = AccessoryDriver(
            port=HAP_PORT,
            persist_file=HAP_STATE,
            pincode=HAP_PINCODE.encode(),
            advertise=True,
            loop=asyncio.get_running_loop(),
        )
        root = Bridge(driver, BRIDGE_NAME)
        mounted: dict[int, HumidifierAccessory] = {}

        try:
            devices = await client.devices()
        except BridgeError as exc:
            LOG.error("读取设备列表失败: %s", exc)
            devices = []

        # 按 device_id 排序，保证多设备时 AID 分配稳定（顺序变了 HomeKit 会当成新配件）
        devices = sorted(devices, key=lambda d: d.get("device_id") or 0)
        multi = len(devices) > 1
        for item in devices:
            attach(driver, root, item, client, mounted, multi=multi)

        if not mounted:
            LOG.warning("当前没有可用设备；配网/注册完成后重启本容器即可挂载")

        driver.add_accessory(root)
        await driver.async_start()
        # 启动完成（state 已从磁盘加载）之后再通知配置变更，这样 c# 会 +1
        # 让 iOS 重新拉取配件描述，同时不会破坏已加载的配对信息。
        driver.config_changed()
        LOG.info("HAP 已启动 port=%s pincode=%s accessories=%s",
                 HAP_PORT, HAP_PINCODE, len(mounted))

        status_runner = await _start_status_server(
            mounted, _read_secret(), lambda: len(driver.state.paired_clients or {})
        )

        stop = asyncio.Event()

        async def poll_loop() -> None:
            while not stop.is_set():
                try:
                    current = sorted(await client.devices(), key=lambda d: d.get("device_id") or 0)
                    for item in current:
                        if attach(driver, root, item, client, mounted,
                                  multi=len(current) > 1):
                            # config_changed 内部已经 persist，不需要再单独写一次
                            driver.config_changed()
                except BridgeError as exc:
                    LOG.debug("设备列表刷新失败: %s", exc)

                for acc in list(mounted.values()):
                    try:
                        await acc.async_refresh()
                        await acc.async_refresh_settings()
                    except Exception as exc:  # 单台设备异常不能拖垮整体
                        LOG.exception("id=%s 刷新异常: %s", acc.device_id, exc)

                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass

        poller = asyncio.create_task(poll_loop())

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass

        await stop.wait()
        poller.cancel()
        await status_runner.cleanup()
        await driver.async_stop()


if __name__ == "__main__":
    asyncio.run(main())
