# Changelog

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
