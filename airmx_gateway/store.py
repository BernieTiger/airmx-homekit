"""SQLite state with encrypted device credentials. Registration is deny-by-default."""
import ipaddress
import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path

from cryptography.fernet import Fernet

from .protocol import SETTING_FIELDS, IntakeError, display_state, normalize_mac, registration_identity, strict_json, topic_device_id, verify_report, wire_mac_from_wifi

# 允许注册的设备数量上限。A3S + A5 + 余量，可用环境变量调整。
DEVICE_CAPACITY = max(1, int(os.environ.get("AIRMX_DEVICE_CAPACITY", "4")))


class Store:
    def __init__(self, directory: str | Path, master_key: bytes, clock=time.time, monotonic=time.monotonic):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError("data_directory_symlink_denied")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_mode & 0o077:
            raise ValueError("data_directory_must_be_private")
        if (self.directory / "gateway.sqlite3").is_symlink():
            raise ValueError("database_symlink_denied")
        self.clock = clock
        self.monotonic = monotonic
        self.clock_baseline = (clock(), monotonic())
        self.received_monotonic = {}
        self.clock_unverified = False
        self.fernet = Fernet(master_key)
        self.session_id = uuid.uuid4().hex
        self.broker_connected = False
        self.counters = Counter()
        self.window = None
        self.db = sqlite3.connect(self.directory / "gateway.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS devices (
                wifi_mac TEXT PRIMARY KEY, wire_mac TEXT NOT NULL,
                device_id INTEGER UNIQUE NOT NULL, model_type INTEGER NOT NULL,
                key_cipher BLOB NOT NULL, peer_ip TEXT NOT NULL, registered_at INTEGER NOT NULL,
                status_json TEXT, settings_json TEXT, status_received INTEGER,
                settings_received INTEGER, device_time INTEGER, session_id TEXT,
                last_digest TEXT, settings_device_time INTEGER, settings_digest TEXT
            );
            CREATE TABLE IF NOT EXISTS seen_reports (
                device_id INTEGER NOT NULL, digest TEXT NOT NULL, received_at INTEGER NOT NULL,
                PRIMARY KEY (device_id, digest)
            );
        """)
        os.chmod(self.directory / "gateway.sqlite3", 0o600)
        # Fail rather than replace keys if the wrong master key is supplied.
        try:
            for row in self.db.execute("SELECT key_cipher FROM devices"):
                self.fernet.decrypt(row["key_cipher"])
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _clock_ok(self):
        wall, mono = self.clock_baseline
        if abs((self.clock() - wall) - (self.monotonic() - mono)) > 10:
            self.clock_unverified = True
            self.window = None
            self.received_monotonic.clear()
        return not self.clock_unverified

    def connection_changed(self, connected: bool):
        self.broker_connected = connected
        self.session_id = uuid.uuid4().hex
        self.received_monotonic.clear()

    def open_window(self, entries: list, duration_seconds: int):
        if not self._clock_ok():
            raise IntakeError("host_clock_unverified")
        if type(duration_seconds) is not int or not 30 <= duration_seconds <= 600:
            raise IntakeError("invalid_window_duration")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
            raise IntakeError("invalid_enrollment_list")
        allowed = {}
        for entry in entries:
            if not isinstance(entry, dict) or not {"wifi_mac", "model_type"} <= set(entry) <= {"wifi_mac", "model_type", "peer_ip"}:
                raise IntakeError("invalid_enrollment_entry")
            model = entry["model_type"]
            if type(model) is not int or model not in (11, 20):
                raise IntakeError("model_profile_unverified")
            # peer_ip 改为可选：容器内看到的来源 IP 会随 Docker 网段变化，
            # 精确绑定会导致网络重建后重新配对失败。只要在 AIRMX_DEVICE_CIDRS 内即可。
            peer_ip = entry.get("peer_ip")
            if peer_ip is not None:
                if not isinstance(peer_ip, str):
                    raise IntakeError("invalid_peer_ip")
                try:
                    peer = ipaddress.ip_address(peer_ip)
                except (ValueError, TypeError):
                    raise IntakeError("invalid_peer_ip") from None
                if peer.version != 4 or not peer.is_private or peer.is_unspecified or peer.is_loopback:
                    raise IntakeError("invalid_peer_ip")
                peer_ip = str(peer)
            # "*" 表示接受该型号的任意设备，用于新设备首次配对
            if entry["wifi_mac"] == "*":
                key = f"*:{model}"
                if key in allowed:
                    raise IntakeError("duplicate_enrollment_mac")
                allowed[key] = {"model_type": model, "peer_ip": peer_ip}
                continue
            mac = normalize_mac(entry["wifi_mac"])
            if mac in allowed:
                raise IntakeError("duplicate_enrollment_mac")
            # Do not enroll a different physical MAC with the same wire ID.
            _, device_id = registration_identity(wire_mac_from_wifi(mac))
            row = self.db.execute("SELECT wifi_mac FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if row and row["wifi_mac"] != mac:
                raise IntakeError("device_id_collision")
            allowed[mac] = {"model_type": model, "peer_ip": peer_ip}
        ids = [registration_identity(wire_mac_from_wifi(mac))[1] for mac in allowed if not mac.startswith("*:")]
        if len(ids) != len(set(ids)):
            raise IntakeError("device_id_collision")
        self.window = {"expires_at": int(self.clock()) + duration_seconds,
                       "deadline": self.monotonic() + duration_seconds, "remaining": allowed}
        return self.window_status()

    def window_status(self):
        if not self._clock_ok() or not self.window or self.monotonic() >= self.window["deadline"]:
            self.window = None
            return {"open": False, "remaining": 0}
        return {"open": True, "expires_at": self.window["expires_at"], "remaining": len(self.window["remaining"])}

    def close_window(self):
        self.window = None

    def delete_device(self, device_id: int):
        """删除一台设备（腾出容量或清理测试设备）。密钥一并从库中移除。"""
        if type(device_id) is not int:
            raise IntakeError("invalid_device_id")
        row = self.db.execute("SELECT wifi_mac FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not row:
            raise IntakeError("device_not_found")
        with self.db:
            self.db.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
            self.db.execute("DELETE FROM seen_reports WHERE device_id=?", (device_id,))
        self.counters["devices_deleted"] += 1
        return {"deleted": device_id, "wifi_mac_hint": "**:**:**:**:" + row["wifi_mac"][-5:-3] + ":" + row["wifi_mac"][-2:]}

    def register(self, params: dict, peer_ip: str):
        if not self.window_status()["open"]:
            raise IntakeError("enrollment_closed")
        if not isinstance(params, dict):
            raise IntakeError("invalid_registration")
        wifi_mac, device_id = registration_identity(params.get("mac"))
        allowed = self.window["remaining"].get(wifi_mac)
        if not allowed:
            # 通配条目：允许该型号的任意设备（新设备首次配对时不必预先知道 MAC）
            model = params.get("type")
            allowed = self.window["remaining"].get(f"*:{model}") if type(model) is int else None
        if not allowed:
            raise IntakeError("device_not_allowed")
        if allowed["peer_ip"] and allowed["peer_ip"] != peer_ip:
            raise IntakeError("peer_not_allowed")
        model = params.get("type")
        if type(model) is not int or model != allowed["model_type"]:
            raise IntakeError("model_mismatch")
        key = params.get("key")
        if not isinstance(key, str) or not key.strip() or len(key.encode("utf-8")) > 256:
            raise IntakeError("invalid_device_key")
        existing = self.db.execute("SELECT * FROM devices WHERE device_id=? OR wifi_mac=?", (device_id, wifi_mac)).fetchone()
        if existing:
            if existing["wifi_mac"] != wifi_mac:
                raise IntakeError("device_id_collision")
            # 2026-11-13 真机验证：AirWater A3 每次 genId 都带一个全新的随机 key，
            # 原先固定 key 的做法会让设备重启后被 existing_identity_change_denied 永久拒绝。
            # 登记窗口已经限定了精确 MAC + 型号 + 来源 IP，因此在这里安全地接受 key 轮换。
            encrypted = self.fernet.encrypt(key.encode("utf-8"))
            with self.db:
                self.db.execute(
                    "UPDATE devices SET key_cipher=?, model_type=?, peer_ip=?, registered_at=? WHERE device_id=?",
                    (encrypted, model, peer_ip, int(self.clock()), device_id),
                )
        elif self.db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= DEVICE_CAPACITY:
            raise IntakeError("device_capacity_reached")
        else:
            for other in self.db.execute("SELECT key_cipher FROM devices"):
                if self.fernet.decrypt(other["key_cipher"]).decode("utf-8") == key:
                    raise IntakeError("shared_device_key_denied")
            encrypted = self.fernet.encrypt(key.encode("utf-8"))
            with self.db:
                self.db.execute("INSERT INTO devices(wifi_mac,wire_mac,device_id,model_type,key_cipher,peer_ip,registered_at) VALUES(?,?,?,?,?,?,?)",
                                (wifi_mac, normalize_mac(params["mac"]), device_id, model, encrypted, peer_ip, int(self.clock())))
        self.counters["registrations_accepted"] += 1
        # Keep exact identity eligible until timeout to permit a lost HTTP response retry.
        return device_id, model

    def device_key(self, device_id: int) -> str:
        """诊断用途：取回设备注册密钥（供本机桥接服务签名下发指令）。"""
        if type(device_id) is not int:
            raise IntakeError("invalid_device_id")
        row = self.db.execute("SELECT key_cipher FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if not row:
            raise IntakeError("device_not_found")
        return self.fernet.decrypt(row["key_cipher"]).decode("utf-8")

    def receive(self, topic: str, payload: bytes, retained: bool = False):
        try:
            if not self._clock_ok():
                raise IntakeError("host_clock_unverified")
            if retained:
                raise IntakeError("retained_report_ignored")
            device_id = topic_device_id(topic)
            row = self.db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if not row:
                raise IntakeError("unregistered_device")
            key = self.fernet.decrypt(row["key_cipher"]).decode("utf-8")
            report = verify_report(topic, payload, key, int(self.clock()))
            if self.db.execute("SELECT 1 FROM seen_reports WHERE device_id=? AND digest=?", (device_id, report.digest)).fetchone():
                raise IntakeError("duplicate_or_old_report")
            previous_time = row["device_time"] if report.command_id == 1001 else row["settings_device_time"]
            if previous_time is not None and report.device_time < previous_time:
                raise IntakeError("duplicate_or_old_report")
            with self.db:
                self.db.execute("DELETE FROM seen_reports WHERE received_at<?", (int(self.clock()) - 600,))
                if self.db.execute("SELECT COUNT(*) FROM seen_reports").fetchone()[0] >= 10000:
                    raise IntakeError("report_dedup_capacity_reached")
                self.db.execute("INSERT INTO seen_reports VALUES(?,?,?)", (device_id, report.digest, int(self.clock())))
            if report.command_id == 1001:
                with self.db:
                    self.db.execute("UPDATE devices SET status_json=?,status_received=?,device_time=?,session_id=?,last_digest=? WHERE device_id=?",
                                    (json.dumps(report.fields, separators=(",", ":")), int(self.clock()), report.device_time, self.session_id, report.digest, device_id))
                self.received_monotonic[device_id] = self.monotonic()
            else:
                with self.db:
                    self.db.execute("UPDATE devices SET settings_json=?,settings_received=?,settings_device_time=?,settings_digest=? WHERE device_id=?",
                                    (json.dumps(report.fields, separators=(",", ":")), int(self.clock()), report.device_time, report.digest, device_id))
            self.counters["verified_status" if report.command_id == 1001 else "verified_settings"] += 1
            return {"accepted": True, "command_id": report.command_id}
        except IntakeError as exc:
            self.counters[exc.code] += 1
            return {"accepted": False, "reason": exc.code}

    def devices(self):
        output = []
        for row in self.db.execute("SELECT * FROM devices ORDER BY registered_at,device_id"):
            stamp = row["status_received"]
            age = int(self.clock()) - stamp if stamp is not None else None
            elapsed = self.monotonic() - self.received_monotonic.get(row["device_id"], float("-inf"))
            device_age = int(self.clock()) - row["device_time"] if row["device_time"] is not None else None
            fresh = bool(self._clock_ok() and row["session_id"] == self.session_id and
                         0 <= elapsed < 30 and device_age is not None and -5 <= device_age < 30)
            fields = strict_json(row["status_json"]) if row["status_json"] else {}
            settings = strict_json(row["settings_json"]) if row["settings_json"] else {}
            state = "online" if self.broker_connected and fresh else ("awaiting_report" if stamp is None else "stale")
            output.append({
                "device_id": row["device_id"], "model_type": row["model_type"],
                "model_label": {11: "A5 (type 11)", 20: "A3/A3S family (type 20)", 21: "A3S V2/Iris family (type 21)"}[row["model_type"]],
                "mac_hint": "**:**:**:**:" + row["wifi_mac"][-4:-2] + ":" + row["wifi_mac"][-2:],
                "state": state, "signature_verified": stamp is not None,
                "fresh_in_current_session": fresh, "last_report_received_at": stamp,
                "report_age_seconds": age, "device_time": row["device_time"],
                "status": fields, "display": display_state(fields),
                "settings": settings, "settings_received_at": row["settings_received"],
                "settings_complete": set(settings) >= SETTING_FIELDS,
                "control_enabled": False,
            })
        return output
