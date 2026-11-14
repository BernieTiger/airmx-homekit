"""Read-only observer with bounded buffering at the paho thread ingress."""
import asyncio
import logging
import threading
from collections import Counter, deque

import paho.mqtt.client as mqtt

LOG = logging.getLogger("airmx_gateway")


class Observer:
    def __init__(self, store, host: str, port: int):
        self.store = store
        self.loop = asyncio.get_running_loop()
        self.pending = deque()
        self.lock = threading.Lock()
        self.dropped = Counter()
        self.drain_scheduled = False
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="airmx-diag-observer", clean_session=True,
                                  protocol=mqtt.MQTTv311, reconnect_on_failure=True)
        self.client.username_pw_set("gateway-observer")
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._connected
        self.client.on_disconnect = self._disconnected
        self.client.on_message = self._message
        self.host, self.port = host, port
        self.running = False

    def _connected(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe("airwater/01/0/1/1/+", qos=1)
        self._push(("connection", not reason_code.is_failure))

    def _disconnected(self, client, userdata, flags, reason_code, properties):
        self._push(("connection", False))

    def _message(self, client, userdata, message):
        if len(message.payload) > 16384:
            self._push(None, "payload_too_large")
        else:
            self._push(("report", message.topic, bytes(message.payload), bool(message.retain)))

    def _push(self, event, error=None):
        with self.lock:
            if not self.running or self.loop.is_closed():
                return
            if error:
                self.dropped[error] += 1
            elif len(self.pending) >= 128:
                self.dropped["intake_queue_full"] += 1
            else:
                self.pending.append(event)
            if not self.drain_scheduled:
                self.drain_scheduled = True
                self.loop.call_soon_threadsafe(self._drain)

    def _drain(self):
        with self.lock:
            batch = [self.pending.popleft() for _ in range(min(16, len(self.pending)))]
            counts = self.dropped.copy()
            self.dropped.clear()
        self.store.counters.update(counts)
        for event in batch:
            if not self.running:
                return
            if event[0] == "connection":
                self.store.connection_changed(event[1])
                self.store.counters["broker_connected" if event[1] else "broker_disconnected"] += 1
            else:
                try:
                    self.store.receive(*event[1:])
                except Exception:
                    self.store.counters["intake_internal_error"] += 1
                    LOG.error("intake_failed")
        with self.lock:
            if self.pending or self.dropped:
                self.loop.call_soon(self._drain)
            else:
                self.drain_scheduled = False

    def start(self):
        self.running = True
        self.client.connect_async(self.host, self.port, keepalive=20)
        self.client.loop_start()

    async def stop(self):
        self.running = False
        self.client.disconnect()
        await asyncio.to_thread(self.client.loop_stop)
        self.store.connection_changed(False)
        with self.lock:
            self.pending.clear()
            self.dropped.clear()
