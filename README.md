# Neomow X Home Assistant Add-on

> ⚠️ **Requires Hookii BETA firmware `1.6.8.4-beta` or newer on every mower this bridge talks to.**
> The new cloud protocol this add-on speaks (`iot.beta.hookii.com`) is only live on the Hookii BETA channel. **Mowers on the stable firmware channel will not work.** Switch each mower to the Beta channel in the Hookii mobile app before installing.

This repository contains a single Home Assistant add-on:

- **Hookii Bridge** — a reverse-engineered Home Assistant integration for **Hookii Neomow** robot mowers. Two-way: it reads live telemetry AND sends control commands. Replaces the community workaround that broke when Hookii migrated their cloud in May 2026.

## What you actually get in Home Assistant

The add-on auto-publishes Home Assistant MQTT-Discovery configs, so as soon as it's running you get — per mower, no YAML required:

**Control (since v1.1.0)**

- A standard `lawn_mower.<your_mower>` entity with the built-in `start_mowing`, `pause` and `dock` services wired straight through to Hookii's cloud.
- Five command buttons: **Start**, **Pause**, **Return to dock**, **Stop (keep progress)**, **Stop (clear progress)**. Drop them on a Lovelace card or trigger them from automations.
- A safe two-step Start flow (pre-check + execute) with the default policy "resume from breakpoints" if any exist — so a "Start at 09:00" automation never accidentally discards the previous job.
- A schedule write/read channel with a built-in safety guard that refuses any schedule whose enabled window covers the current time (the Hookii cloud treats that as an implicit "start mowing now" — the guard stops automations from kicking the mower off the dock unexpectedly).
- Raw command topics under `hookii/cmd/<serial>/<action>` for anyone who wants to wire commands directly from a script or automation instead of via the buttons.

**Telemetry**

- 14 sensors per mower: battery percentage, blade RPM, voltage, charge current, four temperatures (battery / blade motor / left drive / right drive), WiFi signal, GPS satellite count, latitude, longitude, work status code, and a friendly "State" sensor ("mowing" / "returning" / "docked") derived from the mower's state machine.
- The full raw `hookii/details/device/<serial>` STATUS payload is also republished, so any existing template sensors, automations, n8n flows or dashboards you wrote against the old community workaround keep working.

## How it works under the hood

In May 2026 Hookii migrated their cloud from a passive-subscribe MQTT bus to a JWT-gated heartbeat protocol on `iot.beta.hookii.com`. The old "just subscribe to `hookii/details/device/<serial>`" trick stopped working overnight, and the official Hookii app became the only client that could see the new protocol.

This add-on:

1. Logs in to Hookii's REST API with your account (same email/password as the mobile app).
2. Opens an MQTT session to their new self-signed-TLS broker and keeps a heartbeat alive every 15 seconds — without that heartbeat, the cloud stops pushing STATUS.
3. Normalises the cloud's two different STATUS payload shapes into a single legacy-format shape so all your sensors read the same fields no matter which variant arrived.
4. Republishes everything to **your own Mosquitto broker** on the original `hookii/details/device/<serial>` topic.
5. Translates inbound MQTT publishes on `hookii/cmd/<serial>/<action>` into the right REST call against `/api/v1/mower/cmd/...`.
6. Publishes MQTT-Discovery configs so the lawn_mower entity, buttons and sensors above appear in Home Assistant automatically.

## Add this repository to your Home Assistant

1. In Home Assistant: **Settings → Add-ons → Add-on store**
2. Click the **⋮** menu (top right) → **Repositories**
3. Paste this URL:

   ```
   https://github.com/torvalstrom/hookii-bridge-ha-addon
   ```

4. Click **Add → Close**
5. Reload the Add-on store. **Hookii Bridge** appears under "Tor Valstrom Add-ons".
6. Click it → **Install**, then follow the [Hookii Bridge setup guide](hookii_bridge/DOCS.md) — it walks through the Hookii account credentials, your mower serial number(s) and the Mosquitto broker settings the add-on needs.

## Disclaimer

This add-on is **not affiliated with Hookii**. It is a community workaround built so existing Home Assistant integrations keep working after a vendor-side protocol change. The cloud MQTT broker certificate is self-signed and validated insecurely — same trade-off the official mobile app makes.

Licensed Apache-2.0.
