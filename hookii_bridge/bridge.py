"""Hookii Neomow → local MQTT bridge.

Background: as of May 2026 Hookii migrated their cloud IoT from the
plain ``hookii/details/device/<serial>`` topic format on their public
broker to a JWT-gated push protocol on ``iot.beta.hookii.com:8883``.
The new server gates STATUS pushes on a live heartbeat from the client
carrying the user's REST-issued JWT. A passive MQTT subscriber sees
nothing.

This bridge keeps that heartbeat alive per (user-account, mower-serial)
pair, normalises the cloud's STATUS payload back to the legacy field
shape, and republishes it to a LOCAL MQTT broker on the OLD topic
format so any existing Home Assistant template-sensors, automations
or n8n flows that read ``hookii/details/device/<serial>`` keep working
without modification.

It is plain Python + paho-mqtt + requests with no host-specific
dependencies; it runs equally fine as:

  * a Home Assistant Supervisor add-on (the ``hookii-bridge-ha-addon``
    repo wraps this script with a bashio run.sh that reads
    ``/data/options.json`` and exports the same env-vars listed below);
  * a systemd service on Linux / a Windows Service / Docker Compose
    workload pointed at any local Mosquitto / EMQX / RabbitMQ
    broker that speaks MQTT 3.1.1.

Configuration via env:
    HOOKII_ACCOUNTS   "<label>:<email>:<pw>;..." - semicolon-separated
                      account specs. Label is free-form (used for log
                      lines + per-account dedup). Most users have one
                      account; supply just one spec without semicolons.
                      Password may be cleartext OR a 32-char uppercase
                      MD5 hash (auto-detected).
    HOOKII_REST_HOST  default iot.beta.hookii.com:10443
    HOOKII_MQTT_HOST  default iot.beta.hookii.com:8883
    HOOKII_MQTT_USER  default hookii-iot (shared static, same as the
                      official Hookii mobile app uses)
    HOOKII_MQTT_PASS  default ukLWdAbvRF3JVqNyTdAVJsMx (ditto)
    HOOKII_MODEL      default 0002 (Pro). The bridge auto-learns the
                      actual model code per serial from the first
                      observed STATUS push, so this rarely needs
                      overriding.

    LOCAL_MQTT_HOST   default 127.0.0.1
    LOCAL_MQTT_PORT   default 1883
    LOCAL_MQTT_USER   required - an MQTT user on your local broker that
                      can publish to LOCAL_TOPIC_FMT
    LOCAL_MQTT_PASS   required - that user's password
    LOCAL_TOPIC_FMT   default "hookii/details/device/{serial}" -
                      the placeholder {serial} is interpolated per
                      mower; preserves the legacy HA-wiring topic.

    HEARTBEAT_SEC     default 15
    LOG_LEVEL         default INFO. Set to DEBUG only when
                      troubleshooting - it logs every inbound topic.

    HOOKII_SERIALS_<LABEL>
                      Optional override of the device-list returned by
                      REST login. Example for label "addon":
                        HOOKII_SERIALS_ADDON=HKX1...,HKX2...
                      Useful because Hookii's login response doesn't
                      always enumerate devices reliably.
"""
from __future__ import annotations

import hashlib
import re
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import paho.mqtt.client as mqtt
import requests


LOG = logging.getLogger("hookii-bridge")


# Headers the Hookii Android app sends on EVERY request (login + post-auth).
# Reverse-engineered 2026-05-28 from pcap; missing any → server returns
# {"code":2, "msg":"hookii-agent参数错误", "data":null}.
#
# - hookii-token: literal "Hookii " before login (note: trailing space).
#                 After login it becomes "Hookii <JWT>" - "Hookii " prefix
#                 instead of the conventional "Bearer ".
# - hookii-agent: free-form fingerprint format
#                 "Android/<manufacturer> <model> <android-version>/V<app-ver>/<build>".
#                 Server seems to require the prefix shape, exact content
#                 less important - we send a plausible Xiaomi value.
# - app-time-zone-offset: minutes vs UTC.
# - user-agent: Dart/3.9 (dart:io) - Flutter HTTP-client default.
HOOKII_USER_AGENT = "Dart/3.9 (dart:io)"
HOOKII_AGENT = os.environ.get(
    "HOOKII_AGENT",
    "Android/Xiaomi 25010PN30G 16/V1.1.0/189",
)
HOOKII_APP_NAME = os.environ.get("HOOKII_APP_NAME", "Hookii App")
HOOKII_APP_LANG = os.environ.get("HOOKII_APP_LANG", "en")


def _tz_offset_minutes() -> int:
    """Local TZ offset in minutes vs UTC."""
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


def hookii_headers(token: str = "") -> dict[str, str]:
    """Build the standard Hookii request header set. `token` is the JWT from
    /user/login/email - empty before login (header still required as
    'Hookii ' with trailing space)."""
    return {
        "hookii-token": f"Hookii {token}" if token else "Hookii ",
        "user-agent": HOOKII_USER_AGENT,
        "hookii-agent": HOOKII_AGENT,
        "accept-encoding": "gzip",
        "app-time-zone-offset": str(_tz_offset_minutes()),
        "content-type": "application/json",
        "app-language": HOOKII_APP_LANG,
        "app-name": HOOKII_APP_NAME,
    }


_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def normalise_status(payload: dict) -> None:
    """Mutate a STATUS payload so both protocol shapes look identical to HA.

    The new Hookii cloud alternates between two STATUS variants:

    - **Shape A** (legacy, "full"): flat fields the existing HA template
      sensors were written against - `electricity`, `chargeCurrent`,
      `knifeDiscMotorTemp`, motor temps at the top of `data.STATUS`.

    - **Shape B** (newer, "compact"): `battery` replaces `electricity`,
      `workTimeStatusInfo.workStatus` replaces top-level `workStatus`,
      `chargeDischargeCurrent` replaces split charge/discharge currents,
      and the chassis sensor fan-out lives inside `data.STATUS.chassisData`.

    HA's templates read Shape A's flat layout; on Shape B they fall through
    to "previous state" and the dashboard appears to freeze or revert. This
    normaliser maps Shape B back to Shape A *in addition to* leaving the
    original fields in place, so nothing breaks for downstream consumers
    that may already understand the new names.
    """
    status = (payload.get("data") or {}).get("STATUS")
    if not isinstance(status, dict):
        return

    # 1. chassisData fan-out: copy nested fields to top of STATUS so the
    #    existing motor-temp / knife-speed templates resolve.
    chassis = status.get("chassisData")
    if isinstance(chassis, dict):
        for k, v in chassis.items():
            status.setdefault(k, v)

    # 2. battery (Shape B) -> electricity (Shape A).
    if "battery" in status and "electricity" not in status:
        status["electricity"] = status["battery"]

    # 3. workTimeStatusInfo.workStatus -> top-level workStatus.
    wtsi = status.get("workTimeStatusInfo")
    if isinstance(wtsi, dict) and "workStatus" not in status:
        ws = wtsi.get("workStatus")
        if ws is not None:
            status["workStatus"] = ws

    # 4. chargeDischargeCurrent (Shape B) -> chargeCurrent/dischargeCurrent.
    #    Sign convention from the legacy templates: c > 0 == charging,
    #    c < 0 == mowing (see work_status template). chargeDischargeCurrent
    #    appears to follow the same convention so we copy through unchanged.
    if "chargeDischargeCurrent" in status:
        if "chargeCurrent" not in status:
            status["chargeCurrent"] = status["chargeDischargeCurrent"]
        if "dischargeCurrent" not in status:
            status["dischargeCurrent"] = status["chargeDischargeCurrent"]

    # 5. fourGSignal (Shape B) -> networkSignal (Shape A) as a best-effort
    #    proxy. Note: 4G and WiFi can both be present, so we don't touch
    #    wifiSignal which Shape B already publishes.
    if "fourGSignal" in status and "networkSignal" not in status:
        status["networkSignal"] = status["fourGSignal"]


def md5_upper(s: str) -> str:
    """Hookii password hash: MD5(cleartext) upper-cased hex.

    If `s` already looks like a 32-char hex MD5 digest, treat it as
    already-hashed and just upper-case it. This lets users who'd rather
    not store their cleartext password supply the MD5-uppercased digest
    directly via HOOKII_ACCOUNTS - the bridge auto-detects and skips
    the re-hashing step.
    """
    if _HEX32.match(s):
        return s.upper()
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


@dataclass
class HookiiAccount:
    """One Hookii REST account. Owns 1+ mowers."""
    label: str
    email: str
    password: str
    jwt: str | None = None
    app_user_id: str | None = None
    serials: list[str] = field(default_factory=list)


@dataclass
class Config:
    accounts: list[HookiiAccount]
    rest_host: str
    rest_port: int
    cloud_host: str
    cloud_port: int
    cloud_user: str
    cloud_pass: str
    model: str
    local_host: str
    local_port: int
    local_user: str
    local_pass: str
    local_topic_fmt: str
    heartbeat_sec: int


def parse_config() -> Config:
    raw = os.environ.get("HOOKII_ACCOUNTS", "").strip()
    if not raw:
        LOG.fatal("HOOKII_ACCOUNTS env var is required")
        sys.exit(2)
    accounts: list[HookiiAccount] = []
    for spec in raw.split(";"):
        spec = spec.strip()
        if not spec:
            continue
        # label:email:password
        try:
            label, email, password = spec.split(":", 2)
        except ValueError:
            LOG.fatal("HOOKII_ACCOUNTS entry %r is malformed (need label:email:password)", spec)
            sys.exit(2)
        # Hookii's beta REST user lookup is case-sensitive: a user registered
        # as Foo@bar.dk fails with code:5 ("user not registered") when sent
        # as `Foo@…` and succeeds as `foo@…`. Lower-case at parse so config
        # writers don't have to care about original-registration casing -
        # same defensive UX as md5_upper's auto-detection of pre-hashed pw.
        accounts.append(HookiiAccount(label=label.strip(), email=email.strip().lower(), password=password))

    rest = os.environ.get("HOOKII_REST_HOST", "iot.beta.hookii.com:10443").split(":")
    cloud = os.environ.get("HOOKII_MQTT_HOST", "iot.beta.hookii.com:8883").split(":")

    return Config(
        accounts=accounts,
        rest_host=rest[0],
        rest_port=int(rest[1]),
        cloud_host=cloud[0],
        cloud_port=int(cloud[1]),
        cloud_user=os.environ.get("HOOKII_MQTT_USER", "hookii-iot"),
        cloud_pass=os.environ.get("HOOKII_MQTT_PASS", "ukLWdAbvRF3JVqNyTdAVJsMx"),
        model=os.environ.get("HOOKII_MODEL", "0002"),
        local_host=os.environ.get("LOCAL_MQTT_HOST", "127.0.0.1"),
        local_port=int(os.environ.get("LOCAL_MQTT_PORT", "1883")),
        local_user=os.environ.get("LOCAL_MQTT_USER", ""),
        local_pass=os.environ.get("LOCAL_MQTT_PASS", ""),
        local_topic_fmt=os.environ.get("LOCAL_TOPIC_FMT", "hookii/details/device/{serial}"),
        heartbeat_sec=int(os.environ.get("HEARTBEAT_SEC", "15")),
    )


# ---------------------------------------------------------------------------
# REST login
# ---------------------------------------------------------------------------

def hookii_login(cfg: Config, acct: HookiiAccount) -> None:
    """Refresh acct.jwt + acct.serials by hitting /api/v1/user/login/email.

    Server expects email + MD5-upper(password). Returns JWT (used as bearer
    + embedded in MQTT heartbeats) plus a list of devices bound to the user.
    """
    url = f"https://{cfg.rest_host}:{cfg.rest_port}/api/v1/user/login/email"
    body = {"email": acct.email, "password": md5_upper(acct.password)}
    LOG.info("[%s] login: POST %s", acct.label, url)
    r = requests.post(url, json=body, headers=hookii_headers(token=""), timeout=15)
    r.raise_for_status()
    data = r.json()
    # The API wraps payload in {"code", "msg", "data"} - peel it.
    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(payload, dict):
        raise RuntimeError(f"[{acct.label}] unexpected login response shape: {data!r}")
    acct.jwt = payload.get("token") or payload.get("jwt")
    if not acct.jwt:
        raise RuntimeError(f"[{acct.label}] login response missing token: keys={list(payload.keys())}")
    acct.app_user_id = str(payload.get("appUserId") or payload.get("userId") or "")
    # Devices may be in payload.deviceList / payload.devices. Best-effort.
    devs = payload.get("deviceList") or payload.get("devices") or []
    serials: list[str] = []
    for d in devs:
        sn = d.get("deviceSn") or d.get("sn") or d.get("serial")
        if sn:
            serials.append(sn)
    if serials:
        acct.serials = serials
        LOG.info("[%s] login OK, jwt-len=%d, serials=%s", acct.label, len(acct.jwt), serials)
    else:
        LOG.info("[%s] login OK, jwt-len=%d, no device-list in payload - fetch separately or hardcode via HOOKII_SERIALS_%s",
                 acct.label, len(acct.jwt), acct.label.upper())


# ---------------------------------------------------------------------------
# Cloud MQTT client (one per account)
# ---------------------------------------------------------------------------

class AccountClient:
    """One MQTT client to Hookii cloud per user account. Handles:

    - connect with shared hookii-iot/static-pass creds
    - subscribe to hk/server/mower/push/<model>/<serial> for each device
    - heartbeat thread that publishes hk/app/mower/hb/... every N seconds
    - republish inbound STATUS payloads to the LOCAL broker on the legacy
      topic so existing HA wiring keeps working
    """

    def __init__(self, cfg: Config, acct: HookiiAccount, local_client: mqtt.Client):
        self.cfg = cfg
        self.acct = acct
        self.local = local_client
        self.client_id = f"Android_{acct.email}_{int(time.time() * 1000)}"
        self.push_counter = 1
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None
        self._hb_thread: threading.Thread | None = None
        # Per-serial model code learned from observed STATUS topics. Default
        # to cfg.model until we see the first push for that serial.
        self._serial_model: dict[str, str] = {sn: self.cfg.model for sn in self.acct.serials}

    # ---- MQTT lifecycle ------------------------------------------------

    def start(self) -> None:
        c = mqtt.Client(
            client_id=self.client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        c.username_pw_set(self.cfg.cloud_user, self.cfg.cloud_pass)
        # TLS - Hookii cloud broker uses a self-signed cert chain (same
        # reason their mobile app needs reFlutter to bypass validation).
        # We connect to a fixed known hostname so disabling verification
        # is acceptable here.
        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(tls_ctx)
        c.tls_insecure_set(True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        c.connect_async(self.cfg.cloud_host, self.cfg.cloud_port, keepalive=15)
        c.loop_start()
        self._client = c
        # Heartbeat thread starts after on_connect fires.

    def stop(self) -> None:
        self._stop.set()
        if self._client:
            self._client.loop_stop()
            try:
                self._client.disconnect()
            except Exception:
                pass

    # ---- Callbacks -----------------------------------------------------

    def _on_connect(self, client: mqtt.Client, _userdata, _flags, rc, _props=None):
        if rc != mqtt.CONNACK_ACCEPTED:
            LOG.error("[%s] cloud-mqtt connect failed rc=%s", self.acct.label, rc)
            return
        LOG.info("[%s] cloud-mqtt connected as %s, subscribing to %d serial(s)",
                 self.acct.label, self.client_id, len(self.acct.serials))
        for sn in self.acct.serials:
            # Wildcard model code in the topic - Neomow X Pro uses 0002,
            # other models likely use different codes. We don't know all
            # codes a priori, so subscribe to "+" and let the server filter
            # by the JWT authorisation carried in our heartbeat.
            topic = f"hk/server/mower/push/+/{sn}"
            client.subscribe(topic, qos=1)
            LOG.info("[%s] SUB %s", self.acct.label, topic)
        # Start heartbeat thread (idempotent).
        if not self._hb_thread or not self._hb_thread.is_alive():
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"hb-{self.acct.label}", daemon=True,
            )
            self._hb_thread.start()

    def _on_disconnect(self, _client, _userdata, _flags, rc, _props=None):
        LOG.warning("[%s] cloud-mqtt disconnected rc=%s - will auto-reconnect", self.acct.label, rc)

    def _on_message(self, _client, _userdata, msg: mqtt.MQTTMessage):
        try:
            # Topic: hk/server/mower/push/<model>/<serial>
            parts = msg.topic.split("/")
            if len(parts) < 6:
                LOG.warning("[%s] unexpected topic %s", self.acct.label, msg.topic)
                return
            serial = parts[-1]
            observed_model = parts[-2]
            if self._serial_model.get(serial) != observed_model:
                LOG.info("[%s] learned model=%s for serial=%s (was %s)",
                         self.acct.label, observed_model, serial, self._serial_model.get(serial))
                self._serial_model[serial] = observed_model
            payload_raw = msg.payload
            try:
                payload = json.loads(payload_raw)
            except Exception:
                LOG.warning("[%s] non-JSON payload on %s (%d bytes)", self.acct.label, msg.topic, len(payload_raw))
                return
            msg_type = payload.get("msgType", "?")
            LOG.debug("[%s] RX %s msgType=%s serial=%s", self.acct.label, msg.topic, msg_type, serial)
            # Normalise STATUS payloads so HA's existing template sensors
            # work for both shapes the new cloud emits (see normalise_status).
            if msg_type == "STATUS":
                normalise_status(payload)
                payload_out = json.dumps(payload).encode("utf-8")
            else:
                payload_out = payload_raw
            # Republish to local broker on the legacy topic so HA template
            # sensors + n8n keep reading the same place.
            local_topic = self.cfg.local_topic_fmt.format(serial=serial)
            self.local.publish(local_topic, payload_out, qos=0, retain=False)
        except Exception:
            LOG.exception("[%s] error processing inbound msg", self.acct.label)

    # ---- Heartbeat -----------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Send heartbeat per (account, serial) at HEARTBEAT_SEC pace.

        Stops when self._stop is set or the client disconnects permanently.
        """
        LOG.info("[%s] heartbeat thread starting interval=%ds", self.acct.label, self.cfg.heartbeat_sec)
        while not self._stop.is_set():
            if not self._client or not self._client.is_connected():
                time.sleep(1)
                continue
            for sn in self.acct.serials:
                model = self._serial_model.get(sn, self.cfg.model)
                topic = f"hk/app/mower/hb/{model}/{sn}"
                payload = json.dumps({
                    "ts": int(time.time() * 1000),
                    "msgType": "HEARTBEAT",
                    "data": {"push": self.push_counter, "token": self.acct.jwt},
                })
                try:
                    self._client.publish(topic, payload, qos=0)
                except Exception:
                    LOG.exception("[%s] heartbeat publish failed for %s", self.acct.label, sn)
            self.push_counter += 1
            # Sleep in small chunks so stop signal is honoured promptly.
            for _ in range(self.cfg.heartbeat_sec):
                if self._stop.is_set():
                    return
                time.sleep(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    cfg = parse_config()

    # Local MQTT (target for republished STATUS).
    local = mqtt.Client(
        client_id=f"hookii-bridge-{int(time.time())}",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    if cfg.local_user:
        local.username_pw_set(cfg.local_user, cfg.local_pass)
    local.connect_async(cfg.local_host, cfg.local_port, keepalive=60)
    local.loop_start()

    # Wait briefly for the local connection so the very first republish
    # doesn't get queued before the broker handshake completes.
    for _ in range(10):
        if local.is_connected():
            break
        time.sleep(0.5)
    if not local.is_connected():
        LOG.error("local broker %s:%d not connected yet - publishes will queue locally",
                  cfg.local_host, cfg.local_port)

    # REST login + cloud MQTT per account.
    clients: list[AccountClient] = []
    for acct in cfg.accounts:
        # Allow hardcoded serial fallback via env (HOOKII_SERIALS_<LABEL>).
        env_serials = os.environ.get(f"HOOKII_SERIALS_{acct.label.upper()}", "").strip()
        try:
            hookii_login(cfg, acct)
        except Exception:
            LOG.exception("[%s] REST login failed - skipping account this cycle", acct.label)
            continue
        if env_serials:
            override = [s.strip() for s in env_serials.split(",") if s.strip()]
            LOG.info("[%s] overriding serials from env: %s (was %s)", acct.label, override, acct.serials)
            acct.serials = override
        if not acct.serials:
            LOG.warning("[%s] no serials known - skipping", acct.label)
            continue
        ac = AccountClient(cfg, acct, local)
        ac.start()
        clients.append(ac)

    if not clients:
        LOG.fatal("no accounts started successfully")
        return 1

    LOG.info("bridge running with %d cloud client(s); ctrl-c to exit", len(clients))

    # JWT refresh loop: re-login every ~6h so token doesn't expire silently.
    REFRESH_INTERVAL = 6 * 60 * 60

    stop = threading.Event()

    def _sig(_signum, _frame):
        LOG.info("shutdown signal received")
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    last_refresh = time.time()
    while not stop.is_set():
        time.sleep(5)
        if time.time() - last_refresh > REFRESH_INTERVAL:
            LOG.info("rotating JWTs (>%ds since last refresh)", REFRESH_INTERVAL)
            for ac in clients:
                try:
                    hookii_login(cfg, ac.acct)
                except Exception:
                    LOG.exception("[%s] JWT refresh failed - keeping old token", ac.acct.label)
            last_refresh = time.time()

    LOG.info("stopping cloud clients")
    for ac in clients:
        ac.stop()
    local.loop_stop()
    try:
        local.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
