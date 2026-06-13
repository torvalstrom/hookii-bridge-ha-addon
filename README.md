# Neomow X Home Assistant Add-on

> 🚀 **New to this? Start with the [simple step-by-step guide](GETTING_STARTED.md).**
> It's a plain-language, 20-minute walkthrough from nothing to a working mower in
> Home Assistant - no prior knowledge needed. The rest of this README is the full
> reference once you're up and running.

> ℹ️ **Works against BOTH the Hookii BETA cloud and the PRODUCTION cloud.**
> Pick the backend with the `hookii_env` option (add-on) or the `HOOKII_ENV` env var (Container / k3s): `beta` → `iot.beta.hookii.com` (default), `prod` → `iot.hookii.com`. **BETA firmware `1.6.8.4-beta` or newer** unlocks the richest telemetry (granular sensors + firmware-upgrade awareness); mowers on **stable / production firmware** work for the core features (state, battery, command buttons, Discovery) with fewer sensors. The bridge handles both and degrades gracefully - it never fails just because a mower is on stable firmware. See [DOCS.md](hookii_bridge/DOCS.md) "Beta vs Production cloud" for the full tradeoff and how to choose.

> 🚨 **Use a DEDICATED Hookii account for the bridge, not your primary account.**
> Hookii's server only keeps ONE active session per account. If the bridge and your phone app share the same account they will silently evict each other's sessions every few minutes, and you will be permanently logged out of the mobile app.
> **Fix (5 minutes, no code):** create a second Hookii account, then from your primary account share each mower to it via the mobile app's Device Sharing menu. Configure the add-on with the new bridge account's credentials. See [DOCS.md](hookii_bridge/DOCS.md) for the full walk-through.

This repository ships the **Hookii Bridge** add-on:

- **Hookii Bridge** ([`hookii_bridge/`](hookii_bridge/)) — a reverse-engineered Home Assistant integration for **Hookii Neomow** robot mowers. Two-way: it reads live telemetry AND sends control commands. Replaces the community workaround that broke when Hookii migrated their cloud in May 2026. **The live Mower Map is now built in** (since v1.5.0) and appears as a sidebar panel — see [The Mower Map](#the-mower-map--built-in-nothing-to-install) below.

> The `hookii_mower_map/` directory is the **legacy standalone** Mower Map add-on, kept only for users who haven't migrated yet. New installs do not need it — the map is part of the bridge.

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

## The Mower Map — built in, nothing to install

**Since v1.5.0 the Mower Map is part of this add-on — there is no separate add-on to install.** It renders a live SVG view of each mower's yard: the boundary polygon (when the cloud has streamed it), every cut path the mower has driven (thick green), every transit path it took without cutting (thin light green), the live trail in your chosen colour, and the mower itself with a heading arrow. It updates every 10 seconds.

### How to open it

When the add-on starts, Home Assistant adds a **Mower Map** entry to the left sidebar (it is served over HA Ingress — `ingress: true`). Click it and you get a grid with one tile per mower. **Nothing extra to configure** — the map reuses this add-on's MQTT settings and builds its mower list automatically from the `mower_serials` you already set.

Each mower's URL slug (its "label") is its **serial in lower-case** — e.g. serial `HKX1EB100JD25010115` → label `hkx1eb100jd25010115`.

### Put it on a dashboard (optional)

To embed the map in a Lovelace **iframe** card (auto-refreshing, the recommended option):

```yaml
type: iframe
url: /hassio/ingress/hookii_bridge/page/hkx1eb100jd25010115
aspect_ratio: 100%
```

For a **picture** card instead (a single static SVG snapshot — picture cards can't run the JavaScript that drives `/page`):

```yaml
type: picture
image: /hassio/ingress/hookii_bridge/embed/hkx1eb100jd25010115
```

Use the lower-cased serial as the slug. For a side-by-side grid of every configured mower in a single card, point the iframe URL at `/all` instead (`/hassio/ingress/hookii_bridge/all`).

### As a Lovelace tile that re-fetches on mower state changes

For a map tile that sits alongside your other mower sensors (and works in `picture-glance` / `picture-elements` cards with state badges on top), use a `picture-entity` card bound to one of the mower's sensors — HA re-renders the card and re-fetches the SVG every time that sensor's state changes:

```yaml
type: picture-entity
entity: sensor.hookii_hkx1eb100jd25010115_ha_state
name: Mower Map
image: /hassio/ingress/hookii_bridge/embed/hkx1eb100jd25010115
show_name: false
show_state: true
```

Swap `entity:` to `sensor.hookii_<slug>_latitude` for finer-grained refresh while the mower is moving. This is state-driven (no refresh while parked); for a continuous 10-second cadence use the iframe above. Full recipe + sensor-choice table is in [`hookii_bridge/DOCS.md`](hookii_bridge/DOCS.md).

The map starts blank and switches to a rendered yard as soon as the bridge republishes the first `STATUS` payload (usually within seconds). Boundary polygons appear when the cloud first streams `DEVICE_MAP_V2`, which can take minutes to hours after the mower comes online; live position and trail render immediately.

> **Migrating from the old separate "Hookii Mower Map" add-on?** Uninstall it — its job is now done by the bridge. Change any Lovelace iframe URLs from `/hassio/ingress/hookii_mower_map/...` to `/hassio/ingress/hookii_bridge/...`.

Full configuration reference, env-var-driven setup for Container/k3s users, and troubleshooting are in [`hookii_bridge/DOCS.md`](hookii_bridge/DOCS.md).

## Commands & sensors reference (Hookii Bridge)

Everything in this table is auto-published via MQTT Discovery on every bridge startup. Drop the entity into a Lovelace card, call it from an automation, or just `mosquitto_pub` to the raw MQTT topic — both paths are equivalent.

### Buttons (control)

| HA entity | MQTT topic to trigger | What happens |
|---|---|---|
| `button.hookii_<SERIAL>_start` | `hookii/cmd/<SERIAL>/start` | Two-step start (pre-check + execute). Resumes from breakpoints when any exist, so a "Start at 09:00" automation won't accidentally discard the previous job. |
| `button.hookii_<SERIAL>_pause` | `hookii/cmd/<SERIAL>/pause` | Pause the current job. |
| `button.hookii_<SERIAL>_return` | `hookii/cmd/<SERIAL>/return` | Return to dock immediately. |
| `button.hookii_<SERIAL>_stop_keep` | `hookii/cmd/<SERIAL>/stop_keep` | Stop the current job **but keep progress** so the next Start resumes from where it stopped. |
| `button.hookii_<SERIAL>_stop_clear` | `hookii/cmd/<SERIAL>/stop_clear` | Stop the current job AND clear progress so the next Start mows the whole region fresh. |
| `button.hookii_<SERIAL>_recover_alarm` | `hookii/cmd/<SERIAL>/recover_alarm` | Self-heal a remote-recoverable exception (e.g. *Docking failed (514)*). Equivalent to the "slide OK to resolve" affordance in the Hookii mobile app. Hookii server signals completion with the cryptic `code=61` "temporary resources expired" — bridge handles that as success, not failure. |
| `button.hookii_<SERIAL>_snapshot` | `hookii/cmd/<SERIAL>/snapshot` | Trigger an on-demand camera capture. See the camera-snapshot subsection below for the full flow. |

### Lawn mower entity

| HA entity | What it does |
|---|---|
| `lawn_mower.hookii_<SERIAL>_mower` | Standard HA `lawn_mower` device with `start_mowing`, `pause` and `dock` services wired straight through to the bridge. Use this if you want the built-in HA UI affordances instead of the raw buttons above. The `activity` attribute tracks the derived `ha_state` ("mowing" / "returning" / "docked"). |

### Camera entity + snapshot workflow

| HA entity | What it does |
|---|---|
| `camera.hookii_<SERIAL>_last_snapshot` | Latest captured JPG. Updates whenever a snapshot succeeds; persists across HA restarts thanks to MQTT retain. |

The snapshot flow:

1. Press the `Camera snapshot` button (or publish `{}` to `hookii/cmd/<SERIAL>/snapshot`).
2. Bridge POSTs `/api/v1/mower/capture/image` to Hookii's cloud.
3. **If the cloud accepts** (mower is awake + reachable + camera available): bridge downloads the resulting JPG from Hookii's CDN, republishes the bytes to `hookii/snapshot/<SERIAL>` (retained) AND publishes `{"status": "ok", "taken_at": "<ISO>", "size": <bytes>}` to `hookii/snapshot_meta/<SERIAL>` (retained). The camera entity updates within ~5 seconds.
4. **If the cloud declines** (mower asleep / charging-without-camera-active / firmware update / etc): bridge publishes `{"status": "declined", "taken_at": "<ISO>", "reason": "..."}` to `hookii/snapshot_meta/<SERIAL>` instead. The camera entity does NOT update; the legacy `hookii/result/<SERIAL>/error` topic also gets a publish for back-compat.

Use the `status` field on `hookii/snapshot_meta/<SERIAL>` (e.g. via a template sensor that reads `{{ value_json.status }}`) to drive a Lovelace `conditional` card that either shows the picture OR shows a "robot unable to capture in current state" message, so users know the button worked even when nothing visible happens.

### Telemetry sensors (one per mower)

| HA entity | Source field | Unit | Notes |
|---|---|---|---|
| `sensor.hookii_<SERIAL>_battery` | `electricity` (or Shape B `battery`) | % | Battery state-of-charge. |
| `sensor.hookii_<SERIAL>_blade_rpm` | `knifeDiscMotorSpeed` (abs) | rpm | Positive when cutting; sign-of-rotation is stripped so dashboards don't show negative RPM during normal mowing. |
| `sensor.hookii_<SERIAL>_voltage` | `voltage` | V | |
| `sensor.hookii_<SERIAL>_charge_a` | `chargeCurrent` (or Shape B `chargeDischargeCurrent`) | A | **Positive = current INTO battery (charging)**, **negative = current OUT (mowing or idle discharge)**. |
| `sensor.hookii_<SERIAL>_temp_battery` | `batteryTemp` | °C | |
| `sensor.hookii_<SERIAL>_temp_blade` | `knifeDiscMotorTemp` | °C | Blade-motor body temp. |
| `sensor.hookii_<SERIAL>_temp_left` | `leftDriveMotorTemp` | °C | Left drive-motor body temp. |
| `sensor.hookii_<SERIAL>_temp_right` | `rightDriveMotorTemp` | °C | Right drive-motor body temp. |
| `sensor.hookii_<SERIAL>_wifi_signal` | `wifiSignal` | % | Signal QUALITY 0-100 (NOT dBm despite what older docs said; verified live 2026-05-29). |
| `sensor.hookii_<SERIAL>_cutting_height` | `mowingHeight` (from `taskInfo`) | mm | Cutting height in MILLIMETRES (raw `40` = 40 mm = 4 cm, NOT 40 cm). `device_class: distance`, so HA can show cm in the UI if preferred. |
| `sensor.hookii_<SERIAL>_satellite` | `satellite` | (count) | Visible GPS satellites. |
| `sensor.hookii_<SERIAL>_latitude` | `latitude` | ° | Decimal degrees. |
| `sensor.hookii_<SERIAL>_longitude` | `longitude` | ° | Decimal degrees. |
| `sensor.hookii_<SERIAL>_work_status` | `workStatus` | (int) | Raw workStatus code (1=mowing, 2=mowing-active, 3=returning, 5=charging, etc). |
| `sensor.hookii_<SERIAL>_state` | derived `ha_state` | (text) | Friendly "mowing" / "returning" / "docked" — pre-computed from `robotStatus` + `workingMode`. Use this on dashboards instead of `work_status`. |

### Raw MQTT topics

All telemetry the bridge receives gets republished verbatim (after normalisation) to `hookii/details/device/<SERIAL>`. If you have legacy template sensors written against that topic format from the pre-May-2026 community workaround, they keep working unchanged — the bridge runs a `normalise_status` pass that reconstitutes the old field shapes (`deviceRegionTask`, `cutArea`, `uncutArea`, etc.) from the new cloud's `taskInfo` payloads so nothing breaks.

For control commands not exposed as discovery buttons (`schedule_read`, `schedule_write`, `params_read`):

```bash
mosquitto_pub -h <broker> -u <user> -P <pass> \
  -t 'hookii/cmd/HKX1EB100JD25010115/schedule_read' \
  -m '{}'
# Result echoes back to hookii/result/HKX1EB100JD25010115/schedule (retained).
```

## Ready-made automations (Blueprints)

This repo ships a small collection of [Home Assistant Blueprints](https://www.home-assistant.io/docs/automation/using_blueprints/) that solve common Neomow X owner pain points using the bridge's MQTT command channel. Each blueprint is a single YAML file in [`blueprints/automation/torvalstrom/`](blueprints/automation/torvalstrom/) and imports into HA in two clicks.

### Auto-heal failed docking (kissing dock + alarm 514/515)

The Neomow X's flat-pad charging contacts oxidise and accumulate dust quickly in dry/hot weather; the dock-side spring fingers also oxidise. The combined effect: the mower often "kisses" the dock without making electrical contact (`chargeCurrent` flapping near 0 A), or throws docking alarm `514` / `515`. The older Neomow firmware had a built-in self-heal that re-docked automatically until contact succeeded - Hookii removed that behaviour in a later revision even though it worked well. This blueprint restores the equivalent in HA.

**What it does**

- **Path A - explicit alarm:** when the mower posts `errCode` `514`/`515` and stays in error for 60 s → publishes `hookii/cmd/<SERIAL>/recover_alarm` (clears the alarm, same as sliding "OK to resolve" in the Hookii app).
- **Path B - kissing dock:** when battery is below the threshold AND `chargeCurrent` has been flapping inside `[-1, 1]` A for 60 s → publishes `hookii/cmd/<SERIAL>/recharge`, which the bridge translates to the same `start/stop/job` cloud call the mobile app's "Recharge" button makes. The mower physically undocks and redocks.
- **2-minute safety net:** re-evaluates conditions every 2 min so multi-attempt recovery (often needed when the contact problem is bad) happens automatically.

**Import to Home Assistant**

[![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Ftorvalstrom%2Fhookii-bridge-ha-addon%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Ftorvalstrom%2Fneomow_x_kissing_dock_auto_heal.yaml)

Or, if your HA isn't connected to [My Home Assistant](https://my.home-assistant.io/): in HA go to **Settings → Automations & Scenes → Blueprints → Import Blueprint** and paste:

```
https://github.com/torvalstrom/hookii-bridge-ha-addon/blob/main/blueprints/automation/torvalstrom/neomow_x_kissing_dock_auto_heal.yaml
```

Then click **Create Automation** on the imported blueprint and fill in the inputs:

| Input | What to pick |
|---|---|
| **Mower serial number** | The serial printed under the mower (also visible in the Hookii app under Device Info). Used to build the MQTT command topic. |
| **Battery sensor** | The bridge's `sensor.hookii_<SERIAL>_battery` entity. The dropdown auto-filters to `device_class: battery` sensors. |
| **Charge current sensor** | The bridge's `sensor.hookii_<SERIAL>_charge_a` entity. The dropdown auto-filters to `device_class: current` sensors. |
| **Error binary sensor** | The bridge's `binary_sensor.hookii_<SERIAL>_error` entity (the `notice` attribute carries the `errCode` field this blueprint reads). |
| **Battery threshold** | Path B only fires when battery is below this. Default 20 % - safe against a healthy 90-95 % top-up plateau, low enough to catch failed dockings before the mower runs flat. |
| **Notification service** *(optional)* | E.g. `notify.mobile_app_yourphone`. Leave empty to run silently. |

One automation instance per mower - if you have three Neomows, create three automations from the same blueprint with different inputs.

## How it works under the hood

In May 2026 Hookii migrated their cloud from a passive-subscribe MQTT bus to a JWT-gated heartbeat protocol, first on the beta cloud `iot.beta.hookii.com` and now on the production cloud `iot.hookii.com` too. The old "just subscribe to `hookii/details/device/<serial>`" trick stopped working overnight, and the official Hookii app became the only client that could see the new protocol. The bridge speaks to whichever cloud you select with `hookii_env` / `HOOKII_ENV` (`beta` by default, `prod` for stable-firmware mowers on a production account).

This add-on:

1. Logs in to Hookii's REST API with your account (same email/password as the mobile app).
2. Opens an MQTT session to their new self-signed-TLS broker and keeps a heartbeat alive every 15 seconds — without that heartbeat, the cloud stops pushing STATUS.
3. Normalises the cloud's two different STATUS payload shapes into a single legacy-format shape so all your sensors read the same fields no matter which variant arrived.
4. Republishes everything to **your own Mosquitto broker** on the original `hookii/details/device/<serial>` topic.
5. Translates inbound MQTT publishes on `hookii/cmd/<serial>/<action>` into the right REST call against `/api/v1/mower/cmd/...`.
6. Publishes MQTT-Discovery configs so the lawn_mower entity, buttons and sensors above appear in Home Assistant automatically.

## Install path A: Home Assistant OS / Supervised (the Add-on Store)

If you run **Home Assistant OS** or **Home Assistant Supervised** (the install methods that include the Supervisor and have an Add-on Store), use this path:

1. In Home Assistant: **Settings → Add-ons → Add-on store**
2. Click the **⋮** menu (top right) → **Repositories**
3. Paste this URL:

   ```
   https://github.com/torvalstrom/hookii-bridge-ha-addon
   ```

4. Click **Add → Close**
5. Reload the Add-on store. **Hookii Bridge** appears under "Neomow X Home Assistant Add-on".
6. Click **Hookii Bridge** → **Install**, then follow the [Hookii Bridge setup guide](hookii_bridge/DOCS.md) — it walks through the Hookii account credentials, your mower serial number(s) and the Mosquitto broker settings the add-on needs. The **Mower Map** appears automatically as a sidebar panel once the add-on starts — no separate install.

To check which install method you have: **Settings → About → Installation method**. If it says "Home Assistant Container" or "Home Assistant Core", the Add-on Store is not available — use install path B instead.

## Install path B: Home Assistant Container / Core / k3s (Docker, no Supervisor)

If you run Home Assistant as a plain Docker container, in Kubernetes, or as the bare Core install, there's no Supervisor and therefore no Add-on Store. You run the bridge as a standalone Docker container next to your Home Assistant container instead. Same image, no Add-on Supervisor wrapper.

**The easiest option is to pull the prebuilt image** — the same multi-arch images published to GHCR for the Add-on Store, which also run standalone (the launcher auto-detects "no Supervisor" and reads its config from the environment variables below). No local build needed:

| Your host architecture | Image to pull |
|---|---|
| x86-64 (most mini-PCs / NUCs / VMs) | `ghcr.io/torvalstrom/amd64-hookii-bridge:latest` |
| 64-bit ARM (Raspberry Pi 4/5 on a 64-bit OS) | `ghcr.io/torvalstrom/aarch64-hookii-bridge:latest` |
| 32-bit ARM (Raspberry Pi 2/3 on a 32-bit OS) | `ghcr.io/torvalstrom/armv7-hookii-bridge:latest` |

Pick the row matching your host (`uname -m`: `x86_64`→amd64, `aarch64`/`arm64`→aarch64, `armv7l`→armv7). Pin to a specific release with `:1.2.7` instead of `:latest`. The examples below show this image; if you'd rather build from source, the repo's root [`Dockerfile`](Dockerfile) still works — replace the `image:` line with the commented-out `build:` line.

### 1. Add the bridge to your existing `docker-compose.yml`

Drop this service into the same compose file you already use for Home Assistant. Home Assistant Container normally runs with `network_mode: host` so it can reach the host's port 1883 directly; the bridge needs the same so it can connect to your local Mosquitto broker without Docker bridge-network DNS getting in the way.

> 🪧 **About the LABEL placeholder below**
> The string `mower` appears in TWO places: as the first colon-separated field in `HOOKII_ACCOUNTS`, AND as the upper-cased suffix `_MOWER` on the `HOOKII_SERIALS_<LABEL>` variable. If you change one you MUST change the other (and the suffix is ALWAYS upper-cased even if your label is lower-case). If they don't match, the bridge starts up but never receives any STATUS pushes because it has no serial number bound to that account.

```yaml
services:
  hookii-bridge:
    image: ghcr.io/torvalstrom/amd64-hookii-bridge:latest   # ← pick your arch (amd64 / aarch64 / armv7)
    # build: https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main   # ← or build from source instead
    container_name: hookii-bridge
    restart: unless-stopped
    network_mode: host                    # ⚠️  Required so the bridge can reach
                                          #     your host's Mosquitto on 127.0.0.1:1883
    environment:
      # ⚠️  Use a DEDICATED Hookii account, NOT your primary one.
      # See the "dedicated bridge account" warning at the top of this README.
      #
      # Format: <label>:<email>:<password>     ← exactly two colons, no spaces.
      # The <label> is a free identifier you choose — it links this account
      # row to its HOOKII_SERIALS_<LABEL> row below.
      - HOOKII_ACCOUNTS=mower:bridge@yourdomain.com:YOUR_CLEAR_PASSWORD

      # ⚠️  Variable name suffix MUST match the label above, upper-cased.
      #     Label "mower"  →  variable HOOKII_SERIALS_MOWER
      #     Label "garden" →  variable HOOKII_SERIALS_GARDEN
      - HOOKII_SERIALS_MOWER=HKX1EB100JD25010115,HKX2EB100JD24080170  # comma-separated

      - LOCAL_MQTT_HOST=127.0.0.1          # use 127.0.0.1 because network_mode: host
      - LOCAL_MQTT_PORT=1883
      - LOCAL_MQTT_USER=hookii
      - LOCAL_MQTT_PASS=<your local broker password>
      - HEARTBEAT_SEC=1.5                  # match the mobile app
      - ENABLE_DISCOVERY=1                 # auto-create HA entities
      - DISCOVERY_PREFIX=homeassistant
      - HOOKII_ENV=beta                    # beta (iot.beta.hookii.com) or prod (iot.hookii.com)
      # - HOOKII_REST_HOST=host:port       # optional: override the REST endpoint hookii_env selects
      # - HOOKII_MQTT_HOST=host:port       # optional: override the cloud MQTT endpoint
      - LOG_LEVEL=INFO
```

> ℹ️ **Beta vs production cloud.** `HOOKII_ENV=beta` (default) targets `iot.beta.hookii.com` and gives the full telemetry set on BETA firmware `1.6.8.4-beta+`; `HOOKII_ENV=prod` targets `iot.hookii.com` for mowers on stable firmware / a production account (core state, battery, command buttons and Discovery, just fewer sensors). `HOOKII_REST_HOST` / `HOOKII_MQTT_HOST` (each a `host:port`) override the endpoints `HOOKII_ENV` selects and are only needed if a port ever differs from the presets. See [DOCS.md](hookii_bridge/DOCS.md) "Beta vs Production cloud".

> ℹ️ Note the `- KEY=VALUE` list form (not `KEY: "value"`). Both are valid compose syntax, but the list form is more robust against YAML parsers that mis-handle colon-containing string values like passwords or `HOOKII_ACCOUNTS` triplets.

Bring it up:

```bash
docker compose up -d hookii-bridge
```

### 2. Or as a pure `docker run`

If you don't use compose:

```bash
docker run -d --name hookii-bridge --restart unless-stopped \
  --network host \
  -e HOOKII_ACCOUNTS="mower:bridge@yourdomain.com:YOUR_CLEAR_PASSWORD" \
  -e HOOKII_SERIALS_MOWER="HKX1EB100JD25010115" \
  -e LOCAL_MQTT_HOST="127.0.0.1" \
  -e LOCAL_MQTT_PORT="1883" \
  -e LOCAL_MQTT_USER="hookii" \
  -e LOCAL_MQTT_PASS="..." \
  -e HEARTBEAT_SEC="1.5" \
  -e ENABLE_DISCOVERY="1" \
  ghcr.io/torvalstrom/amd64-hookii-bridge:latest   # ← pick your arch (amd64 / aarch64 / armv7)
```

Same pairing rule as the compose example: the label in `HOOKII_ACCOUNTS` (here `mower`) determines the `HOOKII_SERIALS_<LABEL>` variable name (here `HOOKII_SERIALS_MOWER`). (Prefer building from source? Replace the last line with `$(docker build -q https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main)`.)

### 3. Verify it's running

```bash
docker logs hookii-bridge --tail 30
```

You should see lines like:

```
INFO hookii-bridge [bridge] login OK, jwt-len=...
INFO hookii-bridge [bridge] cloud-mqtt connected as Android_bridge@yourdomain.com_..., subscribing to N serial(s)
INFO hookii-bridge discovery: published 20 entities for HKX1EB100JD25010115
INFO hookii-bridge [bridge] heartbeat thread starting interval=1s
```

In Home Assistant, the bridge's mower entities (`lawn_mower.*`, the 5 command buttons and the 14 telemetry sensors) appear automatically via MQTT Discovery — as long as your Home Assistant has the **MQTT** integration configured against the same Mosquitto broker the bridge publishes to.

### 4. Environment variable reference (Container path)

| Env var | Required | Default | Description |
|---------|----------|---------|-------------|
| `HOOKII_ACCOUNTS` | **yes** | — | `label:email:password` triplets, separated by `;`. Use the dedicated bridge account, not your primary one. |
| `HOOKII_SERIALS_<LABEL>` | **yes** | — | Comma-separated list of mower serial numbers that account owns / has been shared. One env var per label. |
| `LOCAL_MQTT_HOST` | **yes** | — | Your local Mosquitto / EMQX broker IP or hostname. |
| `LOCAL_MQTT_PORT` | no | `1883` | |
| `LOCAL_MQTT_USER` | yes if broker requires auth | — | |
| `LOCAL_MQTT_PASS` | yes if broker requires auth | — | |
| `HEARTBEAT_SEC` | no | `1.5` | Hookii cloud heartbeat interval. Match the mobile app's `1.5` or your phone will get logged out. |
| `ENABLE_DISCOVERY` | no | `1` | Set `0` if you prefer to define sensors via the legacy YAML block instead. |
| `DISCOVERY_PREFIX` | no | `homeassistant` | Override only if you customised this in HA's MQTT integration. |
| `HOOKII_ENV` | no | `beta` | `beta` → `iot.beta.hookii.com` (full telemetry on BETA firmware `1.6.8.4-beta+`); `prod` → `iot.hookii.com` (core features for stable-firmware mowers / a production account). |
| `HOOKII_REST_HOST` | no | — | `host:port` override for the REST endpoint that `HOOKII_ENV` selects. Only needed if a port ever differs from the presets. |
| `HOOKII_MQTT_HOST` | no | — | `host:port` override for the cloud MQTT endpoint that `HOOKII_ENV` selects. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` for protocol-level traces. |

### 5. Updating later

There's no Add-on Store update banner on this install path — you pull the new image yourself when you want a new version. Check the [CHANGELOG](hookii_bridge/CHANGELOG.md), then:

```bash
# Compose (using the prebuilt image):
docker compose pull hookii-bridge && docker compose up -d hookii-bridge

# Plain docker (using the prebuilt image):
docker pull ghcr.io/torvalstrom/amd64-hookii-bridge:latest   # ← your arch
docker stop hookii-bridge && docker rm hookii-bridge
# ... then re-run the `docker run` from step 2
```

Pin to a specific release by using `:1.2.7` instead of `:latest` (and bump it when you want to move). If you build from source instead, swap the pull for `docker compose build --no-cache --pull hookii-bridge` (compose) or `docker build --no-cache --pull -t hookii-bridge:latest https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main` (plain docker), appending `#v1.2.7` to pin.

### 6. (Optional) The Mower Map on the Container path

**The bridge image already contains the Mower Map.** The simplest way to get it
is to set the `MOWERS` env var on the **bridge** service and publish port `8000`
— `run.sh` then launches the map alongside the bridge in the same container
(a map crash can never take the bridge down):

```yaml
  hookii-bridge:
    # ... your existing bridge config ...
    ports:
      - "8000:8000"                       # Mower Map web UI
    environment:
      # ... existing bridge env ...
      # Format: label:serial[:color];label:serial[:color];...
      - MOWERS=garden:HKX1EB100JD25010115:#22c55e;pond:HKX2EB100JD24080170:#3b82f6
```

The map is then at `http://<host>:8000/` (all-mowers grid),
`http://<host>:8000/page/<label>` (iframe card) or
`http://<host>:8000/embed/<label>` (picture card — bare SVG, no JS).

<details>
<summary>Legacy: running the map as a separate container</summary>

This was the pre-v1.5.0 layout (a standalone `hookii-mower-map` image). It still
works but is no longer necessary — prefer the bundled map above.

```yaml
  hookii-mower-map:
    image: ghcr.io/torvalstrom/amd64-hookii-mower-map:latest   # ← pick your arch (amd64 / aarch64 / armv7)
    # build:                                                    # ← or build from source instead
    #   context: https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main
    #   dockerfile: Dockerfile.map
    container_name: hookii-mower-map
    restart: unless-stopped
    network_mode: host
    depends_on:
      - hookii-bridge
    volumes:
      - ./hookii-map-data:/data          # persisted boundary + last-known fix
    environment:
      # Format: label:serial[:color];label:serial[:color];...
      - MOWERS=garden:HKX1EB100JD25010115:#22c55e;pond:HKX2EB100JD24080170:#3b82f6
      - LOCAL_MQTT_HOST=127.0.0.1
      - LOCAL_MQTT_PORT=1883
      - LOCAL_MQTT_USER=mowermap
      - LOCAL_MQTT_PASS=<your local broker password>
      - LOG_LEVEL=INFO
```

The separate map's HTTP API is then served at `http://<host>:8000/page/<label>`.

</details>

Full Mower Map configuration reference (display options, troubleshooting) is in [`hookii_bridge/DOCS.md`](hookii_bridge/DOCS.md).

## Disclaimer

This add-on is **not affiliated with Hookii**. It is a community workaround built so existing Home Assistant integrations keep working after a vendor-side protocol change. The cloud MQTT broker certificate is self-signed and validated insecurely — same trade-off the official mobile app makes.

Licensed Apache-2.0.
