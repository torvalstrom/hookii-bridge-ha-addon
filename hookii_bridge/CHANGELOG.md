# Changelog

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
