import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from .mqtt import Observer
from .server import Config, make_admin_app, make_device_app
from .store import Store


def load_config():
    token = Path(os.environ.get("AIRMX_TOKEN_FILE", "/run/secrets/admin_token")).read_text().strip()
    master = Path(os.environ.get("AIRMX_MASTER_KEY_FILE", "/run/secrets/master_key")).read_bytes().strip()
    config = Config(admin_token=token,
                    device_allowed_cidrs=tuple(x.strip() for x in os.environ.get("AIRMX_DEVICE_CIDRS", "").split(",") if x.strip()),
                    bind_host=os.environ.get("AIRMX_BIND_HOST", "127.0.0.1"),
                    device_port=int(os.environ.get("AIRMX_DEVICE_PORT", "8080")),
                    admin_port=int(os.environ.get("AIRMX_ADMIN_PORT", "8086")),
                    mqtt_host=os.environ.get("AIRMX_MQTT_HOST", "127.0.0.1"),
                    mqtt_port=int(os.environ.get("AIRMX_MQTT_PORT", "1883")))
    return config, master


async def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    config, master = load_config()
    store = Store(os.environ.get("AIRMX_DATA_DIR", "/data"), master)
    observer = Observer(store, config.mqtt_host, config.mqtt_port)
    runners = []
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    try:
        for app, port in ((make_device_app(store, config), config.device_port),
                          (make_admin_app(store, config), config.admin_port)):
            runner = web.AppRunner(app, access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, config.bind_host, port).start()
        observer.start()
        logging.getLogger("airmx_gateway").info("diagnostic_gateway_started; controls_disabled")
        await stopped.wait()
    finally:
        await observer.stop()
        for runner in reversed(runners):
            await runner.cleanup()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
