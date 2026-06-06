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
from datetime import datetime, timezone

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

    # 1b. taskInfo fan-out: same trick as chassisData. The new cloud nests
    #     mowing-task telemetry (regionName, mowedArea, unMowedArea,
    #     mowingCoverage, mowingEfficiency, mowingHeight, taskProgress,
    #     executeTime, ...) inside data.STATUS.taskInfo, but every legacy
    #     HA template sensor was written against the flat top-level shape
    #     and reads e.g. value_json.data.STATUS.regionName. Fanning the
    #     fields out as a non-clobbering copy fixes all those templates
    #     without touching the dashboards.
    task_info = status.get("taskInfo")
    if isinstance(task_info, dict):
        for k, v in task_info.items():
            status.setdefault(k, v)

        # 1b'. Backward-compat alias: deviceRegionTask. Pre-May-2026 HA
        #      template sensors were written against an earlier cloud
        #      schema that nested mowing-task telemetry under
        #      data.STATUS.deviceRegionTask using slightly different
        #      field names (cutArea instead of mowedArea,
        #      mowingCoverageRate instead of mowingCoverage). The new
        #      cloud dropped that shape entirely. Templates reading the
        #      old path resolve to undefined and the dashboard shows
        #      "Unknown" / "0". Reconstruct the legacy shape from
        #      taskInfo so those templates keep working without manual
        #      configuration.yaml edits across every install.
        if "deviceRegionTask" not in status:
            drt = dict(task_info)
            if "mowedArea" in drt:
                drt.setdefault("cutArea", drt["mowedArea"])
            if "unMowedArea" in drt:
                drt.setdefault("uncutArea", drt["unMowedArea"])
            if "mowingCoverage" in drt:
                drt.setdefault("mowingCoverageRate", drt["mowingCoverage"])
            status["deviceRegionTask"] = drt

    # 1c. Blade rotation direction (CW vs CCW) is encoded as a sign on
    #     knifeDiscMotorSpeed in the raw cloud payload. HA users want a
    #     "blade is spinning at N rpm" reading, not a vector quantity -
    #     publish the absolute value so the sensor reads positive when
    #     the blade is on. Rotation direction is still derivable from
    #     the sign of knifeDiscMotorCurrent if anyone needs it.
    rpm = status.get("knifeDiscMotorSpeed")
    if isinstance(rpm, (int, float)):
        status["knifeDiscMotorSpeed"] = abs(rpm)

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
    #    Sign convention - same in both shapes (verified live across
    #    Tor's 4 mowers 2026-05-29 with a clear mix of states):
    #        positive value = current flowing INTO battery = charging
    #        negative value = current flowing OUT of battery = discharging/mowing
    #    Examples observed:
    #      Jannick docked at 95% trickle-charging: chargeDischargeCurrent=+2.5
    #      Greenhouse mowing actively (blade -1794 rpm): chargeDischargeCurrent=-2.2
    #      Pond docked at 100% (full): chargeDischargeCurrent=-0.1
    #    An earlier version of this function flipped the sign based on a
    #    one-off observation that turned out to be misread - the flip
    #    inverted the WHOLE table and made every mower's UI lie.
    if "chargeDischargeCurrent" in status:
        cdc = status["chargeDischargeCurrent"]
        if "chargeCurrent" not in status:
            status["chargeCurrent"] = cdc
        if "dischargeCurrent" not in status:
            status["dischargeCurrent"] = cdc

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
    """POST a Hookii command. Auto re-login on 401 OR application-level
    code=10 ("token 失效" = invalid token). Returns response.data dict.

    Hookii's server returns 200 OK with code=10 in the JSON body when the
    JWT has been invalidated server-side (often because the user signed
    in again from another device, or the server eagerly expired our
    token). That requires re-login + retry exactly like a 401."""
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
        if code == 10 and attempt == 1:
            LOG.info("[%s] POST %s -> code=10 token-invalid, re-login + retry",
                     acct.label, path)
            try:
                hookii_login(cfg, acct)
            except Exception:
                LOG.exception("[%s] re-login failed during code=10 retry", acct.label)
                return {}
            continue
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


def cmd_camera_snapshot(cfg: Config, acct: HookiiAccount, serial: str,
                        model: str) -> bytes | None:
    """Trigger an on-demand camera snapshot from the mower and return the JPG
    bytes, or None on failure.

    Two-step protocol PCAP-confirmed 2026-05-30:

    1. POST /api/v1/mower/capture/image  body={serialNumber, modelCode}
       -> data={result:true, fileName:..., fileUrl: 'https://...:9443/...'}
    2. GET  <fileUrl>   -> JPG bytes (port 9443 is a separate CDN host)

    The fileUrl is single-shot - a random hash per snapshot, and the file
    expires after a short TTL on the CDN. So we always GET it immediately
    while it's still warm.
    """
    body = {"serialNumber": serial, "modelCode": model}
    data = _hookii_post(cfg, acct, "/api/v1/mower/capture/image", body)
    if not data or not data.get("result"):
        LOG.warning("[%s] snapshot: server declined or empty data: %s", acct.label, data)
        return None
    file_url = data.get("fileUrl")
    if not file_url:
        LOG.warning("[%s] snapshot: response missing fileUrl: %s", acct.label, data)
        return None
    try:
        r = requests.get(file_url, headers=hookii_headers(token=acct.jwt or ""),
                         timeout=20, verify=False)
        if r.status_code != 200:
            LOG.warning("[%s] snapshot: GET %s -> %d", acct.label, file_url, r.status_code)
            return None
        return r.content
    except requests.RequestException:
        LOG.exception("[%s] snapshot: GET %s transport error", acct.label, file_url)
        return None


def cmd_recover_alarm(cfg: Config, acct: HookiiAccount, serial: str, model: str,
                      req_opr_type: int = 0) -> tuple[int | None, dict]:
    """Send (or poll) the remote-recovery-alarm command. Returns (code, data).

    Returns the WHOLE response envelope's code so callers can detect the
    code=61 "temporary resources expired" terminal state that this endpoint
    uses to signal completion instead of the result==1 marker used by
    start/stop/job. PCAP-confirmed 2026-05-30.
    """
    body = {
        "serialNumber": serial,
        "modelCode": model,
        "response": None,
        "reqOprType": req_opr_type,
    }
    # We can't use the regular _hookii_post here - it swallows code=61 as a
    # warning. Inline the auth+post logic and surface the code directly.
    url = f"https://{cfg.rest_host}:{cfg.rest_port}/api/v1/mower/remote/recovery/alarm"
    for attempt in (1, 2):
        try:
            r = requests.post(url, json=body, headers=hookii_headers(token=acct.jwt or ""), timeout=20)
        except requests.RequestException:
            LOG.exception("[%s] recover_alarm transport error (attempt %d)", acct.label, attempt)
            return None, {}
        if r.status_code == 401 and attempt == 1:
            try:
                hookii_login(cfg, acct)
            except Exception:
                return None, {}
            continue
        try:
            envelope = r.json()
        except Exception:
            return None, {}
        code = envelope.get("code") if isinstance(envelope, dict) else None
        if code == 10 and attempt == 1:
            try:
                hookii_login(cfg, acct)
            except Exception:
                return None, {}
            continue
        return code, (envelope.get("data") if isinstance(envelope, dict) else None) or {}
    return None, {}


def cmd_recover_alarm_with_poll(cfg: Config, acct: HookiiAccount, serial: str,
                                model: str) -> dict:
    """Submit a recover_alarm (reqOprType=0) then poll (reqOprType=1) until
    the server returns code=61 ("temporary resources expired" = the action
    completed and the server cleared its temporary state) or
    _CMD_POLL_TIMEOUT elapses. Returns the last response.data payload."""
    code, data = cmd_recover_alarm(cfg, acct, serial, model, req_opr_type=0)
    LOG.info("[%s] recover_alarm initial: code=%s data=%s", acct.label, code, data)
    if code is None:
        return {}
    if code == 61:
        # Action terminated immediately - already done.
        return {"completed": True}
    deadline = time.time() + _CMD_POLL_TIMEOUT
    poll_n = 0
    while time.time() < deadline:
        time.sleep(_CMD_POLL_INTERVAL)
        poll_n += 1
        code, data = cmd_recover_alarm(cfg, acct, serial, model, req_opr_type=1)
        if code == 61:
            LOG.info("[%s] recover_alarm finalised after %d poll(s) (code=61, action complete)",
                     acct.label, poll_n)
            return {"completed": True}
        if code not in (0, 1):
            LOG.warning("[%s] recover_alarm poll %d -> code=%s (giving up)", acct.label, poll_n, code)
            return data
    LOG.warning("[%s] recover_alarm did not finalise within %.1fs (last code=%s)",
                acct.label, _CMD_POLL_TIMEOUT, code)
    return data


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

    # 1. Seven buttons (each posts an empty JSON object as the "press" payload;
    #    the bridge dispatcher reads serial + action from the topic).
    for action, name, icon in [
        ("start",          "Start",                  "mdi:play"),
        ("pause",          "Pause",                  "mdi:pause"),
        ("return",         "Return to dock",         "mdi:home-import-outline"),
        ("stop_keep",      "Stop (keep progress)",   "mdi:stop"),
        ("stop_clear",     "Stop (clear progress)",  "mdi:stop-circle-outline"),
        # Self-heal a remote-recoverable exception (e.g. docking-failure
        # 515). Maps to /api/v1/mower/remote/recovery/alarm with the
        # reqOprType=0/1 poll pattern.
        ("recover_alarm",  "Clear exception",        "mdi:auto-fix"),
        # On-demand camera snapshot. Bridge POSTs /api/v1/mower/capture/image,
        # downloads the resulting JPG and republishes it retained to
        # hookii/snapshot/<serial>; the auto-discovered MQTT camera entity
        # picks it up and shows the latest snapshot.
        ("snapshot",       "Camera snapshot",        "mdi:camera"),
    ]:
        _publish("button", action, {
            "name": name,
            "unique_id": f"hookii_{serial}_{action}",
            "command_topic": cfg.cmd_topic_fmt.format(serial=serial, action=action),
            "payload_press": "{}",
            "icon": icon,
            "device": device,
        })

    # 1b. MQTT camera entity backed by hookii/snapshot/<serial>. The "Camera
    #     snapshot" button above triggers a fresh capture; this entity shows
    #     the most recent one. Retained image survives HA restarts. We omit
    #     `image_encoding` so HA treats the payload as raw JPG bytes (the
    #     default when the key is absent); setting it to "b64" would expect
    #     base64-encoded text instead.
    _publish("camera", "snapshot", {
        "name": "Last snapshot",
        "unique_id": f"hookii_{serial}_camera",
        "topic": f"hookii/snapshot/{serial}",
        "icon": "mdi:camera-iris",
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
        ("wifi_signal",  "WiFi signal",        "wifiSignal",            "%",   None,          "measurement", "mdi:wifi",          "int"),
        # mowingHeight is the cutting height in MILLIMETRES (raw 40 = 40 mm = 4 cm;
        # a mower does not cut at 40 cm). Declaring "mm" + device_class distance lets
        # HA convert/display correctly. Users who hand-rolled a template sensor often
        # mislabel it "cm" and get "40 cm" - this ships the correct unit out of box.
        ("cutting_height", "Cutting Height",   "mowingHeight",          "mm",  "distance",    "measurement", "mdi:height",        "int"),
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

    LOG.info("discovery: published %d entities for %s", 7 + 1 + 1 + len(sensors), serial)


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
        # Session-scoped "push" value the heartbeat includes. PCAP-observed
        # behaviour is that the app uses the SAME value for every heartbeat
        # in a session (not a counter). Default 23 matches the Android
        # client we captured 2026-05-29.
        self.push_counter = 23
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None
        self._hb_thread: threading.Thread | None = None
        self._watchdog: MqttWatchdog | None = None
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
        # Watchdog: force-exit the process if cloud-MQTT stays disconnected
        # for >5 min. The supervisor (k8s/Docker/HA-Supervisor) respawns us
        # with a fresh paho client - recovers from the known paho state
        # where on_disconnect fires but on_connect never fires again.
        self._watchdog = MqttWatchdog(f"cloud-{self.acct.label}", c)
        self._watchdog.start()

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
        if self._watchdog:
            self._watchdog.on_connect()
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
        if self._watchdog:
            self._watchdog.on_disconnect()

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
            # sensors + n8n keep reading the same place. retain=True is
            # important: an idle docked mower may not push a new STATUS for
            # hours, so without retain the broker has nothing to hand to HA
            # after any restart (bridge, broker, HA itself) and every entity
            # shows "Unavailable" until the cloud emits the next change. With
            # retain=True the last known state is replayed on subscribe and
            # the dashboard recovers immediately; the data is "stale until
            # the next cloud update" but that is strictly better than
            # "missing for an unbounded time".
            local_topic = self.cfg.local_topic_fmt.format(serial=serial)
            self.local.publish(local_topic, payload_out, qos=0, retain=True)
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
            elif action == "recover_alarm":
                # Self-heal a remote-recoverable exception (e.g. docking
                # failure 515). PCAP-confirmed 2026-05-30: same reqOprType=0
                # then reqOprType=1 polling pattern as start/stop/job; the
                # endpoint signals completion with code=61 ("temporary
                # resources expired") which is success not failure.
                cmd_recover_alarm_with_poll(self.cfg, self.acct, serial, model)
            elif action == "snapshot":
                # On-demand camera snapshot. The JPG bytes are published
                # retained to hookii/snapshot/<serial> so the auto-discovered
                # MQTT camera entity displays the latest image until the
                # next snapshot replaces it. We ALSO publish a small metadata
                # payload to hookii/snapshot_meta/<serial> retained on BOTH
                # success and failure - the status field lets HA distinguish
                # "took a fresh picture (show it)" from "cloud declined
                # because mower is asleep/charging (show 'unable to capture
                # in current state' message)" without the user wondering
                # whether the button worked at all.
                taken_at = datetime.now(timezone.utc).isoformat()
                jpg = cmd_camera_snapshot(self.cfg, self.acct, serial, model)
                if jpg:
                    self.local.publish(
                        f"hookii/snapshot/{serial}",
                        jpg, qos=1, retain=True,
                    )
                    self.local.publish(
                        f"hookii/snapshot_meta/{serial}",
                        json.dumps({
                            "status": "ok",
                            "taken_at": taken_at,
                            "size": len(jpg),
                        }),
                        qos=1, retain=True,
                    )
                    LOG.info("[%s] snapshot published: %d bytes at %s",
                             self.acct.label, len(jpg), taken_at)
                else:
                    # Cloud declined - usually because the mower's camera is
                    # offline (deep-sleep, charging-without-camera-active,
                    # firmware-update, etc). Publish a status payload so the
                    # UI can show a friendly "robot unable to take picture
                    # right now" message AND still see that the button worked.
                    self.local.publish(
                        f"hookii/snapshot_meta/{serial}",
                        json.dumps({
                            "status": "declined",
                            "taken_at": taken_at,
                            "reason": "cloud declined - mower may be asleep or unreachable",
                        }),
                        qos=1, retain=True,
                    )
                    # Keep the legacy error topic for back-compat with any
                    # external integrations that already subscribed to it.
                    self.local.publish(
                        f"hookii/result/{serial}/error",
                        json.dumps({"action": "snapshot", "error": "capture or download failed"}),
                        qos=1, retain=False,
                    )
                    LOG.info("[%s] snapshot declined by cloud at %s",
                             self.acct.label, taken_at)
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
                    # push is a FIXED per-session value, not a monotonic
                    # counter. PCAP-confirmed: the Android app sends the
                    # same push value across every heartbeat in a session
                    # (observed value 23 across 18 consecutive heartbeats
                    # in the Greenhouse-recharge capture). Server probably
                    # uses (push, loginId) as a logical session key - so
                    # heartbeats from this bridge + the user's phone with
                    # the same loginId but different push values look like
                    # competing sessions, which is one explanation for
                    # the "phone logged out every hour" symptom (server
                    # picks newest and evicts the older one).
                    "data": {"push": self.push_counter, "token": self.acct.jwt},
                })
                try:
                    self._client.publish(topic, payload, qos=0)
                except Exception:
                    LOG.exception("[%s] heartbeat publish failed for %s", self.acct.label, sn)
            # NB: push_counter intentionally stays constant - first stamp
            # was set when AccountClient was constructed and is preserved
            # across the whole connection. Reconnects (new session) reset
            # it via a fresh AccountClient instance.
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

class MqttWatchdog:
    """Recover-by-restart guard for stuck paho-mqtt clients.

    paho-mqtt's `loop_start()` thread normally handles auto-reconnect
    after a transient broker disconnect: `on_disconnect` fires, the
    transport reconnects, `on_connect` fires, and any subscription
    callback re-subscribes. We observed (2026-05-30) a state where paho
    fired `on_disconnect` with `Unspecified error`, then sat for 4 hours
    without ever calling `on_connect` again. `client.is_connected()`
    returned `False` the whole time. No log line. No exception. Just a
    silently dead bridge.

    The watchdog is a daemon thread that wakes every CHECK_INTERVAL
    seconds and asks one question: "How long has the client been NOT
    connected?". If the answer is more than UNHEALTHY_SECONDS, we call
    `os._exit(1)` so the process supervisor (Home Assistant Supervisor
    for the add-on, k8s/Docker for standalone deploys) respawns us with
    a fresh paho client. That is a heavy-handed recovery, but it is the
    one that always works, and the alternative is the bridge silently
    looking online while delivering nothing.

    Call `on_connect()` from your paho `on_connect` callback and
    `on_disconnect()` from your paho `on_disconnect` callback. The
    bookkeeping must not assume that connect/disconnect arrive in
    strict alternation - paho occasionally collapses pairs after a
    fast bounce.
    """

    CHECK_INTERVAL = 30          # seconds between health checks
    UNHEALTHY_SECONDS = 300      # 5 min disconnected -> recover by restart

    def __init__(self, name: str, client: mqtt.Client,
                 unhealthy_seconds: int = UNHEALTHY_SECONDS,
                 check_interval: int = CHECK_INTERVAL):
        self.name = name
        self.client = client
        self.unhealthy_seconds = unhealthy_seconds
        self.check_interval = check_interval
        # last_connected_at = wall clock of most recent successful CONNACK.
        # Initialised to "now" so we don't trip the watchdog during the
        # initial connect attempt that has not yet succeeded.
        self.last_connected_at = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def on_connect(self) -> None:
        with self._lock:
            self.last_connected_at = time.monotonic()

    def on_disconnect(self) -> None:
        # Nothing to do - the next _check_once will see is_connected()=False
        # and start counting from `last_connected_at`. We intentionally do
        # NOT reset last_connected_at here, because the goal is "alarm when
        # we have stayed disconnected too long since the LAST good state".
        pass

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"mqtt-watchdog-{self.name}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval):
            try:
                self._check_once()
            except Exception:
                LOG.exception("watchdog %s: check failed", self.name)

    def _check_once(self) -> None:
        if self.client.is_connected():
            with self._lock:
                self.last_connected_at = time.monotonic()
            return
        with self._lock:
            seconds_disconnected = time.monotonic() - self.last_connected_at
        if seconds_disconnected > self.unhealthy_seconds:
            LOG.error(
                "watchdog %s: MQTT client has been disconnected for %.0f s "
                "(threshold %d s) - exiting process so the supervisor "
                "respawns us with a fresh paho client. This recovers from "
                "the known paho `loop_start` state where on_disconnect "
                "fires but on_connect never fires again.",
                self.name, seconds_disconnected, self.unhealthy_seconds,
            )
            os._exit(1)


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

    # Wire on_connect to re-subscribe EVERY time the local client (re)connects.
    # Without this, the MQTT subscription is lost across broker hiccups
    # (broker restart, HA restart that bounces Mosquitto, network blip) and
    # the bridge starts silently dropping button presses without any log
    # signal that something is wrong - we observed this 2026-05-30 after
    # back-to-back HA restarts during a v1.2.1 deploy.
    local_watchdog = MqttWatchdog("local", local)

    def _on_local_connect(_c, _u, _flags, reason_code, _props=None) -> None:
        try:
            rc = int(reason_code)
        except Exception:
            rc = reason_code
        if rc == 0:
            local.subscribe("hookii/cmd/+/+", qos=1)
            LOG.info("local broker (re)connected (rc=%s); re-subscribed hookii/cmd/+/+ for %d serial(s)",
                     rc, len(account_by_serial))
            local_watchdog.on_connect()
        else:
            LOG.warning("local broker connect refused (rc=%s); paho will retry", rc)

    def _on_local_disconnect(_c, _u, _flags, reason_code, _props=None) -> None:
        try:
            rc = int(reason_code)
        except Exception:
            rc = reason_code
        if rc != 0:
            LOG.warning("local broker disconnected (rc=%s); paho will reconnect + re-subscribe via on_connect", rc)
            local_watchdog.on_disconnect()

    local.on_connect = _on_local_connect
    local.on_disconnect = _on_local_disconnect
    local_watchdog.start()

    # Initial subscribe (in case we were already connected before wiring
    # the callback). on_connect handles every subsequent reconnect.
    if local.is_connected():
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
