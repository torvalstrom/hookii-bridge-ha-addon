# Neomow X Home Assistant Add-on

> ⚠️ **Requires Hookii BETA firmware `1.6.8.4-beta` or newer on every mower this bridge talks to.**
> The new cloud protocol this add-on speaks (`iot.beta.hookii.com`) is only live on the Hookii BETA channel. **Mowers on the stable firmware channel will not work.** Switch each mower to the Beta channel in the Hookii mobile app before installing.

> 🚨 **Use a DEDICATED Hookii account for the bridge, not your primary account.**
> Hookii's server only keeps ONE active session per account. If the bridge and your phone app share the same account they will silently evict each other's sessions every few minutes, and you will be permanently logged out of the mobile app.
> **Fix (5 minutes, no code):** create a second Hookii account, then from your primary account share each mower to it via the mobile app's Device Sharing menu. Configure the add-on with the new bridge account's credentials. See [DOCS.md](hookii_bridge/DOCS.md) for the full walk-through.

This repository contains **two** Home Assistant add-ons:

- **Hookii Bridge** ([`hookii_bridge/`](hookii_bridge/)) — a reverse-engineered Home Assistant integration for **Hookii Neomow** robot mowers. Two-way: it reads live telemetry AND sends control commands. Replaces the community workaround that broke when Hookii migrated their cloud in May 2026. The bridge is the workhorse you almost certainly want first.

- **Hookii Mower Map** ([`hookii_mower_map/`](hookii_mower_map/)) — an optional companion visualizer that subscribes to the bridge's MQTT output and renders a live SVG yard view per mower (boundary polygon, cut/transit paths, live trail, robot position + heading). Designed to drop straight into a Lovelace iframe card. See [`hookii_mower_map/DOCS.md`](hookii_mower_map/DOCS.md) for setup.

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

## The Mower Map — get started in 3 steps

The Mower Map is an optional companion add-on that renders a live SVG view of each mower's yard: the boundary polygon (when the cloud has streamed it), every cut path the mower has driven (thick green), every transit path it took without cutting (thin light green), the live trail in your chosen colour, and the mower itself with a heading arrow. Drop it into a Lovelace iframe card and you have a moving map of your yard updating every 10 seconds.

### Quick start

1. **Install the Hookii Bridge add-on first.** The Mower Map is a pure consumer of the bridge's MQTT output — it does not talk to Hookii's cloud directly. Without the bridge running, the map sits at "Waiting for data..." forever.

2. **Install the Hookii Mower Map add-on** (same install path as the bridge, see below). Set the `mowers` option to a semicolon-separated list of `label:serial[:color]` entries — one per mower you want to see:

   ```yaml
   mowers: "garden:HKX1EB100JD25010115:#22c55e;pond:HKX2EB100JD24080170:#3b82f6"
   ```

   Re-use the same broker credentials you configured for the bridge. The label is the URL slug you'll reference in step 3.

3. **Drop the map into a Lovelace iframe card:**

   ```yaml
   type: iframe
   url: /hassio/ingress/hookii_mower_map/page/garden
   aspect_ratio: 100%
   ```

   Replace `garden` with the label you set in step 2. For a side-by-side grid of every configured mower in a single card, point the URL at `/all` instead.

That's all that's required to get a live view. The map will start at "Waiting for data..." and switch to a rendered yard as soon as the bridge republishes the first `STATUS` payload (usually within seconds). Boundary polygons appear when the cloud first streams `DEVICE_MAP_V2`, which can take minutes to hours after the mower comes online; live position and trail render immediately.

Full configuration reference, env-var-driven setup for Container/k3s users, and troubleshooting are in [`hookii_mower_map/DOCS.md`](hookii_mower_map/DOCS.md).

## How it works under the hood

In May 2026 Hookii migrated their cloud from a passive-subscribe MQTT bus to a JWT-gated heartbeat protocol on `iot.beta.hookii.com`. The old "just subscribe to `hookii/details/device/<serial>`" trick stopped working overnight, and the official Hookii app became the only client that could see the new protocol.

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
5. Reload the Add-on store. **Hookii Bridge** and **Hookii Mower Map** both appear under "Neomow X Home Assistant Add-on".
6. Click **Hookii Bridge** → **Install**, then follow the [Hookii Bridge setup guide](hookii_bridge/DOCS.md) — it walks through the Hookii account credentials, your mower serial number(s) and the Mosquitto broker settings the add-on needs.
7. (Optional) Once the bridge is publishing, install **Hookii Mower Map** the same way and follow the [Mower Map setup guide](hookii_mower_map/DOCS.md). It re-uses the same broker config and just needs your mower serials.

To check which install method you have: **Settings → About → Installation method**. If it says "Home Assistant Container" or "Home Assistant Core", the Add-on Store is not available — use install path B instead.

## Install path B: Home Assistant Container / Core / k3s (Docker, no Supervisor)

If you run Home Assistant as a plain Docker container, in Kubernetes, or as the bare Core install, there's no Supervisor and therefore no Add-on Store. You run the bridge as a standalone Docker container next to your Home Assistant container instead. Same image, no Add-on Supervisor wrapper.

The repo's root [`Dockerfile`](Dockerfile) is built for exactly this case: a plain `python:3.12-slim` image with the bridge as its entrypoint, configured entirely through environment variables instead of `options.json`.

### 1. Add the bridge to your existing `docker-compose.yml`

Drop this service into the same compose file you already use for Home Assistant. Home Assistant Container normally runs with `network_mode: host` so it can reach the host's port 1883 directly; the bridge needs the same so it can connect to your local Mosquitto broker without Docker bridge-network DNS getting in the way.

> 🪧 **About the LABEL placeholder below**
> The string `mower` appears in TWO places: as the first colon-separated field in `HOOKII_ACCOUNTS`, AND as the upper-cased suffix `_MOWER` on the `HOOKII_SERIALS_<LABEL>` variable. If you change one you MUST change the other (and the suffix is ALWAYS upper-cased even if your label is lower-case). If they don't match, the bridge starts up but never receives any STATUS pushes because it has no serial number bound to that account.

```yaml
services:
  hookii-bridge:
    build: https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main
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
      - LOG_LEVEL=INFO
```

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
  $(docker build -q https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main)
```

Same pairing rule as the compose example: the label in `HOOKII_ACCOUNTS` (here `mower`) determines the `HOOKII_SERIALS_<LABEL>` variable name (here `HOOKII_SERIALS_MOWER`).

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
| `LOG_LEVEL` | no | `INFO` | `DEBUG` for protocol-level traces. |

### 5. Updating later

There's no Add-on Store update banner on this install path — you rebuild the image yourself when you want a new version. Check the [CHANGELOG](hookii_bridge/CHANGELOG.md), then:

```bash
# Compose:
docker compose build --no-cache --pull hookii-bridge && \
  docker compose up -d hookii-bridge

# Plain docker:
docker build --no-cache --pull -t hookii-bridge:latest \
  https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main
docker stop hookii-bridge && docker rm hookii-bridge
# ... then re-run the `docker run` from step 2 with `-d hookii-bridge:latest` as the image
```

Pin to a specific release by appending `#v1.1.6` instead of `#main` to the build URL.

### 6. (Optional) Adding the Mower Map alongside the bridge

If you want the live SVG yard view too, add a second service to the same compose file:

```yaml
  hookii-mower-map:
    build:
      context: https://github.com/torvalstrom/hookii-bridge-ha-addon.git#main
      dockerfile: Dockerfile.map
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

The map's HTTP API is then served at `http://<host>:8000/page/<label>` — drop that URL into a Lovelace iframe card. Full configuration reference and troubleshooting in [`hookii_mower_map/DOCS.md`](hookii_mower_map/DOCS.md).

## Disclaimer

This add-on is **not affiliated with Hookii**. It is a community workaround built so existing Home Assistant integrations keep working after a vendor-side protocol change. The cloud MQTT broker certificate is self-signed and validated insecurely — same trade-off the official mobile app makes.

Licensed Apache-2.0.
