# Changelog

## 1.5.0-beta4 (2026-06-13)

**Adds a dedicated `/embed` path for embedding the mower SVG in HA dashboards.** The existing `/svg/<label>` route works for the in-panel JS refresh wrapper but is unusable inside HA `picture` / `picture-entity` cards: the SVG is emitted with `width="100%" height="100%"`, which stretches correctly inside a sized parent but collapses to 0×0 inside an `<img src="…">` tag (HA cards render image URLs that way), and the `Cache-Control: no-store` header blocks the conditional 304 re-fetch HA uses when an entity state changes. The new endpoint fixes both:

- **`GET /embed/<label>`** - the mower SVG with absolute pixel width/height (viewBox dims) and `Cache-Control: no-cache, must-revalidate`. Drop the URL into a `picture` / `picture-entity` card's `image:` field and the map will render.
- **`GET /embed`** (no label) - convenience for single-mower installs, returns the only configured mower directly. Returns 400 with the list of labels if you have more than one.

`/svg/<label>` is unchanged so the sidebar panel and any existing iframe setups keep working. For auto-refresh, the `iframe` card with `/page/<label>` is still the most reliable option (HTML + JS); `/embed` is for cases where you need a bare SVG URL.

> Via HA Ingress, the URL is `/api/hassio_ingress/<session-token>/embed/<label>`. The session token is the long hash you see in the browser URL bar when the Mower Map panel is open. For multi-user dashboards or external access, expose the add-on's host port (`8000/tcp`) and use `http://<ha-host>:8000/embed/<label>` directly.

## 1.5.0-beta3 (2026-06-13)

**Adds the missing `Error` binary_sensor that the auto-heal docking blueprint requires.** The "Neomow X - Auto-heal failed docking" blueprint asks you to pick an *error binary sensor*, but the bridge never actually published one - only the `Firmware upgrading` binary_sensor existed, so the dropdown had nothing usable to select. The bridge now publishes a per-mower **`binary_sensor.hookii_<serial>_error`**:

- Turns **on** when the cloud raises a `NOTICE_ALARM` (docking failure `514`/`515`, obstacle `116`, etc.), and exposes the code as the `notice.errCode` attribute the blueprint reads.
- **Self-clears** as soon as the mower is clearly OK again (drawing charge at the dock, or actively mowing), with a 30-minute safety timeout - so it can never get stuck `on`.
- Backed by a dedicated retained topic (`hookii/alarm/<serial>`) and seeded `off` at startup, so the entity is available immediately rather than `unknown`.

Also fixed a type bug in the auto-heal blueprint: it compared the (integer) `errCode` against a list of string codes, so `recover_alarm` would never have fired. Update the blueprint from the repo if you imported it earlier.

> The first time a real alarm fires, the bridge logs the raw `NOTICE_ALARM` body (truncated) so the exact cloud schema is captured; the code extractor is defensive (falls back to any `*code*` field) but this lets us tighten it if a firmware revision moves the field.

## 1.5.0-beta2 (2026-06-13)

**Fixes the Mower Map panel showing raw JSON / a blank map.** Two bugs in the bundled map (from beta1) are fixed:

- **Sidebar panel showed a JSON blob instead of the map.** The Home Assistant ingress panel opens the add-on's root URL (`/`), but that route returned a machine-readable JSON index. The root now serves the live all-mowers map grid (the JSON index moved to `/api`).
- **Map tiles stayed blank inside the panel.** The map's in-page requests for each mower's SVG used absolute paths (`/svg/...`) that escape the Home Assistant ingress prefix and 404. The map now rebases every request onto the ingress path (via the `X-Ingress-Path` header), so the tiles render in the sidebar panel as well as on direct host:port access.

No configuration change. If you were on beta1, just update to beta2 and reload the **Mower Map** panel.

## 1.5.0-beta1 (2026-06-12)

**The Mower Map is now built into the Bridge - one add-on instead of two.** The live SVG yard view (formerly the separate "Hookii Mower Map" add-on) now runs inside this add-on and appears automatically as a **Mower Map** panel in the Home Assistant sidebar. Big simplification for setup:

- **No second add-on to install**, and **no re-typing** your MQTT details or mower serials - the map reuses everything you already configured here.
- The map's mower list is built automatically from your `mower_serials`, so the old `mowers` field is gone.
- Two optional display tweaks were added: `map_trail_max` and `map_rotate_deg`.

The map is served over Home Assistant Ingress (the sidebar panel) - no host port needed. If you want direct host:port access, set the optional port in the Network section.

**Migration from the old separate add-on:** uninstall the old "Hookii Mower Map" add-on after updating this one. If you embedded the map in a Lovelace iframe card, change the URL slug from `hookii_mower_map` to `hookii_bridge` (e.g. `/hassio/ingress/hookii_bridge/all`).

> Beta note: this is a `-beta1` release for testing the merged image before it becomes the default. The bridge half is unchanged from 1.4.0; only the bundled map + launcher are new.

## 1.4.0 (2026-06-12)

**Friendlier setup for non-technical users.** Added a `translations/en.yaml` so the add-on Configuration screen now shows a plain-language name and help text for every field. The fields people get stuck on are now self-explanatory right in the UI: the dedicated-account requirement is spelled out on the email field, the serial-number field tells you where to find the code, beta-vs-prod explains which to pick, and every power-user field is clearly marked "(advanced) - leave blank". No behaviour change - purely a Home Assistant UI clarity improvement.

## 1.3.1 (2026-06-12)

**Production cloud now works out of the box - no manual MQTT credential needed.** A production traffic capture (HOOKII_1.1.0 build 191 against `iot.hookii.com`) confirmed the cloud MQTT broker uses the same username (`hookii-iot`) in both environments but a **different shared password** per environment. The bridge now ships **both** passwords and selects the correct one automatically from `hookii_env`, so `hookii_env: prod` connects telemetry without any extra setup (1.3.0 required pasting the password in by hand; that step is gone).

The `hookii_mqtt_user` / `hookii_mqtt_pass` options remain only as a manual override for the rare case where Hookii rotates the shared credential before a new add-on release ships the new value. DOCS, the options table and the `Bad user name or password` troubleshooting entry were updated to reflect the automatic behaviour.

The capture also confirmed the rest of the protocol is identical between environments (REST paths/ports, command codes, topic structure, STATUS schema, JWT-in-heartbeat authorization, modelCode `0002`) - only the hostname, the MQTT password and the app build number differ. Multi-mower was already handled (the required `mower_serials` field is comma-separated and the bridge builds one entity set per serial).

## 1.3.0 (2026-06-12)

**Production-cloud MQTT credential override.** The first real `hookii_env: prod` test surfaced that the production MQTT broker (`iot.hookii.com:8883`) uses a **different shared credential** than the beta broker - so the bridge's built-in (beta) credential is rejected on prod with `cloud-mqtt connect failed rc=Bad user name or password`. REST login, the device list and the command channel all work on prod; only the telemetry MQTT connection was failing.

Two new optional options expose the broker login so prod users can supply the production credential:

- `hookii_mqtt_user` - cloud MQTT broker username. Blank on beta (the bridge ships the beta broker's shared credential); **required on prod**.
- `hookii_mqtt_pass` - the paired password. Set both together for `hookii_env: prod`.

For Container / k3s / docker users (no Supervisor) the same is controlled by the existing `HOOKII_MQTT_USER` / `HOOKII_MQTT_PASS` env vars. DOCS gained an options-table entry, a "Beta vs Production cloud" credential note, and a `Bad user name or password` troubleshooting entry. No change to beta behaviour - beta users need not set anything.

## 1.2.9 (2026-06-12)

**Production-cloud support: the bridge can now run against Hookii's PRODUCTION cloud (`iot.hookii.com`), not just the beta backend.** Until now the bridge only ever connected to `iot.beta.hookii.com`, so mowers on stable firmware / a production Hookii account were out of luck. A new `hookii_env` option selects the backend:

- `hookii_env: beta` (default) → `iot.beta.hookii.com` (REST :10443, MQTT :8883), the protocol's reference backend. BETA firmware `1.6.8.4-beta+` emits the full STATUS here: the fine-grained `robotStatus` state machine, the granular per-system sensors, and firmware-upgrade awareness.
- `hookii_env: prod` → `iot.hookii.com` (same ports :10443 / :8883), the production cloud. Stable (production) firmware emits a sparser "Shape A" STATUS (`workingMode` only, often no `robotStatus`), so on prod you still get the core lawn_mower state (docked / mowing / returning, derived from `workingMode`), battery, the command buttons and Discovery - but NOT the granular sensors or the firmware-upgrade indicator. The bridge handles both shapes and degrades gracefully; nothing fails just because a mower is on stable firmware.

Two new optional advanced options, `hookii_rest_host` and `hookii_mqtt_host` (each a `host:port`), override the endpoints `hookii_env` selects - blank by default, only needed if a port ever differs from the presets. For Container / k3s / docker users (no Supervisor) the same is controlled by `HOOKII_ENV=beta|prod` plus the optional `HOOKII_REST_HOST` / `HOOKII_MQTT_HOST` env vars. The doc banners that previously said stable-firmware mowers "will not work" have been rewritten to reflect both clouds and the telemetry tradeoff.

## 1.2.8 (2026-06-11)

**Firmware-version sensor, firmware-upgrading binary_sensor, and command lockout during an OTA flash.** Two new auto-discovered entities per mower:

- `sensor.hookii_<SERIAL>_firmware_version` - the mower's reported firmware version.
- `binary_sensor.hookii_<SERIAL>_firmware_upgrading` - on while a firmware OTA is in progress.

During a firmware update (`robotStatus` 6) the bridge now **auto-disables all command buttons and the `lawn_mower` entity** (publishes availability offline) and drops any command it receives, so nothing can interfere with the flash. Availability is restored automatically when the OTA completes. These features need `robotStatus`, so they are **BETA-firmware only** (`hookii_env: beta`); on stable / production firmware the firmware-upgrade detection isn't available.

## 1.2.7 (2026-06-07)

**The add-on now ships as a prebuilt multi-arch image from GitHub Container Registry - Home Assistant downloads it instead of building it on your device.** Until now this add-on had no `image:` key, so every install/update made your Home Assistant build the Docker image locally from the Dockerfile. On slow or ARM hardware (Raspberry Pi etc.), or during a transient hiccup pulling the base image, that local build could fail or time out - which showed up as "update does not download / not possible to update". A GitHub Action now builds `amd64` / `aarch64` / `armv7` images on every release and pushes them to `ghcr.io/torvalstrom/<arch>-hookii-bridge`, and the add-on's `config.yaml` points at that image. Updates are now a fast registry pull, identical on every architecture. No configuration change is needed; this is purely how the image is delivered. (Code is unchanged from 1.2.6.)

## 1.2.6 (2026-06-06)

**New `Cutting Height` sensor, shipped with the CORRECT unit (mm).** The cloud reports `mowingHeight` (fanned out from `taskInfo`) as a millimetre value - raw `40` means 40 mm / 4 cm, not 40 cm (a mower obviously doesn't cut at 40 cm). Several users had hand-rolled a template sensor for this and mislabelled the unit as `cm`, so Home Assistant rendered a wrong "40,0 cm". The bridge now auto-discovers `sensor.hookii_<SERIAL>_cutting_height` with `unit_of_measurement: mm` and `device_class: distance`, so it reads correctly out of the box (and HA can convert to cm in the UI if the user prefers). Adds one entity; no change to existing sensors.

## 1.2.5 (2026-05-30)

**One image runs in BOTH the HA add-on Supervisor AND standalone (k3s, docker, compose).** The launcher (`run.sh`) probes for the Supervisor at start. If `bashio::supervisor.ping` answers, we are a bona fide add-on and hydrate env from `/data/options.json` via bashio (multi-account collapsed into the single-account form the add-on UI exposes). If the probe fails, we trust the env vars the operator already set in the Deployment / docker -e / compose env block and skip bashio entirely. The base image stays the official HA add-on Python base (so the add-on flavour is unchanged), and the second `Dockerfile.k3s` flavour added during 1.2.4 has been removed - there is one source, one image, one set of tests, both deployment shapes.

## 1.2.4 (2026-05-30)

Two reliability fixes after a real-world incident that produced 4 hours of "Unavailable" sensors in Home Assistant.

- **MqttWatchdog: recover-by-restart from stuck paho clients.** v1.2.3 added on_connect re-subscribe so HA-restart-driven broker hiccups would self-heal. That fix handles the case where paho fires both `on_disconnect` AND `on_connect`. It does NOT handle the case observed 2026-05-30 09:23 UTC where paho fired `on_disconnect` with `Unspecified error`, then sat for 4 hours without ever calling `on_connect` again. `client.is_connected()` returned `False` the entire time. No exception, no log line, just a silently dead bridge. The new watchdog is a daemon thread that wakes every 30 s and asks "how long since the last successful CONNACK?". If the answer is over 5 minutes AND we are still not connected, the process calls `os._exit(1)` so the supervisor (k3s/Docker/HA-Supervisor) respawns us with a fresh paho client. Wraps both the local broker client and every cloud-MQTT client (one per account).

- **STATUS republish is now `retain=True`.** Idle docked mowers can go many minutes without pushing a fresh STATUS, so without retain the broker has nothing to hand to HA after any restart (bridge, broker, HA itself). Every entity that reads from `hookii/details/device/<serial>` would show "Unavailable" until the cloud emitted the next change - which could be hours. With retain the last known state is replayed on subscribe and the dashboard recovers immediately. Trade-off: the value is "stale until the next cloud update" but that is strictly better than "missing for an unbounded time", and the dashboard's `last fix:` timestamp already tells the user how fresh it is.

## 1.2.3 (2026-05-30)

**Local MQTT cmd subscriber re-subscribes on every (re)connect.** Previously the bridge called `local.subscribe("hookii/cmd/+/+")` once at startup, but MQTT subscriptions live on the broker side and are lost whenever the broker (or the bridge's MQTT TCP connection) goes down even momentarily. After a broker restart, network blip or HA restart that bounces Mosquitto, the bridge stayed connected (paho-mqtt's `loop_start` auto-reconnects) but no longer received any button-press publishes - and there was nothing in the log to indicate why. Now wires `on_connect` to re-subscribe + `on_disconnect` to log the reason code; symptoms become visible AND self-healing.

## 1.2.2 (2026-05-30)

**`hookii/snapshot_meta/<serial>` is now ALSO published on declined captures**, with `{"status": "declined", "taken_at": "<ISO>", "reason": "..."}`. The successful payload now also includes the explicit `"status": "ok"` field so consumers can branch on `status` directly instead of inferring success from a missing field. This lets downstream HA dashboards show a friendly "robot unable to capture in current state" message when the cloud declines (deep sleep, charging-without-camera-active, etc.) - users no longer wonder whether the button worked at all when nothing visible happens. The JPG payload on `hookii/snapshot/<serial>` is unchanged. The legacy `hookii/result/<serial>/error` publish is also preserved for backwards compatibility.

## 1.2.1 (2026-05-30)

**Snapshot metadata topic for freshness-aware HA cards.** A successful camera capture now also publishes a small retained JSON payload to `hookii/snapshot_meta/<serial>` with `{"taken_at": "<ISO timestamp>", "size": <jpg-byte-count>}`. This lets HA template sensors evaluate "snapshot is fresh within the last N seconds" so dashboards can wrap the `picture-entity` card in a `conditional` card that only shows the image while it's recent - tackles the "image card takes too much space" complaint without losing the snapshot-on-demand capability. The JPG payload on `hookii/snapshot/<serial>` is unchanged.

## 1.2.0 (2026-05-30)

**Two new control commands, both reverse-engineered from a 2026-05-30 PCAP of the official Hookii Android app handling a docking-failure 515 incident.**

### `recover_alarm` — clear a remote-recoverable exception

When the mower trips a remote-recoverable error like *Docking failed (515): please check for any obstruction near the charging station*, the official app exposes a "slide OK to resolve" affordance that POSTs `/api/v1/mower/remote/recovery/alarm` with the same `reqOprType=0` then `reqOprType=1` polling pattern start/pause/return already use. The endpoint signals completion with `code=61` ("割草机远程恢复告警指令临时资源已过期" = "temporary resources expired") instead of the `result==1` marker `start/stop/job` uses; the new `cmd_recover_alarm_with_poll` understands this terminal state so it doesn't log code=61 as a failure.

Exposed as:

- A button entity per mower: `button.hookii_<serial>_recover_alarm` with the `mdi:auto-fix` icon and friendly label *Clear exception*.
- A raw MQTT topic for automations: `hookii/cmd/<serial>/recover_alarm` (payload `{}`).

Drop the button onto a Lovelace card or wire an HA automation triggered on a 515 error to auto-self-heal docking failures without manual intervention.

### `snapshot` — on-demand camera image from the mower

The same anomaly flow lets the app fetch a fresh photo from the mower's onboard camera. Two-step protocol PCAP-confirmed:

1. `POST /api/v1/mower/capture/image` → server returns `{result, fileName, fileUrl}`. The fileUrl is single-shot, hashed and short-lived.
2. `GET <fileUrl>` over HTTPS on port 9443 (a separate Hookii CDN host) → JPG bytes.

The bridge does both legs and republishes the JPG bytes retained to `hookii/snapshot/<serial>`. An auto-discovered MQTT camera entity (`camera.hookii_<serial>_last_snapshot`) shows the most recent one and survives HA restarts.

Exposed as:

- A button: `button.hookii_<serial>_snapshot` with the `mdi:camera` icon and label *Camera snapshot*.
- An MQTT camera: `camera.hookii_<serial>_last_snapshot` with `mdi:camera-iris`.
- Raw topics: `hookii/cmd/<serial>/snapshot` to trigger; `hookii/snapshot/<serial>` to consume.

Wire the camera entity into any `picture-entity` Lovelace card for a tap-to-refresh live yard view from anywhere the mower has 4G or WiFi.

### Discovery count

The discovery log now reports `19 entities` per mower (was 20 — but the 14 sensors + lawn_mower were already there; this commit grows the button set from 5 to 7 and adds the camera entity, so an existing install gains 3 new discoverable entities on next pod restart). The count line was previously double-counting the lawn_mower; corrected.

## 1.1.9 (2026-05-29)

**Extend the `deviceRegionTask` backward-compat alias to include `uncutArea`.** Discovered after v1.1.8 deploy that pre-May-2026 ETA-style templates also read `deviceRegionTask.uncutArea` (the old name for what the new cloud calls `unMowedArea`). v1.1.8 aliased `cutArea`/`mowedArea` and `mowingCoverageRate`/`mowingCoverage` but missed the un-cut counterpart; HA was logging `UndefinedError: 'dict object' has no attribute 'uncutArea'` for any template that extrapolated remaining time from the ratio. Now reconstructed alongside the other two aliases.

## 1.1.8 (2026-05-29)

**`deviceRegionTask` backward-compat alias in `normalise_status`.** Pre-May-2026 HA template sensors were written against an earlier Hookii cloud schema that nested mowing-task telemetry under `data.STATUS.deviceRegionTask` with slightly different field names (`cutArea` instead of `mowedArea`, `mowingCoverageRate` instead of `mowingCoverage`). The new cloud dropped that nested shape entirely in favour of `data.STATUS.taskInfo` with the renamed fields. Templates reading the old path were resolving to undefined and dashboards were showing "Unknown" / "0" for Cut / Region / Coverage / Height / Progress / Efficiency even after v1.1.7's `taskInfo` fan-out (because users' templates were not reading the fanned-out top-level fields - they were reading `deviceRegionTask.*`).

Bridge now reconstructs the legacy `deviceRegionTask` object from `taskInfo` whenever `taskInfo` is present and `deviceRegionTask` is not, including the two name aliases (`cutArea` ← `mowedArea`, `mowingCoverageRate` ← `mowingCoverage`). Legacy templates resume working without any configuration.yaml edits.

If you wrote your templates against the v1.1.7 fanned-out top-level fields (`value_json.data.STATUS.regionName` etc.), they continue to work too - both paths now resolve to the same values.

## 1.1.7 (2026-05-29)

**Systematic sensor audit fixes - three classes of bug discovered by walking every HA dashboard sensor against a captured raw STATUS payload from a live mowing mower.**

1. **`taskInfo` fan-out in `normalise_status`.** The new cloud payload nests mowing-task telemetry inside `data.STATUS.taskInfo`: `regionName`, `mowedArea`, `unMowedArea`, `mowingCoverage`, `mowingEfficiency`, `mowingHeight`, `taskProgress`, `executeTime`, `startTime`, etc. The bridge's existing fan-out logic only handled `chassisData`, so every legacy HA template sensor that reads e.g. `value_json.data.STATUS.regionName` was resolving to undefined and showing "Unknown" / "0" / blank. Fan-out copies these to top-level as a non-clobbering setdefault, exactly the same trick we already use for `chassisData`. Every legacy template sensor fixes itself without dashboard edits.

2. **WiFi signal unit corrected from `dBm` to `%` and the `signal_strength` device class dropped.** The cloud sends `wifiSignal` as a 0-100 signal quality value, not an RSSI dBm reading - HA dashboards were showing things like `25 dBm` (which is wrong both in unit AND would be physically implausible). The `signal_strength` device class in HA expects dBm and was triggering misleading "weak signal" classifications. Now reads as a clean `14 %` / `25 %` quality figure.

3. **Blade RPM absolute value.** The cloud encodes blade rotation direction (CW vs CCW) as a sign on `knifeDiscMotorSpeed`. HA users want a "blade spinning at N rpm" reading, not a vector - the negative was making dashboards display `-1841 rpm` when the blade was actively cutting. Bridge now publishes `abs(rpm)` so HA shows positive RPM whenever the blade is spinning. Rotation direction remains derivable from the sign of `knifeDiscMotorCurrent` if anyone needs it.

No HA dashboard / template-sensor edits are needed for any of these fixes - they all surface immediately after upgrading the bridge image. The legacy `electricity` / `chargeCurrent` / `voltage` / motor-temp sensors continue to read correctly as before.

## Docs (also in this release)

- README gains a full "Install path B: Home Assistant Container / Core / k3s" walkthrough so users on Docker-based HA installs (no Supervisor) can install + update the bridge using docker-compose / `docker run` against the repo's root `Dockerfile`. Includes env-var reference table and update commands. The existing Add-on Store path is now explicitly labelled "Install path A: Home Assistant OS / Supervised".
- Install path B compose example revised based on field feedback from the first user who set it up — adds the missing `network_mode: host` (required so the bridge can reach the host's Mosquitto on `127.0.0.1:1883` without Docker bridge-network DNS in the way), uses the `- KEY=VALUE` list form for `environment` instead of the `KEY: "value"` map form (more robust against YAML parsers that mis-handle colon-containing values like the `HOOKII_ACCOUNTS` triplet), and pulls the LABEL↔`HOOKII_SERIALS_<LABEL>` dependency out into an explicit warning callout so the link between the two env vars is impossible to miss.

## 1.1.6 (2026-05-29)

**Revert the v1.1.x sign-flip on `chargeDischargeCurrent`.** Sampling all 4 mowers in mixed states (docked-trickle-charging, mowing-actively, fully-charged-standby) showed both Shape A `chargeCurrent` and Shape B `chargeDischargeCurrent` use the SAME sign convention:

- **positive value** = current flowing INTO the battery (charging)
- **negative value** = current flowing OUT (mowing / discharging)

An earlier release introduced a sign-flip based on a one-off observation that turned out to be misread. The flip inverted the WHOLE table - every mower's `chargeCurrent` sensor read the opposite of reality. Now passed through unchanged. The `work_status` template's `c > 0 == charging, c < 0 == mowing` logic reads correctly again.

(No HA-side dashboard change needed - same `chargeCurrent` sensor name, just correctly signed values.)

## 1.1.5 (2026-05-29)

**Detect `code=10` "token 失效" + re-login retry.** Resolves the "I press Dock or Start and nothing happens" symptom that 1.1.3's polling alone didn't fully fix.

Hookii's server returns HTTP 200 with `{"code":10,"msg":"token 失效"}` (= "invalid token") in the JSON body when a JWT has been demoted server-side - which appears to happen aggressively when another client logs in for the same user, or when the bridge's heartbeat session gets remapped. The bridge was treating this as a normal error response and giving up; commands silently failed.

`_hookii_post` now treats `code:10` the same way as HTTP 401: re-login through `/api/v1/user/login/email`, refresh the JWT in-place, and retry the original POST once. Verified live: a return command that previously logged `code=10 msg=token 失效` now logs `re-login + retry` then `cmd 1 finalised after 10 poll(s)`.

## 1.1.4 (2026-05-29)

**Heartbeat `push` field is now a per-session constant (was a monotonic counter).** Likely root cause of the "mobile app logs me out every ~hour" symptom that 1.1.2 alone didn't fully resolve.

PCAP analysis of the Android app showed the `push` field stays at exactly the same value across every heartbeat in a session - 23 across 18 consecutive heartbeats in the Greenhouse-recharge capture, never increments. The protocol-reference doc that called it a "monotonic counter" turns out to be wrong: it's a session-instance identifier the server probably uses together with `loginId` as a logical session key.

When the bridge sent an ever-incrementing counter, the server saw heartbeats with the same `loginId` (= user) but different `push` (= "client instance") as competing sessions - applied its single-session-per-user policy - and evicted whichever it considered "older" between bridge and phone app. With both now using the same constant push value the server should treat them as one consistent session.

`push` is now initialised to 23 on AccountClient construction (per-session) and never increments. New connections (e.g. after a SIGTERM-restart) get a fresh AccountClient with the same constant value.

## 1.1.3 (2026-05-29)

**Commands now poll the server until completion (matches mobile app).** Fixes the "I pressed Dock and nothing happened" symptom on a contact-fault / stuck mower.

PCAP analysis of the Android app pressing Recharge while a mower was stuck idle in its dock showed the app issues the command with `reqOprType=0` once, then polls with `reqOprType=1` every ~2-3 seconds until the response carries `result=1` (or `waitingProgressInfo` becomes null). Previous bridge versions sent the initial command only and considered the operation "submitted" - which the server treats as best-effort and silently drops if the mower needs nudging out of a stuck state.

`cmd_start_stop_with_poll()` now wraps all start/pause/return/stop and start-execute calls with the same poll loop (2.5s cadence, 30s timeout). Schedule and params endpoints are still single-shot since they're already idempotent reads/writes.

## 1.1.2 (2026-05-29)

**Heartbeat cadence aligned with the Hookii mobile app (1.5s, was 15s).** Strongly recommended update for everyone running 1.1.x.

PCAP analysis of the official Hookii Android app's MQTT traffic showed it heartbeats to `hk/app/mower/hb/<model>/<serial>` at exactly 1.5 second intervals (variance < 1ms). The bridge was at 15s, which created a session-aging mismatch on Hookii's server: it likely treated the bridge as a "slower / older" session relative to a concurrently active phone app, and intermittently evicted one or the other. Symptom: mobile app silently logged out every ~hour while the bridge was running.

Changes:

- `heartbeat_sec` is now a float (was int). Defaults to `1.5` (was `15`). Sub-second sleep loop still polls the stop signal at 0.5s granularity so SIGTERM behaves.
- The add-on's `heartbeat_seconds` option still accepts an int, but 1 (the closest representable to the spec) is the sensible floor; the addon's run.sh forwards the value through `HEARTBEAT_SEC` env so power users can override at sub-second precision via env if they're running outside the add-on.

If your phone app has been getting logged out periodically since installing earlier 1.1.x, that should stop after upgrading to 1.1.2.

## 1.1.1 (2026-05-29)

- Add `robotStatus = 4` to the "docked" derivation. Observed live on at least one Pro mower at trickle-charge state; not documented in the protocol reference but always co-occurs with `workingMode = 0` and near-zero `chargeCurrent`. Without this, `ha_state` would stay unset on those payloads and the `lawn_mower` entity would flap to "previous state".

## 1.1.0 (2026-05-29)

**New: REST command channel + MQTT Discovery.** Big release.

- Adds a REST-command wrapper around `iot.beta.hookii.com/api/v1/mower/cmd/...` so the add-on can SEND commands (start, pause, return-to-dock, stop, schedule R/W, params R), not just receive STATUS. Commands are published to `hookii/cmd/<serial>/<action>` on your local Mosquitto broker; the add-on translates each publish into the right REST call with auto re-login on 401.
- Adds the two-step Start flow (cmd=7 pre-check then cmd=6 execute) with the default policy of "resume from breakpoints if any exist". Safer than the alternative for automations.
- Adds a derived `ha_state` field in normalised STATUS payloads ("mowing" / "returning" / "docked") computed from `robotStatus` + `workingMode` per the reverse-engineered state machine. Lets the discovered `lawn_mower` entity reflect the mower's activity without per-user template logic.
- Publishes Home Assistant MQTT-Discovery configs per mower: one `lawn_mower` entity, five command buttons (Start / Pause / Return to dock / Stop keep / Stop clear) and 14 telemetry sensors (battery, blade RPM, voltage, charge current, motor temps, GPS, work status, friendly state). Users no longer need to paste the legacy YAML block. The DOCS-section that documents that block is kept under "Optional: paste-by-hand sensor YAML" for v1.0.x compatibility.
- Schedule-write safety guard: writes that would set an `enable: true` task whose window covers the current minute-of-day are rejected with an explanatory error published to `hookii/result/<serial>/error`. The Hookii cloud treats such a schedule as an implicit start command and would otherwise make the mower start mowing immediately, even if it was returning to dock.
- New config options: `hookii_agent` (REST client fingerprint, default Android/Xiaomi), `enable_discovery` (default true), `discovery_prefix` (default homeassistant).
- Action results: `schedule_read` / `params_read` publish to `hookii/result/<serial>/{schedule,params}` (retained) so automations can subscribe.

## 1.0.4 (2026-05-29)

- Auto-lowercase `hookii_email` at config parse, mirroring the auto-uppercase MD5 detection in `md5_upper`. The Hookii beta REST API treats email as case-sensitive on user lookup; rather than make users notice + remember that, the bridge now normalises whatever they type. Existing installs with already-lowercased emails are unaffected. Docs still mention the original gotcha so users searching for the chinese error message find context.

## 1.0.3 (2026-05-29)

- Docs: warn that the Hookii beta server's user lookup is **case-sensitive on email**. An account registered as `Foo@bar.dk` must be entered as `foo@bar.dk` in `hookii_email`; the upper-case form returns `code: 5, msg: 该用户未注册` ("user not registered") and the login fails. Discovered live with a second beta account where capital letters in the address blocked the login until we lowercased them. Added an explicit lowercase instruction on the `hookii_email` field and a new troubleshooting entry.

## 1.0.2 (2026-05-29)

- Docs: prominent warning across the repo README, add-on README, DOCS.md, config description and troubleshooting section that this add-on **requires Hookii BETA firmware `1.6.8.4-beta` or newer**. Mowers on stable firmware do not push to `iot.beta.hookii.com` and the bridge has no way to make the cloud talk to them. Added explicit prerequisite step and a troubleshooting entry for the "REST login OK but no STATUS arriving" symptom that stable-firmware users will hit.

## 1.0.1 (2026-05-29)

- Docs-only: scrubbed Conscient Systems / k3s-specific references from `bridge.py` docstrings so the file reads cleanly as a standalone Python script for HA OS users (or for anyone else who wants to run it under systemd / Docker Compose / etc.). No behaviour change.

## 1.0.0 (2026-05-29)

- Initial public release.
- Logs in to `iot.beta.hookii.com` REST API with your Hookii account, opens MQTT session over self-signed TLS, keeps a 15 s heartbeat alive so the cloud keeps pushing STATUS for your mower(s).
- Normalises both protocol payload shapes (legacy `electricity` / new `battery`, flat motor temps / nested `chassisData`, top-level `workStatus` / nested `workTimeStatusInfo.workStatus`) so all existing Home Assistant template sensors keep working.
- Auto-learns per-mower model code from the first observed STATUS push (works for Neomow X Pro `0002` and any non-Pro variant your account owns).
- Accepts either cleartext password or a 32-character MD5-uppercased hash in `hookii_password` (auto-detected).
