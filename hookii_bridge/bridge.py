"""Hookii Neomow → local MQTT bridge with HA Discovery + REST commands.

Background: as of May 2026 Hookii migrated their cloud IoT from the
plain ``hookii/details/device/<serial>`` topic format on their public
broker to a JWT-gated push protocol on ``iot.beta.hookii.com:8883``.
The new server gates STATUS pushes on a live heartbeat from the client
carrying the user's REST-issued JWT. A passive MQTT subscriber sees
nothing.

This bridge keeps that heartbeat alive per (user-account, mower-serial)
pair, normalises the cloud's STATUS payload back to the legacy field
shape, republishes it to a LOCAL MQTT broker on the legacy topic
format, exposes a REST-command channel for control operations (start,
pause, return, schedule R/W, params read) and publishes Home Assistant
MQTT Discovery configs so a `lawn_mower` entity and five command
buttons appear in HA without the user touching YAML.

Plain Python + paho-mqtt + requests with no host-specific dependencies;
runs equally well as a Home Assistant Supervisor add-on, systemd
service, or Docker Compose workload pointed at any local Mosquitto /
EMQX / RabbitMQ broker.

Configuration via env: see DOCS.md / the add-on Configuration tab.
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

    # 6. HA-friendly derived state from robotStatus + workingMode (per the
    #    reverse-engineered command/state reference). This lets the
    #    discovered lawn_mower entity use a stable activity field and saves
    #    every user from writing the same template logic by hand.
    #
    #    NOT every STATUS shape carries robotStatus - Shape A only has the
    #    higher-level workingMode (0/1/2) so we fall back to that when
    #    robotStatus is absent. This keeps ha_state populated on every
    #    STATUS message instead of half of them.
    rs = status.get("robotStatus")
    wm = status.get("workingMode")
    cc = status.get("chargeCurrent")
    ha_state: str | None = None
    ha_is_charging = False
    if rs == 5:
        # Confirmed in the reference: "Charging at dock".
        ha_state, ha_is_charging = "docked", True
    elif rs in (0, 3, 4):
        # 0=idle, 3=sleeping, 4=at-dock-trickle (observed live, undocumented
        # in the reference but always co-occurs with wm=0 + near-zero
        # chargeCurrent so it's clearly a docked sub-state).
        ha_state = "docked"
    elif rs in (9, 10):
        ha_state = "returning"
    elif rs == 7:
        # robotStatus=7 is the "travelling" sub-state. workingMode=1 means
        # the higher-level intent is "go home"; workingMode=2 means "work"
        # so we're transiting between zones mid-job.
        ha_state = "returning" if wm == 1 else "mowing"
    elif rs in (1, 2):
        ha_state = "mowing"
    elif rs is None:
        # No robotStatus (typical Shape A, or sparse heartbeat) - derive
        # from workingMode alone. workingMode 0 = idle/parked, 1 = returning,
        # 2 = working. chargeCurrent > 0 means current is flowing into the
        # battery so the mower is at the dock charging.
        if wm == 0:
            ha_state = "docked"
            if isinstance(cc, (int, float)) and cc > 0:
                ha_is_charging = True
        elif wm == 1:
            ha_state = "returning"
        elif wm == 2:
            ha_state = "mowing"
    # else: still-unknown robotStatus value (future firmware) - leave
    # ha_state unset so HA templates fall back to previous-state.
    if ha_state is not None:
        status["ha_state"] = ha_state
        status["ha_is_charging"] = ha_is_charging


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
    heartbeat_sec: float
    enable_discovery: bool = True
    discovery_prefix: str = "homeassistant"
    cmd_topic_fmt: str = "hookii/cmd/{serial}/{action}"


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
        heartbeat_sec=float(os.environ.get("HEARTBEAT_SEC", "1.5")),
        enable_discovery=os.environ.get("ENABLE_DISCOVERY", "1") not in ("0", "false", "False", ""),
        discovery_prefix=os.environ.get("DISCOVERY_PREFIX", "homeassistant"),
        cmd_topic_fmt=os.environ.get("CMD_TOPIC_FMT", "hookii/cmd/{serial}/{action}"),
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
# REST command helpers (the command channel is HTTP, not MQTT)
# ---------------------------------------------------------------------------
#
# Per the reverse-engineered protocol reference: all control operations go to
# `iot.beta.hookii.com:10443` over HTTPS. The cloud MQTT bus is read-only for
# telemetry. Commands need the JWT from /user/login/email in the hookii-token
# header; on 401 we re-login once and retry. The response envelope is
# {"code": 1, "msg": "...", "data": {...}}; code 1 = success.


def _hookii_post(cfg: Config, acct: HookiiAccount, path: str, body: dict) -> dict:
    """POST a Hookii command. Auto re-login on 401. Returns response.data dict."""
    url = f"https://{cfg.rest_host}:{cfg.rest_port}{path}"
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=body, headers=hookii_headers(token=acct.jwt or ""), timeout=20)
        except requests.RequestException:
            LOG.exception("[%s] POST %s transport error (attempt %d)", acct.label, path, attempt)
            return {}
        if r.status_code == 401 and attempt == 1:
            LOG.info("[%s] POST %s -> 401, re-login + retry", acct.label, path)
            try:
                hookii_login(cfg, acct)
            except Exception:
                LOG.exception("[%s] re-login failed during command retry", acct.label)
                return {}
            continue
        try:
            data = r.json()
        except Exception:
            LOG.error("[%s] POST %s -> %s (non-JSON %d-byte body)", acct.label, path, r.status_code, len(r.content))
            return {}
        code = data.get("code") if isinstance(data, dict) else None
        if code not in (0, 1):
            LOG.warning("[%s] POST %s -> code=%s msg=%s",
                        acct.label, path, code, data.get("msg") if isinstance(data, dict) else "?")
        return data.get("data") if isinstance(data, dict) else {} or {}
    return {}


def cmd_start_stop(cfg: Config, acct: HookiiAccount, serial: str, model: str,
                   command: int, region_list: list | None = None,
                   req_opr_type: int = 0) -> dict:
    body: dict = {
        "command": command,
        "serialNumber": serial,
        "modelCode": model,
        "reqOprType": req_opr_type,
    }
    if region_list is not None:
        body["regionList"] = region_list
    return _hookii_post(cfg, acct, "/api/v1/mower/cmd/start/stop/job", body)


# PCAP-confirmed polling cadence the Hookii Android app uses for cmd=1 (and
# probably all start/stop/job commands). The app keeps reqOprType=1 polling
# at ~2-3s intervals until result == 1 (operation complete) or the response
# stops carrying waitingProgressInfo. The protocol reference Tor wrote says
# polling shouldn't be necessary - but observed live in 2026-05-29 testing
# the server only finalises a stuck/contact-fault recharge command after
# the polling sequence runs. So bridges that don't poll see the initial
# response, declare success, and the mower never actually executes.
_CMD_POLL_INTERVAL = 2.5
_CMD_POLL_TIMEOUT = 30.0


def cmd_start_stop_with_poll(cfg: Config, acct: HookiiAccount, serial: str,
                             model: str, command: int,
                             region_list: list | None = None) -> dict:
    """Submit a start/stop/job command (reqOprType=0) then poll (reqOprType=1)
    until the server finalises it or _CMD_POLL_TIMEOUT elapses. Returns the
    final response.data payload."""
    initial = cmd_start_stop(cfg, acct, serial, model, command,
                             region_list=region_list, req_opr_type=0)
    if not initial:
        return initial
    # result==1 + waitingProgressInfo absent means the server finished
    # synchronously (e.g. cmd=2 cancel for an idle mower).
    if initial.get("result") == 1 and not initial.get("waitingProgressInfo"):
        return initial
    deadline = time.time() + _CMD_POLL_TIMEOUT
    last = initial
    poll_n = 0
    while time.time() < deadline:
        time.sleep(_CMD_POLL_INTERVAL)
        poll_n += 1
        last = cmd_start_stop(cfg, acct, serial, model, command,
                              region_list=region_list, req_opr_type=1)
        if not last:
            LOG.warning("[%s] cmd %s poll %d: empty response - server may have errored",
                        acct.label, command, poll_n)
            return {}
        wpi = last.get("waitingProgressInfo")
        if last.get("result") == 1 or not wpi:
            LOG.info("[%s] cmd %s finalised after %d poll(s)",
                     acct.label, command, poll_n)
            return last
        LOG.debug("[%s] cmd %s poll %d: progress=%s",
                  acct.label, command, poll_n, wpi.get("progress"))
    LOG.warning("[%s] cmd %s polling timed out after %.0fs - returning last response",
                acct.label, command, _CMD_POLL_TIMEOUT)
    return last


def cmd_start_with_precheck(cfg: Config, acct: HookiiAccount, serial: str, model: str,
                            region_list: list | None = None) -> dict:
    """Two-step start: cmd=7 pre-check then cmd=6 execute (per protocol reference).

    Default policy when the pre-check returns interrupted mowing areas is to
    RESUME from breakpoints ("Keep mowing"). That's the safer behaviour for
    automation: nothing the mower had already cut gets discarded silently.
    Users who want a fresh start can send the action `stop_clear` first.
    """
    rl = region_list or []
    LOG.info("[%s] start %s: pre-check (cmd=7) regionList=%s", acct.label, serial, rl)
    pre = cmd_start_stop(cfg, acct, serial, model, 7, region_list=rl)
    interrupted = (pre or {}).get("interruptedMowingAreaList") or []
    if interrupted:
        LOG.info("[%s] start %s: %d interrupted area(s) - resuming from breakpoints",
                 acct.label, serial, len(interrupted))
    LOG.info("[%s] start %s: execute (cmd=6)", acct.label, serial)
    return cmd_start_stop_with_poll(cfg, acct, serial, model, 6, region_list=rl)


def _local_minutes_now() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def cmd_schedule_read(cfg: Config, acct: HookiiAccount, serial: str, model: str) -> dict:
    body = {
        "command": 0, "response": None,
        "serialNumber": serial, "modelCode": model,
        "timeZoneOffset": None, "taskList": None, "shieldTaskList": None,
    }
    return _hookii_post(cfg, acct, "/api/v1/mower/cmd/calendar/time", body)


def cmd_schedule_write(cfg: Config, acct: HookiiAccount, serial: str, model: str,
                      task_list: list, time_zone_offset: int | None = None) -> dict:
    """Write the schedule. Refuses if any enabled task overlaps the current
    local time, because the Hookii cloud treats "active schedule starting now"
    as an implicit start command - even if the mower was in the middle of
    returning to dock. The reference doc calls this out explicitly."""
    now_m = _local_minutes_now()
    for t in (task_list or []):
        if not t.get("enable"):
            continue
        st = t.get("startTime")
        et = t.get("endTime")
        if st is None or et is None:
            continue
        if st <= now_m <= et:
            raise ValueError(
                f"refusing schedule write: enabled task {t.get('taskId')} window "
                f"[{st},{et}] overlaps current minute-of-day {now_m}; mower would "
                f"start mowing immediately. Disable the task or wait until after "
                f"the window."
            )
    body = {
        "command": 1, "response": None,
        "serialNumber": serial, "modelCode": model,
        "timeZoneOffset": time_zone_offset,
        "taskList": task_list, "shieldTaskList": [],
    }
    return _hookii_post(cfg, acct, "/api/v1/mower/cmd/calendar/time", body)


def cmd_params_read(cfg: Config, acct: HookiiAccount, serial: str, model: str) -> dict:
    body = {
        "command": 0, "response": None,
        "serialNumber": serial, "modelCode": model,
        "areaParamList": None, "globalParam": None, "globalModeParamList": None,
    }
    return _hookii_post(cfg, acct, "/api/v1/mower/cmd/calendar/param", body)


# ---------------------------------------------------------------------------
# Home Assistant MQTT Discovery
# ---------------------------------------------------------------------------


def _device_descriptor(serial: str) -> dict:
    return {
        "identifiers": [f"hookii_{serial}"],
        "name": f"Neomow {serial[-6:]}",
        "manufacturer": "Hookii",
        "model": "Neomow",
        "via_device": "hookii_bridge",
    }


def _value_tmpl(jinja_inner: str, fallback_jinja: str = "this.state") -> str:
    """Wrap a Shape-A field extractor in the standard "previous-state if not STATUS" guard."""
    return (
        "{% if value_json is mapping and value_json.msgType == 'STATUS' "
        f"and {jinja_inner.replace(' is defined', '')} is defined %}}"
        f"{{{{ {jinja_inner} }}}}"
        "{% else %}"
        f"{{{{ {fallback_jinja} }}}}"
        "{% endif %}"
    )


def publish_discovery(local: mqtt.Client, cfg: Config, serial: str) -> None:
    """Publish HA MQTT-discovery configs for one mower.

    Emits a `lawn_mower` entity, five command `button`s (start, pause,
    return, stop_keep, stop_clear) and the common telemetry `sensor`s.
    Users who already pasted the YAML from DOCS get duplicate entities
    with distinct unique_ids - no conflict - and can delete their YAML
    blocks once they've verified the discovered ones work.
    """
    if not cfg.enable_discovery:
        return
    prefix = cfg.discovery_prefix
    state_topic = cfg.local_topic_fmt.format(serial=serial)
    device = _device_descriptor(serial)

    def _publish(component: str, object_id: str, body: dict) -> None:
        topic = f"{prefix}/{component}/hookii_{serial}/{object_id}/config"
        local.publish(topic, json.dumps(body), qos=1, retain=True)

    # 1. Five buttons (each posts an empty JSON object as the "press" payload;
    #    the bridge dispatcher reads serial + action from the topic).
    for action, name, icon in [
        ("start",      "Start",                  "mdi:play"),
        ("pause",      "Pause",                  "mdi:pause"),
        ("return",     "Return to dock",         "mdi:home-import-outline"),
        ("stop_keep",  "Stop (keep progress)",   "mdi:stop"),
        ("stop_clear", "Stop (clear progress)",  "mdi:stop-circle-outline"),
    ]:
        _publish("button", action, {
            "name": name,
            "unique_id": f"hookii_{serial}_{action}",
            "command_topic": cfg.cmd_topic_fmt.format(serial=serial, action=action),
            "payload_press": "{}",
            "icon": icon,
            "device": device,
        })

    # 2. Standard lawn_mower entity. The activity_value_template extracts our
    #    derived ha_state field from STATUS payloads; non-STATUS msgTypes
    #    leave the previous activity alone.
    _publish("lawn_mower", "mower", {
        "name": "Mower",
        "unique_id": f"hookii_{serial}_lawn_mower",
        "activity_state_topic": state_topic,
        "activity_value_template": _value_tmpl(
            "value_json.data.STATUS.ha_state",
            "this.attributes.activity or 'docked'",
        ),
        "start_mowing_command_topic": cfg.cmd_topic_fmt.format(serial=serial, action="start"),
        "pause_command_topic": cfg.cmd_topic_fmt.format(serial=serial, action="pause"),
        "dock_command_topic": cfg.cmd_topic_fmt.format(serial=serial, action="return"),
        "device": device,
    })

    # 3. Sensors - the canonical out-of-box telemetry set. Users with custom
    #    YAML still see THEIR sensors; these are an additional discovered
    #    namespace.
    sensors = [
        # (object_id, friendly_name, field, unit, device_class, state_class, icon, value_type)
        ("battery",      "Battery",            "electricity",           "%",   "battery",     "measurement", None,                "int"),
        ("blade_rpm",    "Blade RPM",          "knifeDiscMotorSpeed",   "rpm", None,          "measurement", "mdi:saw-blade",     "int"),
        ("voltage",      "Voltage",            "voltage",               "V",   "voltage",     "measurement", None,                "float"),
        ("charge_a",     "Charge current",     "chargeCurrent",         "A",   "current",     "measurement", None,                "float"),
        ("temp_battery", "Battery temp",       "batteryTemp",           "°C",  "temperature", "measurement", None,                "float"),
        ("temp_blade",   "Blade motor temp",   "knifeDiscMotorTemp",    "°C",  "temperature", "measurement", None,                "float"),
        ("temp_left",    "Left drive temp",    "leftDriveMotorTemp",    "°C",  "temperature", "measurement", None,                "float"),
        ("temp_right",   "Right drive temp",   "rightDriveMotorTemp",   "°C",  "temperature", "measurement", None,                "float"),
        ("wifi_signal",  "WiFi signal",        "wifiSignal",            "dBm", "signal_strength", "measurement", None,            "int"),
        ("satellite",    "GPS satellites",     "satellite",             None,  None,          "measurement", "mdi:satellite-variant", "int"),
        ("latitude",     "Latitude",           "latitude",              "°",   None,          None,          "mdi:latitude",      "float"),
        ("longitude",    "Longitude",          "longitude",             "°",   None,          None,          "mdi:longitude",     "float"),
        ("work_status",  "Work status",        "workStatus",            None,  None,          None,          "mdi:robot-mower",   "int"),
        ("ha_state",     "State",              "ha_state",              None,  None,          None,          "mdi:robot-mower-outline", "str"),
    ]
    for obj, name, field_, unit, dc, sc, icon, vt in sensors:
        body = {
            "name": name,
            "unique_id": f"hookii_{serial}_{obj}",
            "state_topic": state_topic,
            "value_template": _value_tmpl(f"value_json.data.STATUS.{field_}"),
            "device": device,
        }
        if unit:
            body["unit_of_measurement"] = unit
        if dc:
            body["device_class"] = dc
        if sc:
            body["state_class"] = sc
        if icon:
            body["icon"] = icon
        _publish("sensor", obj, body)

    LOG.info("discovery: published %d entities for %s", 5 + 1 + len(sensors), serial)


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

    # ---- Local command execution --------------------------------------

    def execute_local_command(self, action: str, serial: str, payload: dict) -> None:
        """Translate a hookii/cmd/<serial>/<action> publish into a REST call.

        Each branch maps to one of the command codes documented in the
        protocol reference. Schedule writes go through cmd_schedule_write
        which enforces the "no overlap with current minute-of-day" guard
        so an automation cannot accidentally make the mower start mowing
        by writing an active schedule for "now".
        """
        model = self._serial_model.get(serial, self.cfg.model)
        LOG.info("[%s] cmd %s serial=%s payload-keys=%s",
                 self.acct.label, action, serial, list(payload.keys()))
        try:
            if action == "start":
                cmd_start_with_precheck(self.cfg, self.acct, serial, model,
                                        payload.get("regionList"))
            elif action == "pause":
                cmd_start_stop_with_poll(self.cfg, self.acct, serial, model, 3)
            elif action in ("return", "dock", "recharge"):
                cmd_start_stop_with_poll(self.cfg, self.acct, serial, model, 1)
            elif action == "stop_keep":
                cmd_start_stop_with_poll(self.cfg, self.acct, serial, model, 2)
            elif action == "stop_clear":
                cmd_start_stop_with_poll(self.cfg, self.acct, serial, model, 8)
            elif action == "schedule_read":
                data = cmd_schedule_read(self.cfg, self.acct, serial, model)
                # Echo back to a local "result" topic so automations can
                # subscribe and see the current schedule.
                self.local.publish(
                    f"hookii/result/{serial}/schedule",
                    json.dumps(data), qos=1, retain=True,
                )
            elif action == "schedule_write":
                cmd_schedule_write(
                    self.cfg, self.acct, serial, model,
                    payload.get("taskList") or [],
                    payload.get("timeZoneOffset"),
                )
            elif action == "params_read":
                data = cmd_params_read(self.cfg, self.acct, serial, model)
                self.local.publish(
                    f"hookii/result/{serial}/params",
                    json.dumps(data), qos=1, retain=True,
                )
            else:
                LOG.warning("[%s] unknown cmd action %r (serial %s)", self.acct.label, action, serial)
        except ValueError as e:
            # Guardrail rejection (e.g. schedule overlap). Surface via a
            # local error topic so users can wire it into an HA persistent
            # notification.
            LOG.warning("[%s] cmd %s rejected: %s", self.acct.label, action, e)
            self.local.publish(
                f"hookii/result/{serial}/error",
                json.dumps({"action": action, "error": str(e)}), qos=1, retain=False,
            )
        except Exception:
            LOG.exception("[%s] cmd %s failed", self.acct.label, action)

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
            # The mobile app heartbeats at exactly 1.5s (confirmed via pcap).
            # We default to that. Sub-second intervals stay responsive to
            # SIGTERM by polling _stop at 0.5s granularity rather than the
            # full heartbeat interval - matters when interval >= 1s.
            deadline = time.time() + self.cfg.heartbeat_sec
            while time.time() < deadline:
                if self._stop.is_set():
                    return
                remaining = deadline - time.time()
                time.sleep(min(0.5, max(0.01, remaining)))


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
    # serial -> AccountClient lookup used by the local command dispatcher
    # (one local MQTT client receives all command publishes; the serial
    # in the topic tells us which account-bound REST session owns it).
    account_by_serial: dict[str, AccountClient] = {}
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
        for sn in acct.serials:
            account_by_serial[sn] = ac
            publish_discovery(local, cfg, sn)

    if not clients:
        LOG.fatal("no accounts started successfully")
        return 1

    # Wire the local command dispatcher. Every button-press, lawn_mower
    # service-call and direct mqtt.publish on hookii/cmd/<serial>/<action>
    # arrives here and gets translated to a REST call.
    def _on_local_cmd(_c, _u, m: mqtt.MQTTMessage) -> None:
        try:
            # Expected: hookii/cmd/<serial>/<action>
            parts = m.topic.split("/")
            if len(parts) != 4 or parts[0] != "hookii" or parts[1] != "cmd":
                return
            serial, action = parts[2], parts[3]
            ac = account_by_serial.get(serial)
            if not ac:
                LOG.warning("local cmd %s: unknown serial %s (known: %s)",
                            action, serial, list(account_by_serial.keys()))
                return
            try:
                payload = json.loads(m.payload) if m.payload else {}
            except Exception:
                payload = {}
            ac.execute_local_command(action, serial, payload)
        except Exception:
            LOG.exception("error dispatching local command on %s", m.topic)

    local.on_message = _on_local_cmd
    local.subscribe("hookii/cmd/+/+", qos=1)
    LOG.info("local cmd subscriber active on hookii/cmd/+/+ for %d serial(s)",
             len(account_by_serial))

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
