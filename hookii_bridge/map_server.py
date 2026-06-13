"""Hookii Neomow yard visualizer.

Subscribes directly to your local Mosquitto/EMQX broker (the same broker the
Hookii Bridge add-on publishes to), captures the cloud's STATUS / DEVICE_MAP_V2
/ ALL_PATH_LIST_V2 / ALL_PATH_INDEX_V2 messages per mower, and renders an SVG
view of the yard with:

- Boundary polygon (when DEVICE_MAP_V2 is captured)
- Path coverage split into cut (thick green) vs transit (thin light) segments
- Live trail of recent positions in the per-mower colour
- Robot live position + heading arrow
- "Last fix" timestamp watermark

Endpoints:
  GET /             — All-mowers grid HTML (shown by the HA sidebar panel)
  GET /api          — JSON index of configured mowers + endpoints
  GET /svg/<label>  — Latest SVG, width/height 100% (stretches to parent).
                      Use inside a full-page browser or the /page wrapper's
                      JS fetch. Collapses inside HA picture cards - see
                      /embed below.
  GET /embed[/<label>] — SVG tuned for HA dashboard embedding (picture /
                      picture-entity / iframe cards). Carries absolute pixel
                      width/height so it renders inside <img> tags, and uses
                      Cache-Control: no-cache (drops no-store) so HA's image
                      refresh flow can re-fetch on entity state changes. The
                      label-less /embed returns the only configured mower, or
                      400 if you have more than one.
  GET /page/<label> — HTML wrapper with 10-second meta-refresh, ready to drop
                      into a Home Assistant iframe card
  GET /state/<label>— JSON of the mower's current position + battery + capture
                      timestamps
  GET /all          — Grid HTML showing every configured mower at once

Configuration is via environment variables (see DOCS.md). Defaults match the
field names the Hookii Bridge add-on publishes - if you're running both add-ons
side by side, the only required values are the broker auth + the mower list.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOCAL_MQTT_HOST = os.environ.get("LOCAL_MQTT_HOST", "127.0.0.1")
LOCAL_MQTT_PORT = int(os.environ.get("LOCAL_MQTT_PORT", "1883"))
LOCAL_MQTT_USER = os.environ.get("LOCAL_MQTT_USER", "")
LOCAL_MQTT_PASS = os.environ.get("LOCAL_MQTT_PASS", "")
TOPIC_PREFIX = os.environ.get("TOPIC_PREFIX", "hookii/details/device")

TRAIL_MAX = int(os.environ.get("TRAIL_MAX", "2000"))
PERSIST_DIR = Path(os.environ.get("PERSIST_DIR", "/data"))
PERSIST_DIR.mkdir(parents=True, exist_ok=True)

# Watchdog: force-exit the process if NO MQTT message arrives for this many
# seconds. Recover-by-restart: the supervisor (k3s, Docker, HA Supervisor)
# spawns us again with a fresh paho client. Necessary because paho's
# `loop_forever(retry_first_connection=True)` can wedge after certain broker
# disconnects without ever returning (observed 2026-05-30 after the bridge's
# EMQX bounced and this client never reconnected for 4+ hours).
#
# 300 s is generous: an actively-mowing mower publishes STATUS every 2-5 s,
# an idle docked mower every 30-60 s, so 5 minutes of total silence across
# all configured mowers is a strong "something is wrong" signal regardless
# of the cause (broker dead, network split, stuck paho state).
WATCHDOG_IDLE_SECONDS = int(os.environ.get("WATCHDOG_IDLE_SECONDS", "300"))
_last_mqtt_message_at = time.monotonic()

# Counter-clockwise rotation in degrees applied to every (x, y) before the
# SVG bounding box is computed and the path is drawn. The Hookii cloud
# delivers points in the mower's own local frame which has no fixed
# relation to compass north; in practice the app and the SVG sometimes look
# 90 deg apart because the in-app projection is doing its own orientation.
# Set ROTATE_DEG to the offset that makes the SVG match your in-app view
# (typical values: 0, 90, 180, 270). Defaults to 0 = identity.
ROTATE_DEG = float(os.environ.get("ROTATE_DEG", "0"))
_ROT_RAD = math.radians(ROTATE_DEG)
_ROT_COS = math.cos(_ROT_RAD)
_ROT_SIN = math.sin(_ROT_RAD)


def _rotate_xy(x: float, y: float) -> tuple[float, float]:
    if ROTATE_DEG == 0:
        return (x, y)
    return (x * _ROT_COS - y * _ROT_SIN, x * _ROT_SIN + y * _ROT_COS)

# Default cutting width in cm for cut-path stroke rendering. The Neomow X Pro
# is ~23cm; the bridge may republish a more precise value via REGION_TASK
# payloads (mowingWidth field) which is then picked up at render time.
MOWING_WIDTH_DEFAULT_CM = float(os.environ.get("MOWING_WIDTH_CM", "25"))


def parse_mowers(spec: str) -> dict[str, dict]:
    """Parse the MOWERS env var into a per-label state dict.

    Format: ``label1:serial1:color1;label2:serial2:color2;...``

    `color` is optional and defaults to a curated palette. Each label becomes
    the URL slug on the /svg/<label>, /page/<label>, /state/<label> endpoints.

    Example::

        MOWERS=garden:HKX1EB100JD25010115:#22c55e;pond:HKX2EB100JD24080170
    """
    palette = ["#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#ec4899", "#06b6d4"]
    result: dict[str, dict] = {}
    for i, raw in enumerate(spec.split(";")):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) < 2:
            print(f"WARNING: MOWERS entry {raw!r} is malformed (need label:serial[:color])", flush=True)
            continue
        label = parts[0].strip()
        serial = parts[1].strip()
        color = parts[2].strip() if len(parts) >= 3 else palette[i % len(palette)]
        result[label] = {
            "serial": serial,
            "color": color,
            "trail": deque(maxlen=TRAIL_MAX),
            "robot_x": None,
            "robot_y": None,
            "heading": None,
            "battery": None,
            "work_status": None,
            "online_status": None,
            "last_update": None,
            "device_map": None,
            "path_list": None,
            "path_index": None,
            "region_task": None,  # latest REGION_TASK (mowingWidth, etc.)
            "device_map_at": None,
            "path_list_at": None,
            "path_index_at": None,
            "min_x": -1000, "max_x": 1000,
            "min_y": -1000, "max_y": 1000,
        }
    return result


MOWERS_RAW = os.environ.get("MOWERS", "").strip()
if not MOWERS_RAW:
    print("FATAL: MOWERS env var is required. Format: label1:serial1[:color1];label2:serial2[:color2]", flush=True)
    raise SystemExit(2)

state = parse_mowers(MOWERS_RAW)
if not state:
    print("FATAL: no usable mowers parsed from MOWERS env var", flush=True)
    raise SystemExit(2)

# Reverse index: serial → label, used by the MQTT message handler.
serial_to_label = {s["serial"]: label for label, s in state.items()}


# ---------------------------------------------------------------------------
# Persistence (so captures survive container restarts)
# ---------------------------------------------------------------------------

def persist_capture(label: str, msg_type: str, payload: str) -> None:
    p = PERSIST_DIR / f"{label}_{msg_type}.json"
    try:
        p.write_text(payload, encoding="utf-8")
        print(f"  persisted {p}", flush=True)
    except Exception as e:
        print(f"  persist failed: {e}", flush=True)


def persist_status(label: str, s: dict) -> None:
    """Save the latest STATUS so a restart doesn't wipe last-known position.

    Mowers only broadcast STATUS while online; without this the visualizer
    would show "Waiting for data..." between restarts and the first STATUS push
    after the container comes back up.
    """
    p = PERSIST_DIR / f"{label}_STATUS.json"
    payload = {
        "serial": s["serial"],
        "robot_x": s["robot_x"],
        "robot_y": s["robot_y"],
        "heading": s["heading"],
        "battery": s["battery"],
        "work_status": s["work_status"],
        "online_status": s["online_status"],
        "last_update": s["last_update"],
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        p.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        print(f"  status persist failed: {e}", flush=True)


def load_persisted() -> None:
    """Restore captures and last-known STATUS on startup."""
    for label, s in state.items():
        for kind, msg_type in (
            ("device_map", "DEVICE_MAP_V2"),
            ("path_list", "ALL_PATH_LIST_V2"),
            ("path_index", "ALL_PATH_INDEX_V2"),
        ):
            p = PERSIST_DIR / f"{label}_{msg_type}.json"
            if p.exists():
                try:
                    s[kind] = json.loads(p.read_text(encoding="utf-8"))
                    s[f"{kind}_at"] = datetime.fromtimestamp(
                        p.stat().st_mtime, tz=timezone.utc
                    ).isoformat()
                    print(f"loaded {p}", flush=True)
                except Exception as e:
                    print(f"load failed {p}: {e}", flush=True)

        sp = PERSIST_DIR / f"{label}_STATUS.json"
        if sp.exists():
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
                s["robot_x"] = data.get("robot_x")
                s["robot_y"] = data.get("robot_y")
                s["heading"] = data.get("heading")
                s["battery"] = data.get("battery")
                s["work_status"] = data.get("work_status")
                s["online_status"] = data.get("online_status")
                s["last_update"] = data.get("last_update")
                print(
                    f"loaded {sp} (pos={s['robot_x']},{s['robot_y']})",
                    flush=True,
                )
            except Exception as e:
                print(f"load failed {sp}: {e}", flush=True)

        # Fallback: derive last known position from path_list when STATUS
        # was never captured (e.g. mower has been offline since first boot).
        if s["robot_x"] is None and s.get("path_list"):
            try:
                pts = (
                    s["path_list"]["data"]["ALL_PATH_LIST_V2"]["pathList"][0]
                    .get("pathPointList", [])
                )
                if pts:
                    last = pts[-1]
                    s["robot_x"] = int(last.get("x", 0))
                    s["robot_y"] = int(last.get("y", 0))
                    s["last_update"] = s.get("path_list_at")
                    print(
                        f"fallback {label} pos from path_list last point: "
                        f"{s['robot_x']},{s['robot_y']}",
                        flush=True,
                    )
            except Exception as e:
                print(f"path_list fallback failed {label}: {e}", flush=True)


# ---------------------------------------------------------------------------
# MQTT listener
# ---------------------------------------------------------------------------

def handle_status(s: dict, status: dict) -> None:
    """Update mower state from a STATUS message."""
    x = status.get("robotX")
    y = status.get("robotY")
    heading = status.get("robotNavigation")
    if heading is None:
        heading = status.get("robotNav")  # older field name
    if x is None or y is None:
        return
    try:
        x = int(x)
        y = int(y)
    except Exception:
        return

    s["robot_x"] = x
    s["robot_y"] = y
    s["heading"] = heading
    s["battery"] = status.get("electricity")
    s["online_status"] = status.get("onlineStatus")
    s["last_update"] = (
        status.get("updateTime")
        or datetime.now(timezone.utc).isoformat()
    )

    # Trail: add a point if the mower has moved >5cm since the last sample
    if not s["trail"] or (
        abs(s["trail"][-1][0] - x) > 5 or abs(s["trail"][-1][1] - y) > 5
    ):
        s["trail"].append((x, y, time.time()))

    # Auto-expand the SVG viewport bounds with padding
    pad = 200
    s["min_x"] = min(s["min_x"], x - pad)
    s["max_x"] = max(s["max_x"], x + pad)
    s["min_y"] = min(s["min_y"], y - pad)
    s["max_y"] = max(s["max_y"], y + pad)

    # Throttled persist - only after a meaningful move or once per minute
    now = time.time()
    last_saved = s.get("_status_saved_at") or 0
    moved = (
        s.get("_status_saved_xy") is None
        or abs(s["_status_saved_xy"][0] - x) > 20
        or abs(s["_status_saved_xy"][1] - y) > 20
    )
    if moved or (now - last_saved) > 60:
        for label, v in state.items():
            if v is s:
                persist_status(label, s)
                break
        s["_status_saved_at"] = now
        s["_status_saved_xy"] = (x, y)


def on_message(client: mqtt.Client, userdata, msg):
    global _last_mqtt_message_at
    _last_mqtt_message_at = time.monotonic()
    try:
        payload_raw = msg.payload.decode("utf-8", errors="replace")
        payload = json.loads(payload_raw)
    except Exception:
        return

    msg_type = payload.get("msgType", "?")
    serial = msg.topic.rsplit("/", 1)[-1]
    label = serial_to_label.get(serial)
    if not label:
        return
    s = state[label]

    if msg_type == "STATUS":
        status = payload.get("data", {}).get("STATUS", {})
        handle_status(s, status)

    elif msg_type == "DEVICE_MAP_V2":
        s["device_map"] = payload
        s["device_map_at"] = datetime.now(timezone.utc).isoformat()
        persist_capture(label, "DEVICE_MAP_V2", payload_raw)
        print(
            f"  GOT DEVICE_MAP_V2 for {label} ({len(payload_raw)} bytes)",
            flush=True,
        )

    elif msg_type == "ALL_PATH_LIST_V2":
        # Sanity check: don't overwrite good data with empty/blank payload.
        # When the mower comes online or the app reconnects, the broker may
        # briefly send empty path data - keep the last known good map.
        new_pts = (
            payload.get("data", {}).get("ALL_PATH_LIST_V2", {})
            .get("pathList", [{}])[0].get("pathPointList", [])
        )
        existing_pts_count = 0
        if s.get("path_list"):
            try:
                existing_pts_count = len(
                    s["path_list"]["data"]["ALL_PATH_LIST_V2"]["pathList"][0]
                    .get("pathPointList", [])
                )
            except Exception:
                pass
        # Lenient threshold: a real "new zone" can legitimately drop the count
        # significantly without being bogus.
        if not existing_pts_count or len(new_pts) >= existing_pts_count * 0.10:
            s["path_list"] = payload
            s["path_list_at"] = datetime.now(timezone.utc).isoformat()
            persist_capture(label, "ALL_PATH_LIST_V2", payload_raw)
            print(
                f"  GOT ALL_PATH_LIST_V2 for {label} ({len(payload_raw)} bytes, {len(new_pts)} pts)",
                flush=True,
            )
        else:
            print(
                f"  SKIPPED stale ALL_PATH_LIST_V2 for {label} "
                f"({len(new_pts)} pts vs {existing_pts_count} existing)",
                flush=True,
            )

    elif msg_type == "ALL_PATH_INDEX_V2":
        s["path_index"] = payload
        s["path_index_at"] = datetime.now(timezone.utc).isoformat()
        # Only persist once - these messages are noisy and the same data
        # rides every refresh, no point overwriting the file every cycle.
        if not (PERSIST_DIR / f"{label}_ALL_PATH_INDEX_V2.json").exists():
            persist_capture(label, "ALL_PATH_INDEX_V2", payload_raw)

    elif msg_type == "REGION_TASK":
        # Used by render_svg() to pick up the mower's actual cutting width
        # (`mowingWidth`, in cm) so the cut-path stroke renders as a swept
        # area that matches the physical reality. Not persisted: this is
        # derived state and we'd rather a clean re-derive on restart than
        # a stale cached value.
        s["region_task"] = payload.get("data", {}).get("REGION_TASK", {})


def mqtt_idle_watchdog() -> None:
    """Force-exit the process when no MQTT message has arrived for
    WATCHDOG_IDLE_SECONDS. Run as a daemon thread.

    Recover-by-restart: the supervisor (k3s, Docker, HA Supervisor) respawns
    us with a fresh paho client. Necessary because paho's
    `loop_forever(retry_first_connection=True)` can wedge in a half-
    connected state after certain broker disconnects, never returning AND
    never reconnecting - the same pattern observed in the bridge.
    """
    while True:
        time.sleep(30)
        idle_for = time.monotonic() - _last_mqtt_message_at
        if idle_for > WATCHDOG_IDLE_SECONDS:
            print(
                f"watchdog: no MQTT message for {idle_for:.0f}s "
                f"(threshold {WATCHDOG_IDLE_SECONDS}s) - forcing process "
                f"exit so the supervisor respawns with a fresh paho client",
                flush=True,
            )
            os._exit(1)


def mqtt_listener() -> None:
    """Long-running thread: connect to local broker, subscribe per mower."""
    while True:
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"hookii-mower-map-{os.getpid()}",
            )
            if LOCAL_MQTT_USER:
                client.username_pw_set(LOCAL_MQTT_USER, LOCAL_MQTT_PASS)
            client.on_message = on_message

            print(
                f"connecting to MQTT {LOCAL_MQTT_HOST}:{LOCAL_MQTT_PORT}",
                flush=True,
            )
            client.connect(LOCAL_MQTT_HOST, LOCAL_MQTT_PORT, keepalive=60)

            # One per configured mower - keeps wildcard noise out of the
            # logs and means an unknown extra mower on the broker is silently
            # ignored rather than logged as "unknown serial".
            for label, s in state.items():
                topic = f"{TOPIC_PREFIX}/{s['serial']}"
                client.subscribe(topic, qos=0)
                print(f"  subscribed {topic} -> {label}", flush=True)

            client.loop_forever(retry_first_connection=True)
        except Exception as e:
            print(f"MQTT loop error: {e}", flush=True)
            try:
                client.disconnect()
            except Exception:
                pass
            time.sleep(5)


# ---------------------------------------------------------------------------
# SVG rendering
# ---------------------------------------------------------------------------

def extract_path_points(s: dict) -> list:
    """Return [(x, y, info), ...] where info=1 means "this segment is a cut
    swath" and info=0 means "transit only - the blade was idle".

    May 2026 cloud schema CHANGE: the per-point dict only carries `{x, y}` -
    the cut/transit classification lives on the SEGMENT in
    `ALL_PATH_INDEX_V2.indexInfoList`, expressed as
    `{startIndex, endIndex, info}`. Earlier addon versions read
    `point.info` which is now always 0 on this firmware - that made the
    entire path render as transit (thin/light) and the cut overlay never
    appeared, so the SVG looked like an uncoloured boundary regardless of
    how much the mower had actually mowed. Verified 2026-05-30 against
    Greenhouse capture: 31260 points, all `info=0`; segment index had
    39 entries with the real cut/transit alternation. Fix: project the
    segment-level `info` down to per-point info before decimating.
    """
    if not s.get("path_list"):
        return []
    try:
        pl = (
            s["path_list"].get("data", {}).get("ALL_PATH_LIST_V2", {})
            .get("pathList", [])
        )
        if not pl:
            return []
        points = pl[0].get("pathPointList", [])
        if not points:
            return []

        # Build a per-point info array from the segment index, if we have it.
        # Fall back to per-point .info (legacy / pre-May-2026 firmware), and
        # finally to 0 if neither channel exists.
        point_info = [None] * len(points)
        idx_segs = []
        if s.get("path_index"):
            try:
                idx_list = (
                    s["path_index"].get("data", {}).get("ALL_PATH_INDEX_V2", {})
                    .get("pathIndexList", [])
                )
                if idx_list:
                    idx_segs = idx_list[0].get("indexInfoList", []) or []
            except Exception:
                idx_segs = []

        for seg in idx_segs:
            try:
                start = int(seg.get("startIndex", 0))
                end = int(seg.get("endIndex", 0))
                info = int(seg.get("info", 0))
            except Exception:
                continue
            for i in range(max(0, start), min(end, len(point_info))):
                point_info[i] = info

        # Anywhere the segment-index didn't cover, fall back to per-point info
        # (legacy schema), then 0.
        for i, p in enumerate(points):
            if point_info[i] is None:
                point_info[i] = p.get("info", 0)

        # Decimate path + per-point-info in lockstep so the cut/transit
        # boundaries stay aligned in the rendered SVG.
        if len(points) > 4000:
            step = len(points) // 4000
            points = points[::step]
            point_info = point_info[::step]

        return [
            (p.get("x", 0), p.get("y", 0), info)
            for p, info in zip(points, point_info)
        ]
    except Exception:
        return []


def extract_boundary(s: dict) -> dict:
    """Pull the mowing-area + exclusion-area polygons from DEVICE_MAP_V2.

    Returns a dict::

        {
            "mowing":    [ [(x, y), ...], ... ],  # the yard territory
            "exclusion": [ [(x, y), ...], ... ],  # no-go zones (flower beds etc)
        }

    The captured payload nests these inside
    ``data.DEVICE_MAP_V2.mapDataList[0].{mowingAreaElementList, exclusionAreaElementList}``;
    each element has an ``elementPointList`` of point dicts ``{x, y, attr}``.
    Older builds of this addon searched top-level keys (``boundary`` /
    ``boundaryPoints`` / ``regionPoints`` / ``borderPoints`` / ``points``)
    which the real cloud schema doesn't use - silent miss made the boundary
    invisible.
    """
    out = {"mowing": [], "exclusion": []}
    if not s.get("device_map"):
        return out
    try:
        d = s["device_map"].get("data", {}).get("DEVICE_MAP_V2", {})
        if not isinstance(d, dict):
            return out

        # Modern shape (May 2026 cloud): nested under mapDataList[0]
        for map_entry in d.get("mapDataList", []) or []:
            if not isinstance(map_entry, dict):
                continue
            for src_key, dst_key in (
                ("mowingAreaElementList", "mowing"),
                ("exclusionAreaElementList", "exclusion"),
            ):
                for area in map_entry.get(src_key, []) or []:
                    pts = area.get("elementPointList") if isinstance(area, dict) else None
                    if not isinstance(pts, list) or not pts:
                        continue
                    poly = [
                        (p.get("x", p.get("posX", 0)), p.get("y", p.get("posY", 0)))
                        for p in pts if isinstance(p, dict)
                    ]
                    if len(poly) >= 3:
                        out[dst_key].append(poly)

        # Legacy shape fallback - older firmware may have used flat keys.
        if not out["mowing"]:
            for key in ("boundary", "boundaryPoints", "regionPoints",
                        "borderPoints", "points"):
                pts = d.get(key)
                if isinstance(pts, list) and pts:
                    poly = [
                        (p.get("x", p.get("posX", 0)), p.get("y", p.get("posY", 0)))
                        for p in pts if isinstance(p, dict)
                    ]
                    if len(poly) >= 3:
                        out["mowing"].append(poly)
                        break
    except Exception:
        pass
    return out


def render_svg(label: str, absolute_size: bool = False) -> str:
    s = state.get(label)
    if not s:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"/>'

    color = s["color"]
    if s["robot_x"] is None:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
            '<rect width="400" height="300" fill="#0f172a"/>'
            '<text x="20" y="40" fill="#94a3b8" font-family="sans-serif">'
            'Waiting for data...</text></svg>'
        )

    boundary = extract_boundary(s)
    # Apply the global ROTATE_DEG transform to every input point BEFORE the
    # bounding box is computed and BEFORE to_svg() projects to screen coords,
    # so the rotated content keeps the same translucent fill / cut overlay /
    # robot position layered the same way they always did. Rotation is a
    # rigid 2D transform; the relative geometry between path, boundary,
    # exclusion zones and robot position is preserved.
    boundary_mowing = [[_rotate_xy(x, y) for (x, y) in poly]
                       for poly in boundary["mowing"]]
    boundary_exclusion = [[_rotate_xy(x, y) for (x, y) in poly]
                          for poly in boundary["exclusion"]]
    _raw_path = extract_path_points(s)
    path_points_for_bounds = [(*_rotate_xy(p[0], p[1]), p[2]) for p in _raw_path]
    bound_pts: list = []
    if path_points_for_bounds:
        bound_pts = [(p[0], p[1]) for p in path_points_for_bounds]
    elif boundary_mowing:
        # Flatten all mowing polygons into one bounding sample
        for poly in boundary_mowing:
            bound_pts.extend(poly)
    robot_x_r, robot_y_r = _rotate_xy(s["robot_x"], s["robot_y"])
    bound_pts.append((robot_x_r, robot_y_r))

    if len(bound_pts) > 1:
        min_x = min(p[0] for p in bound_pts)
        max_x = max(p[0] for p in bound_pts)
        min_y = min(p[1] for p in bound_pts)
        max_y = max(p[1] for p in bound_pts)
        pad = 200
        min_x -= pad; max_x += pad; min_y -= pad; max_y += pad
    else:
        min_x = s["min_x"]; max_x = s["max_x"]
        min_y = s["min_y"]; max_y = s["max_y"]

    span_x = max(max_x - min_x, 2000)
    span_y = max(max_y - min_y, 2000)
    width = int(span_x)
    height = int(span_y)

    def to_svg(x, y):
        return (x - min_x, max_y - y)  # flip Y for SVG coords

    # absolute_size switches the SVG root between percentage and pixel
    # dimensions. Default (False) emits width="100%" height="100%" so the SVG
    # stretches to fill its parent - correct for the /page wrapper's JS swap
    # and any full-page browser view. /embed uses absolute_size=True so the
    # SVG carries its own pixel dimensions, which it must do when the parent
    # has no intrinsic size (HA `picture` / `picture-entity` cards render the
    # image via <img src>; a percentage-sized SVG with no resolvable parent
    # collapses to 0x0 inside the <img>).
    size_attr = (
        f'width="{width}" height="{height}"' if absolute_size
        else 'width="100%" height="100%"'
    )
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" {size_attr} '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">',
        f'<rect width="{width}" height="{height}" fill="#0f172a"/>',
    ]
    px = max(span_x, span_y) / 800  # 1 screen px in data units

    # Yard territory: render every mowing-area polygon as a translucent
    # light-green fill so the cut-coverage strokes layer on top the same way
    # the Hookii mobile app shows it ("light green = yet to mow, darker green
    # = mowed"). Each entry in DEVICE_MAP_V2.mapDataList[].mowingAreaElementList
    # is its own polygon (a Neomow can have multiple disjoint mowing zones).
    for poly in boundary_mowing:
        pts = " ".join(
            f"{to_svg(x, y)[0]:.1f},{to_svg(x, y)[1]:.1f}" for x, y in poly
        )
        svg.append(
            f'<polygon points="{pts}" fill="#86efac33" stroke="#86efac55" '
            f'stroke-width="{px*1:.1f}" stroke-linejoin="round"/>'
        )
    # Exclusion zones (flower beds, ponds, etc.) - punched out as a darker
    # opaque-ish fill on top of the mowing territory, so the user can see
    # where the mower will never reach. Matches the Hookii app convention.
    for poly in boundary_exclusion:
        pts = " ".join(
            f"{to_svg(x, y)[0]:.1f},{to_svg(x, y)[1]:.1f}" for x, y in poly
        )
        svg.append(
            f'<polygon points="{pts}" fill="#0f172acc" stroke="#475569" '
            f'stroke-width="{px*1:.1f}" stroke-linejoin="round"/>'
        )

    # Path coverage. Cut segments are rendered with a stroke-width equal to
    # the mower's actual cutting width (`mowing_width_cm`, see below) so that
    # adjacent parallel rows physically overlap and visually merge into one
    # continuous filled coverage polygon - the same look the Hookii mobile
    # app produces, instead of the "stripes with holes" you get when the
    # stroke is sized in pixel-equivalents rather than data units.
    #
    # We pull the cutting width out of the latest STATUS / REGION_TASK
    # payload when present, with a Neomow X Pro default of 23 cm (per
    # the protocol reference).
    mowing_width_cm = MOWING_WIDTH_DEFAULT_CM
    try:
        # The bridge fans out REGION_TASK to status if available; fall back
        # to taskInfo (newer cloud shape) or status root.
        rt = s.get("region_task") or {}
        mw = (
            rt.get("mowingWidth")
            if isinstance(rt, dict) else None
        )
        if isinstance(mw, (int, float)) and mw > 0:
            mowing_width_cm = float(mw)
    except Exception:
        pass

    # Reuse the rotated path computed above for bounds - re-calling
    # extract_path_points() here would drop the ROTATE_DEG transform and
    # render the cut/transit overlay in the un-rotated frame on top of a
    # rotated boundary (the v1.0.6 first-attempt bug).
    path_points = path_points_for_bounds
    if path_points:
        cut_segments: list[list[tuple]] = []
        transit_segments: list[list[tuple]] = []
        cur: list[tuple] = []
        cur_info = None
        for x, y, info in path_points:
            if info != cur_info:
                if cur:
                    (cut_segments if cur_info == 1 else transit_segments).append(cur)
                cur = []
                cur_info = info
            cur.append((x, y))
        if cur:
            (cut_segments if cur_info == 1 else transit_segments).append(cur)

        # Cut paths: stroke-width in DATA units (cm), large enough that the
        # 23-30cm row spacing on adjacent parallel rows guarantees overlap.
        # We multiply by a small fudge factor so neighbouring rows touch
        # cleanly even when the path was sampled sparsely.
        cut_stroke = max(mowing_width_cm * 1.4, px * 2)
        for seg in cut_segments:
            if len(seg) < 2:
                continue
            pts = " ".join(
                f"{to_svg(x, y)[0]:.0f},{to_svg(x, y)[1]:.0f}" for x, y in seg
            )
            svg.append(
                f'<polyline points="{pts}" fill="none" stroke="#22c55e" '
                f'stroke-width="{cut_stroke:.0f}" stroke-linecap="round" '
                f'stroke-linejoin="round" opacity="0.85"/>'
            )
        # Transit paths stay thin - they're not coverage, they're "the mower
        # was moving between zones without cutting" and should not visually
        # claim coverage area.
        for seg in transit_segments:
            if len(seg) < 2:
                continue
            pts = " ".join(
                f"{to_svg(x, y)[0]:.0f},{to_svg(x, y)[1]:.0f}" for x, y in seg
            )
            svg.append(
                f'<polyline points="{pts}" fill="none" stroke="#86efac" '
                f'stroke-width="{px*1:.1f}" opacity="0.4"/>'
            )

    if len(s["trail"]) > 1:
        # Trail also rotates through the global ROTATE_DEG transform so the
        # rendered path stays aligned with the boundary and current robot.
        rotated_trail = [_rotate_xy(x, y) for x, y, _ in s["trail"]]
        pts = " ".join(
            f"{to_svg(x, y)[0]:.0f},{to_svg(x, y)[1]:.0f}" for x, y in rotated_trail
        )
        svg.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="{px*2:.1f}" opacity="0.7"/>'
        )

    rx, ry = to_svg(robot_x_r, robot_y_r)
    r = px * 10
    svg.append(
        f'<circle cx="{rx:.0f}" cy="{ry:.0f}" r="{r:.0f}" fill="{color}" '
        f'stroke="#fff" stroke-width="{px*2:.1f}"/>'
    )
    if s["heading"] is not None:
        try:
            # The mower's reported heading is in its own local frame, so the
            # arrow gets the same ROTATE_DEG offset as the geometry.
            angle_rad = math.radians(float(s["heading"]) + ROTATE_DEG)
            ahx = math.sin(angle_rad) * px * 18
            ahy = -math.cos(angle_rad) * px * 18
            svg.append(
                f'<line x1="{rx:.0f}" y1="{ry:.0f}" '
                f'x2="{rx + ahx:.0f}" y2="{ry + ahy:.0f}" '
                f'stroke="#fff" stroke-width="{px*3:.1f}"/>'
            )
        except Exception:
            pass

    if s.get("last_update"):
        label_str = f"last fix: {s['last_update']}"
        fs = px * 18
        tx = px * 10
        ty = px * 24
        svg.append(
            f'<text x="{tx:.0f}" y="{ty:.0f}" fill="#e2e8f0" '
            f'font-family="sans-serif" font-size="{fs:.0f}" '
            f'stroke="#0f172a" stroke-width="{px*3:.1f}" paint-order="stroke">'
            f'{label_str}</text>'
        )

    svg.append('</svg>')
    return "\n".join(svg)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

app = FastAPI(title="Hookii Mower Map")


def _ingress_base(request: Request) -> str:
    # Home Assistant serves an ingress add-on under /api/hassio_ingress/<token>/
    # and sets the X-Ingress-Path request header to that prefix so in-page URLs
    # can be rebased. Absent on direct host:port access -> "" (plain /svg/.. ok).
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def _render_all(request: Request) -> HTMLResponse:
    base = _ingress_base(request)
    blocks = "".join(
        f'<div class="mower" data-label="{label}">'
        f'<h2>{label.upper()}</h2>'
        f'<div class="map"></div>'
        f'</div>'
        for label in state
    )
    html = (
        _ALL_HTML
        .replace("__BASE__", base)
        .replace("__BLOCKS__", blocks)
        .replace("__LABELS_JSON__", json.dumps(list(state.keys())))
    )
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    # The HA ingress sidebar panel ("Mower Map") opens the add-on ROOT. Serve
    # the all-mowers grid here so the panel shows the map, not a JSON blob.
    return _render_all(request)


@app.get("/api")
def api_index():
    # Machine-readable index (was previously served at "/").
    return {
        "mowers": [
            {"label": label, "serial": s["serial"], "color": s["color"]}
            for label, s in state.items()
        ],
        "endpoints": ["/svg/{label}", "/embed/{label}", "/page/{label}", "/state/{label}", "/all"],
    }


@app.get("/svg/{label}")
def get_svg(label: str):
    if label not in state:
        raise HTTPException(status_code=404, detail=f"unknown mower {label!r}")
    return Response(
        content=render_svg(label),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def _embed_response(label: str) -> Response:
    """Build the HA-friendly embed response for a known-good label.

    Same payload as /svg/{label} but with absolute pixel width/height and a
    slightly more permissive Cache-Control - see get_embed for the why.
    """
    return Response(
        content=render_svg(label, absolute_size=True),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/embed")
def get_embed_default():
    """Single-mower convenience: /embed returns the only configured mower.

    Most installs have one mower. Forcing the label into the URL is friction
    when there's only one candidate, so /embed (no label) returns it directly.
    For multi-mower setups, use /embed/<label> (raises 400 here so the user
    sees the available labels instead of silently picking one).
    """
    if len(state) == 1:
        return _embed_response(next(iter(state)))
    raise HTTPException(status_code=400, detail=(
        f"{len(state)} mowers configured - use /embed/<label>. "
        f"available labels: {', '.join(state.keys())}"
    ))


@app.get("/embed/{label}")
def get_embed(label: str):
    """SVG tuned for HA dashboard embedding (picture / picture-entity / iframe).

    Returns the same mower SVG as /svg/{label} but with two tweaks that make
    it render reliably inside HA cards:

    - Absolute width/height (viewBox pixel dims) instead of 100%/100%. The
      default SVG stretches to fill its parent - correct for the /page JS
      wrapper but collapses to 0x0 inside HA's `picture` / `picture-entity`
      cards, which render images via <img src="..."> where the parent has no
      intrinsic size. Embed contexts need the SVG to carry its own dimensions.
    - Cache-Control: no-cache, must-revalidate (drops the no-store directive
      that /svg/{label} uses). no-store blocks the conditional 304 re-fetch
      flow HA's image refresh uses when an entity state changes, so a card
      that should re-fetch on state change never does.

    For auto-refresh in an HA dashboard, the most reliable approach is still
    the iframe card with /page/<label> (HTML+JS). This endpoint is for cases
    where you specifically need a bare SVG URL (custom cards, external
    dashboards, picture-entity with state-driven refresh).
    """
    if label not in state:
        raise HTTPException(status_code=404, detail=f"unknown mower {label!r}")
    return _embed_response(label)


@app.get("/state/{label}")
def get_state(label: str):
    s = state.get(label)
    if not s:
        raise HTTPException(status_code=404, detail=f"unknown mower {label!r}")
    return {
        "serial": s["serial"],
        "robot_x": s["robot_x"],
        "robot_y": s["robot_y"],
        "heading": s["heading"],
        "battery": s["battery"],
        "trail_len": len(s["trail"]),
        "last_update": s["last_update"],
        "device_map_captured": s["device_map_at"],
        "path_list_captured": s["path_list_at"],
        "path_index_captured": s["path_index_at"],
    }


_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>__LABEL__</title>
<style>
  html, body { background:#0f172a; margin:0; padding:0; height:100%; overflow:hidden; }
  #wrap { width:100%; height:100%; display:flex; align-items:center; justify-content:center; }
  #wrap svg { width:100%; height:100%; display:block; }
</style></head><body>
<div id="wrap"></div>
<script>
  // JS-driven refresh: fetch the SVG every 10s and swap into the existing
  // container's innerHTML. No page-reload flash, no white frame in between.
  // We never throw away the previous frame until the new one has arrived,
  // and we cache-bust with a timestamp so any intermediate caches don't
  // serve stale frames.
  // BASE is the HA ingress path prefix (e.g. /api/hassio_ingress/<token>) when
  // shown in the sidebar panel, or "" on direct host:port access. Without it,
  // an absolute '/svg/..' escapes the ingress prefix and 404s -> blank map.
  const BASE = "__BASE__";
  const wrap = document.getElementById('wrap');
  async function tick() {
    try {
      const r = await fetch(BASE + '/svg/__LABEL__?t=' + Date.now(), { cache: 'no-store' });
      if (r.ok) {
        wrap.innerHTML = await r.text();
      }
    } catch (e) { /* swallow: try again next tick */ }
  }
  tick();
  setInterval(tick, 10000);
</script>
</body></html>"""


_ALL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hookii Mower Map</title>
<style>
  body { background:#0f172a; color:#f1f5f9; font-family:sans-serif; margin:0; padding:12px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(380px,1fr)); gap:12px; }
  .mower { background:#1e293b; border-radius:8px; padding:8px; }
  h2 { margin:4px 8px; font-size:14px; }
  .map { width:100%; aspect-ratio: 1.4 / 1; display:block; border-radius:4px; overflow:hidden; }
  .map svg { width:100%; height:100%; display:block; }
</style></head><body>
<div class="grid">__BLOCKS__</div>
<script>
  // Same no-blink swap pattern as /page, applied per-mower in the grid.
  // BASE rebases fetches onto the HA ingress prefix (see /page note).
  const BASE = "__BASE__";
  const labels = __LABELS_JSON__;
  async function refreshOne(label) {
    try {
      const r = await fetch(BASE + '/svg/' + label + '?t=' + Date.now(), { cache: 'no-store' });
      if (r.ok) {
        const el = document.querySelector('div[data-label="' + label + '"] .map');
        if (el) el.innerHTML = await r.text();
      }
    } catch (e) { /* try again */ }
  }
  function tick() { labels.forEach(refreshOne); }
  tick();
  setInterval(tick, 10000);
</script>
</body></html>"""


@app.get("/page/{label}", response_class=HTMLResponse)
def get_page(label: str, request: Request):
    if label not in state:
        raise HTTPException(status_code=404, detail=f"unknown mower {label!r}")
    base = _ingress_base(request)
    return _PAGE_HTML.replace("__BASE__", base).replace("__LABEL__", label)


@app.get("/all", response_class=HTMLResponse)
def get_all(request: Request):
    return _render_all(request)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

load_persisted()
threading.Thread(target=mqtt_listener, name="mqtt", daemon=True).start()
threading.Thread(target=mqtt_idle_watchdog, name="mqtt-watchdog", daemon=True).start()
print(
    f"hookii-mower-map ready: {len(state)} mower(s) configured: "
    f"{', '.join(state.keys())}",
    flush=True,
)
