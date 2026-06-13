# Hookii Bridge

> ℹ️ **Works against BOTH the Hookii BETA cloud and the PRODUCTION cloud - pick with the `hookii_env` option.**
>
> The add-on can talk to either backend:
>
> - `hookii_env: beta` (default) → `iot.beta.hookii.com`, the protocol's reference backend. Mowers on **Hookii BETA firmware `1.6.8.4-beta` or newer** push the richest telemetry here: the fine-grained `robotStatus` state machine, the granular per-system sensors, and firmware-upgrade awareness.
> - `hookii_env: prod` → `iot.hookii.com`, the production cloud. Use this if your mowers run **stable (production) firmware** and you log in with your normal Hookii production account. Stable firmware emits a sparser STATUS, so you still get the core `lawn_mower` state (docked / mowing / returning), battery, the command buttons and Discovery - but NOT the granular sensors or the firmware-upgrade indicator.
>
> Short version: choose `prod` if your mowers are on stable firmware / a production account; choose `beta` for the full feature set (and switch each mower to the Beta channel in the Hookii mobile app first). See the "Beta vs Production cloud" section below for the full tradeoff. The bridge handles both shapes and degrades gracefully - it never fails just because a mower is on stable firmware.

> 🚨 **Use a DEDICATED Hookii account for the bridge - DO NOT reuse your primary account.**
>
> Hookii's server enforces a strict **one-active-session-per-account** policy: every time the bridge OR your mobile app logs in for the same account, all previous JWTs are silently invalidated server-side. The result is a tug-of-war the user always loses - **you (or your spouse / family) will get logged out of the Hookii mobile app every few minutes** as the bridge re-authenticates, and bridge commands intermittently fail.
>
> **The mandatory setup (5 minutes, no code):**
>
> 1. Create a **second Hookii account** with a different email - e.g. `homeassistant@yourdomain.com` or `bridge@…`. This is the account the add-on will use.
> 2. From your **primary** Hookii account in the mobile app, open each mower → Settings → Device Sharing (or "Share Device" / "Add Member" depending on app version) and **share each mower to the new bridge account**.
> 3. Use the **new bridge account's** email + password in the add-on's `hookii_email` and `hookii_password` config fields (NOT your primary account).
> 4. Keep using your **primary** account in the Hookii mobile app as normal. No more silent logouts.
>
> Your primary account stays the device owner; the bridge account is a shared viewer/controller. Both can issue commands to the mowers because Hookii's sharing model grants full control. If you skip this step the add-on still appears to work - until you open the mobile app, and then you and the bridge will keep evicting each other's sessions forever.

This add-on logs in to Hookii's cloud with your account, keeps the new (May 2026) JWT-gated heartbeat protocol alive, republishes your mower's STATUS to your **own Mosquitto broker** on the legacy `hookii/details/device/<serial>` topic, AND (since v1.1.0) exposes a REST command channel for control operations plus auto-discovered Home Assistant entities so you don't need to write any YAML.

After install you get, per mower, in HA:

- One `lawn_mower.<your_mower>` entity with `start_mowing`, `pause` and `dock` services wired up.
- Five buttons: **Start**, **Pause**, **Return to dock**, **Stop (keep progress)**, **Stop (clear progress)**.
- Telemetry sensors: battery, blade RPM, voltage, charge current, four temperatures (battery / blade / left & right drive), WiFi signal, GPS satellites, latitude, longitude, work status and a friendly "State" sensor that reads "mowing" / "returning" / "docked".
- Raw `hookii/details/device/<serial>` topic still publishes the full STATUS payload, so any existing template sensors or n8n flows keep working unchanged.

## What you need before installing

You need to have these set up in Home Assistant already:

1. **A Hookii cloud to connect to - beta or production.** The add-on works with both (set `hookii_env`; see the banner and the "Beta vs Production cloud" section). For the **full** feature set (granular sensors + firmware-upgrade awareness) your mower needs Hookii BETA firmware `1.6.8.4-beta` or newer and `hookii_env: beta`; verify the `…-beta` suffix in the Hookii mobile app under each mower → Settings → Firmware. On **stable** firmware, leave `hookii_env: prod` and you still get state, battery, the command buttons and Discovery - just fewer sensors.
2. **A DEDICATED Hookii account for the bridge** (see the second banner above - this is the most-skipped step and the one that causes the "my mobile app keeps logging out" complaint). Create a second Hookii account with a different email, then from your primary account share every mower to the bridge account via the mobile app's Device Sharing menu. The add-on will use the bridge account's credentials below; your primary account stays signed in on your phone.
3. **Mosquitto broker add-on** (Settings → Add-ons → Add-on Store → "Mosquitto broker" → Install + Start). The community version published by Home Assistant is fine.
4. **MQTT integration** (Settings → Devices & Services → Add Integration → MQTT). Point it at your Mosquitto broker.
5. A **dedicated MQTT user** for the bridge. In Home Assistant: Settings → Users → Add user → give it a username like `hookii` and a strong password. You don't need to change anything in the Mosquitto broker add-on - by default it accepts any Home Assistant user's username + password for MQTT login, so the user you just created is immediately usable. (You'll paste that username and password into the Hookii Bridge add-on's `local_mqtt_user` and `local_mqtt_pass` fields below.)
6. **The bridge Hookii account's credentials** (the email + password you just created for the dedicated bridge account - NOT your primary phone-app account). Read the "How to enter your password" section below before you paste it.
7. **Your mower's serial number(s).** You can read these in the Hookii app under each mower → Device info, or off the sticker on the underside of the mower. They look like `HKX1EB100JD25010115`.

## How to enter your password

The Hookii cloud expects your password as an **uppercase MD5 hash**, not as plain text. The bridge handles this for you in two ways:

**Option A — Just paste your cleartext password** into the `hookii_password` field. The bridge MD5-hashes and uppercases it automatically before sending. The hash never leaves the add-on container; it never gets logged.

**Option B — Hash it yourself first** and paste only the uppercase 32-character hex digest. The bridge auto-detects that the value is already hashed and forwards it as-is. Use this if you'd rather not store your cleartext password in your Home Assistant configuration.

To hash your password yourself, use any MD5 tool, e.g. on Linux/macOS:

```
printf 'your_password_here' | md5sum | awk '{print toupper($1)}'
```

…or paste into a trusted browser-side MD5 tool like <https://emn178.github.io/online-tools/md5.html> and uppercase the result.

The two options produce the same login result; pick whichever you're more comfortable with.

## Beta vs Production cloud

The bridge can run against either of Hookii's two clouds, selected with the `hookii_env` option (add-on) or the `HOOKII_ENV` env var (Container / k3s / docker):

| `hookii_env` | Server | Use it when | What you get |
|---|---|---|---|
| `beta` (default) | `iot.beta.hookii.com` (REST :10443, MQTT :8883) | Your mowers are on **BETA firmware `1.6.8.4-beta` or newer** and you log in with a beta account. | The **full** feature set: the fine-grained `robotStatus` state machine, all granular sensors, and firmware-upgrade awareness (the firmware-upgrading binary_sensor + auto-disable-during-OTA behaviour). |
| `prod` | `iot.hookii.com` (REST :10443, MQTT :8883) | Your mowers are on **stable (production) firmware** and you use your normal production Hookii account. | The **core** feature set: the `lawn_mower` state (docked / mowing / returning, derived from `workingMode`), battery, the command buttons and Discovery. |

**The honest tradeoff:** the rich STATUS fields (`robotStatus`, the fine-grained state machine, firmware-upgrade detection) are only emitted by BETA firmware `1.6.8.4-beta+`. Mowers on stable firmware send a **sparser** STATUS ("Shape A" - `workingMode` only, often no `robotStatus`). The bridge handles both shapes and degrades gracefully: on prod / stable firmware you keep the core lawn_mower state, battery, the command buttons and Discovery, but you do **not** get the granular sensors or the firmware-upgrade indicator. Nothing fails - you simply get fewer entities.

So: pick `prod` if your mowers run stable firmware and you want to use your production Hookii account as-is; pick `beta` (and switch each mower to the Beta channel in the Hookii mobile app) for the complete sensor set.

> ℹ️ **Cloud MQTT broker credential.** The telemetry channel authenticates with a shared static credential baked into the Hookii app. The username (`hookii-iot`) is the same in both environments, but the **password differs between the beta and prod brokers**. The bridge ships **both** and selects the right one automatically from `hookii_env`, so beta and prod each work out of the box - you do not need to set anything. (Per-user access is still enforced by the JWT in the heartbeat, not at MQTT auth, so the shared password is safe to ship.) The `hookii_mqtt_user` / `hookii_mqtt_pass` options exist only as a manual override in case Hookii rotates the credential before a new add-on release catches up.

Two advanced options, `hookii_rest_host` and `hookii_mqtt_host` (each a `host:port`), let you override the endpoints that `hookii_env` selects. They are blank by default and only needed if a port ever differs from the presets - most users should leave them empty. For Container / k3s users the same overrides are the `HOOKII_REST_HOST` / `HOOKII_MQTT_HOST` env vars.

## Install

1. Add this repository to Home Assistant:
   - Settings → Add-ons → Add-on Store → **⋮** → Repositories → paste

     ```
     https://github.com/torvalstrom/hookii-bridge-ha-addon
     ```

   - Click Add → Close → reload.
2. Find **Hookii Bridge** under "Conscient Systems Add-ons" → click → **Install**.
3. Switch to the **Configuration** tab and fill in:

   | Field | What to put |
   |---|---|
   | `hookii_email` | **The bridge account's** Hookii email (the dedicated second account you created for the add-on - NOT your primary phone-app account; see the dedicated-account banner above). Capital letters in the address are fine - the add-on auto-lowercases before sending to Hookii (their beta server is case-sensitive on user lookup, but you don't have to think about that). |
   | `hookii_password` | Either your cleartext password, or its uppercase MD5 hash (see above). |
   | `mower_serials` | Your mower serial number(s). Multiple are comma-separated, e.g. `HKX1EB100JD25010115,HKX2EB100JD24080170`. |
   | `local_mqtt_host` | Leave as `core-mosquitto` if you use the official broker add-on. |
   | `local_mqtt_port` | `1883` |
   | `local_mqtt_user` | The dedicated MQTT user you created above. |
   | `local_mqtt_pass` | That user's password. |
   | `heartbeat_seconds` | `15` (default; only change if you know why). |
   | `log_level` | `INFO` (use `DEBUG` only when troubleshooting). |
   | `hookii_agent` | Client fingerprint string sent on every REST request to Hookii. The default is a plausible Android/Xiaomi value. Override only if you want the add-on to identify as a different device — most users should leave this. |
   | `enable_discovery` | `true` (default) to auto-create the `lawn_mower` entity, 5 buttons and telemetry sensors via MQTT Discovery. Set `false` if you want to manage everything via your own YAML. |
   | `discovery_prefix` | `homeassistant` (default; matches the HA convention). Change only if you've reconfigured Home Assistant's MQTT integration to use a different prefix. |
   | `hookii_env` | `beta` (default) or `prod`. `beta` connects to `iot.beta.hookii.com` (REST :10443, MQTT :8883), the reference backend with the full telemetry set on BETA firmware. `prod` connects to `iot.hookii.com` (same ports) for mowers on stable firmware / a production Hookii account. See "Beta vs Production cloud" below. |
   | `hookii_rest_host` | Advanced; blank by default. Set to `host:port` to override the REST endpoint that `hookii_env` selects. Only needed if a port ever differs from the presets - leave blank otherwise. |
   | `hookii_mqtt_host` | Advanced; blank by default. Set to `host:port` to override the cloud MQTT endpoint that `hookii_env` selects. Leave blank otherwise. |
   | `hookii_mqtt_user` | Advanced; **leave blank.** The cloud MQTT broker username is `hookii-iot` in both environments and the bridge ships it. Only set this if Hookii ever changes the username. |
   | `hookii_mqtt_pass` | Advanced; **leave blank.** The bridge ships the correct shared broker password for each environment (selected automatically by `hookii_env`). Only set this to override if Hookii rotates the password before a new add-on release ships the new value. |

4. Save → switch to the **Info** tab → click **Start**.
5. Open the **Log** tab and confirm you see lines like:

   ```
   INFO hookii-bridge [addon] login OK, jwt-len=…
   INFO hookii-bridge [addon] cloud-mqtt connected as Android_<your-email>_…, subscribing to N serial(s)
   INFO hookii-bridge [addon] SUB hk/server/mower/push/+/HKX…
   INFO hookii-bridge [addon] heartbeat thread starting interval=15s
   ```

   If you see `REST login failed` with `code: 5, msg: 该用户未注册`, your email is not registered on `iot.beta.hookii.com`. Try the credentials in the Hookii mobile app first. If you see `code: 2, msg: hookii-agent参数错误`, that's a Hookii server change and we'll need to update the add-on.

## Updating to a newer version

If you already have Hookii Bridge installed, new releases show up automatically in the Home Assistant Add-on Store. The full flow:

1. Go to **Settings → Add-ons**. When an update is available, a small orange dot appears next to **Hookii Bridge**.
2. Click **Hookii Bridge** to open the add-on page.
3. At the top there's a banner reading *"There is an update available for this add-on"* with the new version number, and an **Update** button. Click it.
4. The Supervisor downloads + rebuilds the new image. Takes 1-2 minutes.
5. The add-on auto-restarts when the update is done. You're on the new version.

**If you don't see the update banner** but you know there's a newer version on GitHub:

- In the Add-on Store, click the **⋮** menu (top right) → **Reload** (sometimes called **Check for updates**). HA refreshes the marketplace metadata for every installed repository.
- Reload the add-on page; the update banner should now appear.

**To check which version you're on right now**: open the add-on page and scroll down - the currently-installed version is shown near the bottom.

**To read what changed between versions**: the [CHANGELOG](CHANGELOG.md) lists every release with its highlights, breaking changes and rationale.

## The built-in Mower Map

Since **v1.5.0** the live Mower Map is built into this add-on — there is **no
separate add-on to install** and **nothing extra to configure**. It draws a live
SVG view of each mower's yard: the boundary polygon (once the cloud streams it),
the cut paths (thick green), transit paths (thin light green), the live trail in
your colour, and the mower itself with a heading arrow. It refreshes every 10s.

**How to open it:** when the add-on starts, Home Assistant adds a **Mower Map**
entry to the left sidebar (the map is served over Home Assistant Ingress). Click
it — you get a grid with one tile per mower. The map builds its mower list
automatically from your `mower_serials` and reuses this add-on's MQTT settings,
so it just works.

**Mower URL slugs.** Each mower's slug (its "label") is its **serial in
lower-case** — e.g. serial `HKX1EB100JD25010115` → slug `hkx1eb100jd25010115`.
You only need this if you embed the map on a dashboard:

```yaml
type: iframe
url: /hassio/ingress/hookii_bridge/page/hkx1eb100jd25010115   # one mower
aspect_ratio: 100%
```

Use `/hassio/ingress/hookii_bridge/all` for the all-mowers grid (same as the
sidebar panel). On the Container/k3s path (no Supervisor), the map is served
directly at `http://<host>:8000/` when you set the `MOWERS` env var and publish
port 8000 — see the repo README's Container path.

**Display options** (both optional, in the add-on Configuration):

| Option | Default | What it does |
|---|---|---|
| `map_trail_max` | `2000` | Maximum number of live-position points kept in the trail before the oldest are dropped. Higher = longer visible trail, slightly more memory. |
| `map_rotate_deg` | `0` | Rotate the whole map by this many degrees (`-360`..`360`) so your yard's orientation matches how you think of it. |

**If the map is blank or shows text instead of a picture:** make sure you are on
**v1.5.0-beta2 or newer** (beta1 had a bug where the panel showed raw JSON and
the tiles couldn't load). The map also starts blank until the bridge republishes
the first `STATUS` payload (usually seconds); boundary polygons can take minutes
to hours after the mower first comes online, while the live position and trail
appear right away.

## Sending commands

The add-on subscribes to `hookii/cmd/<serial>/<action>` and translates each publish into a REST call against `iot.beta.hookii.com`. The five buttons published via Discovery already wire up the common actions; if you'd rather call them from automations or scripts, the topics are:

| Topic | Payload | What it does |
|---|---|---|
| `hookii/cmd/<serial>/start` | `{}` or `{"regionList":[0,1]}` | Two-step start (pre-check + execute). Default policy is "resume from breakpoints" if any exist. Pass `regionList` to mow only specific areas. |
| `hookii/cmd/<serial>/pause` | `{}` | Pause the current task. |
| `hookii/cmd/<serial>/return` | `{}` | Return to dock and recharge. Also aliased as `dock` and `recharge`. |
| `hookii/cmd/<serial>/stop_keep` | `{}` | Cancel the current task but keep breakpoint progress so a subsequent Start resumes where it left off. |
| `hookii/cmd/<serial>/stop_clear` | `{}` | Cancel and discard all progress. Use this if you want the next Start to be a fresh full-coverage pass. |
| `hookii/cmd/<serial>/schedule_read` | `{}` | Read the current schedule; result is published to `hookii/result/<serial>/schedule` (retained). |
| `hookii/cmd/<serial>/schedule_write` | `{"taskList":[ ... ],"timeZoneOffset":null}` | Replace the schedule. See "Schedule write" section below for the task shape **and a critical safety note**. |
| `hookii/cmd/<serial>/params_read` | `{}` | Read the per-area mowing parameters (height, speed, mode, etc); result on `hookii/result/<serial>/params`. |

Errors (e.g. a schedule-write safety guard rejection) are published to `hookii/result/<serial>/error` as `{"action": "...", "error": "..."}`. Wire this into a persistent-notification automation if you want to surface failures.

### Schedule write — important safety note

The Hookii cloud treats an enabled schedule whose start/end window contains the current local time as an **implicit start command**. Writing such a schedule causes the mower to immediately start mowing, even if it was returning to dock or charging. The add-on rejects writes that would trigger this and emits an `error` payload — but you should still avoid writing schedules close to the current time from automations.

Task shape:

```json
{
  "taskId": 1,
  "enable": true,
  "startTime": 180,
  "endTime": 1440,
  "weekList": [0, 1, 2, 3, 4, 5, 6],
  "areaIndexList": [0, 1, 2]
}
```

- `startTime` / `endTime` are minutes since midnight (`180 = 03:00`, `1440 = 24:00`).
- `weekList` is 0-indexed; full coverage = `[0,1,2,3,4,5,6]`. The exact weekday-vs-Sunday convention isn't fully pinned down in the protocol reference; if you see off-by-one behaviour, try shifting.
- `areaIndexList` selects which mowing zones the schedule applies to (0-indexed as defined in the Hookii app).

## Optional: paste-by-hand sensor YAML (legacy compatibility)

> Since v1.1.0 you do **not** need to do this. The Discovery feature above already creates the sensors. This block is kept for users on v1.0.x or anyone who has set `enable_discovery: false`.

The bridge publishes raw mower STATUS to `hookii/details/device/<your-serial>`. Add this YAML to your `configuration.yaml` (or under `mqtt:` if you already have a section), replacing **every occurrence of `HKX1EB100JD25010115` with YOUR serial**. If you have more than one mower, duplicate the whole block once per mower and rename `neomow1_*` → `neomow2_*` etc.

```yaml
mqtt:
  sensor:
    # ------ neomow1 ------
    - name: "neomow1_raw"
      unique_id: "neomow1_raw"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" %}
          STATUS
        {% else %}
          {{ states('sensor.neomow1_raw') }}
        {% endif %}
      json_attributes_topic: "hookii/details/device/HKX1EB100JD25010115"
      json_attributes_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" %}
          {{ value_json | tojson }}
        {% else %}
          {{ state_attr('sensor.neomow1_raw', 'data') | tojson }}
        {% endif %}

    - name: "neomow1_battery"
      unique_id: "neomow1_battery"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      unit_of_measurement: "%"
      device_class: battery
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.electricity is defined %}
          {{ value_json.data.STATUS.electricity }}
        {% else %}
          {{ states('sensor.neomow1_battery') | int(0) }}
        {% endif %}

    - name: "neomow1_blade_rpm"
      unique_id: "neomow1_blade_rpm"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      unit_of_measurement: "rpm"
      icon: mdi:saw-blade
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.knifeDiscMotorSpeed is defined %}
          {{ value_json.data.STATUS.knifeDiscMotorSpeed }}
        {% else %}
          {{ states('sensor.neomow1_blade_rpm') | int(0) }}
        {% endif %}

    - name: "neomow1_temp_battery"
      unique_id: "neomow1_temp_battery"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      unit_of_measurement: "°C"
      device_class: temperature
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.batteryTemp is defined %}
          {{ value_json.data.STATUS.batteryTemp | round(0) }}
        {% else %}
          {{ states('sensor.neomow1_temp_battery') | int(0) }}
        {% endif %}

    - name: "neomow1_temp_blade_motor"
      unique_id: "neomow1_temp_blade_motor"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      unit_of_measurement: "°C"
      device_class: temperature
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.knifeDiscMotorTemp is defined %}
          {{ value_json.data.STATUS.knifeDiscMotorTemp | round(0) }}
        {% else %}
          {{ states('sensor.neomow1_temp_blade_motor') | int(0) }}
        {% endif %}

    - name: "neomow1_voltage"
      unique_id: "neomow1_voltage"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      unit_of_measurement: "V"
      device_class: voltage
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.voltage is defined %}
          {{ value_json.data.STATUS.voltage }}
        {% else %}
          {{ states('sensor.neomow1_voltage') | float(0) }}
        {% endif %}

    - name: "neomow1_charge_current"
      unique_id: "neomow1_charge_current"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      unit_of_measurement: "A"
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.chargeCurrent is defined %}
          {{ value_json.data.STATUS.chargeCurrent }}
        {% else %}
          {{ states('sensor.neomow1_charge_current') | float(0) }}
        {% endif %}

    - name: "neomow1_work_status"
      unique_id: "neomow1_work_status"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      value_template: >-
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.workStatus is defined %}
          {% set s   = value_json.data.STATUS %}
          {% set ws  = s.workStatus | int %}
          {% set c   = s.chargeCurrent | float(0) %}
          {% set rpm = s.knifeDiscMotorSpeed | int(0) %}
          {% if   ws == 0 %} Idle
          {% elif ws in [1, 5] %}
            {% if   c > 0 %} Charging
            {% elif c < 0 %} Mowing
            {% else %}        Docked
            {% endif %}
          {% elif ws in [2, 4] %}
            {% if rpm > 0 %}  Mowing
            {% else %}        Travelling
            {% endif %}
          {% elif ws == 3 %} Returning
          {% else %}          Unknown
          {% endif %}
        {% else %}
          {{ states('sensor.neomow1_work_status') }}
        {% endif %}

    - name: "neomow1_latitude"
      unique_id: "neomow1_latitude"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.latitude is defined %}
          {{ value_json.data.STATUS.latitude }}
        {% else %}
          {{ states('sensor.neomow1_latitude') }}
        {% endif %}

    - name: "neomow1_longitude"
      unique_id: "neomow1_longitude"
      state_topic: "hookii/details/device/HKX1EB100JD25010115"
      value_template: >
        {% if value_json is mapping and value_json.msgType == "STATUS" and value_json.data.STATUS.longitude is defined %}
          {{ value_json.data.STATUS.longitude }}
        {% else %}
          {{ states('sensor.neomow1_longitude') }}
        {% endif %}
```

After pasting, **Developer tools → YAML → Check configuration** (must be green) then **Restart** Home Assistant. Within ~30 s of the bridge being up the new sensors populate.

## What sensors you get

After the YAML above, each mower exposes:

- `sensor.neomow1_battery` — battery percentage (0–100).
- `sensor.neomow1_blade_rpm` — knife disc speed; non-zero while mowing.
- `sensor.neomow1_temp_battery` — battery temperature (°C).
- `sensor.neomow1_temp_blade_motor` — blade motor temperature.
- `sensor.neomow1_voltage` — pack voltage.
- `sensor.neomow1_charge_current` — positive = charging, negative = discharging.
- `sensor.neomow1_work_status` — friendly state: Idle / Mowing / Travelling / Charging / Docked / Returning / Unknown.
- `sensor.neomow1_latitude` + `sensor.neomow1_longitude` — live GPS, suitable for a `device_tracker` template.
- `sensor.neomow1_raw` — full raw payload as `attributes.data` for ad-hoc Jinja2 in your own templates.

If you want more (chassis attitude, lift sensors, individual drive motor stats, network signal strength, etc.) inspect `state_attr('sensor.neomow1_raw', 'data')` in **Developer tools → States**. Everything Hookii sends is in there.

## Troubleshooting

- **My family / I keep getting logged out of the Hookii mobile app every few minutes.** This is the most-reported issue and it has ONE cause: the add-on is using the same Hookii account as the mobile app. Hookii's server permits exactly one active session per account; whichever client logged in most recently wins and the other is silently kicked. **Fix:** create a separate Hookii account for the bridge and share your mowers to it from your primary account (see the dedicated-account banner at the top of this page). Five minutes of setup, permanent fix.
- **REST login fails with `code: 5, msg: 该用户未注册` ("user not registered").** Since v1.0.4 the add-on auto-lowercases your email before sending it to Hookii (their beta server is case-sensitive), so this should not happen from a case mismatch alone. If you still see it, double-check the email matches the one you log in to the official Hookii app with, and that the account exists on the cloud you selected with `hookii_env` (a beta account is registered on `iot.beta.hookii.com`; a production account on `iot.hookii.com` - they are separate user directories).
- **REST login OK, MQTT connected, but no `RX hk/server/mower/push/...` lines ever appear.** Most common cause on `hookii_env: beta`: at least one mower is still on stable firmware, so it isn't present on `iot.beta.hookii.com`. Either switch that mower to the Beta channel (confirm the `…-beta` suffix in the Hookii app), OR set `hookii_env: prod` to talk to the production cloud `iot.hookii.com` instead. Also double-check you're pointed at the cloud that actually hosts your account: a beta account won't appear on prod and vice versa.
- **`hookii_env: prod` shows a sparser status with no granular sensors / no firmware-upgrade sensor.** This is **expected, not a bug.** Stable (production) firmware emits the "Shape A" STATUS (`workingMode` only, often no `robotStatus`), so the bridge can derive the core lawn_mower state, battery, the command buttons and Discovery - but not the granular per-system sensors or the firmware-upgrade indicator (those need BETA firmware `1.6.8.4-beta+` and `hookii_env: beta`). Everything else keeps working normally.
- **`hookii_env: prod`: REST login OK but `cloud-mqtt connect failed rc=Bad user name or password`.** Since v1.3.1 the bridge ships the correct prod broker password and selects it automatically from `hookii_env: prod`, so this should no longer happen on a current release. If you still see it: (a) confirm you're on add-on **v1.3.1 or newer**; (b) make sure you haven't set a stale `hookii_mqtt_pass` override (clear it so the built-in per-environment password is used); (c) if Hookii has rotated the shared broker password since this release, set the new value in `hookii_mqtt_pass` as a stopgap and open an issue so a new release can ship it. REST login and commands are unaffected regardless - only the live telemetry stream needs the broker login.
- **No sensors update at all.** Double-check the bridge logs show `cloud-mqtt connected` AND a `RX hk/server/mower/push/...` line within ~30 s. If the second is missing, the heartbeat isn't being accepted — verify your account works in the official Hookii app first.
- **REST login OK but `mowing_zero` / serial mismatch.** Open the bridge log, look for `learned model=… for serial=…`. If your serial never shows up there, you typed the wrong one in `mower_serials`. Check it against the Hookii app.
- **Sensor values are stuck.** Try setting `log_level: DEBUG`, restart the add-on, watch the logs and reload your YAML.
- **Multi-mower setup.** Each entry in `mower_serials` is independent. The bridge subscribes per-serial.

## Privacy and safety

- Your Hookii credentials never leave the add-on container. They're sent once to Hookii's REST API to obtain a JWT, then that JWT is reused for ~6 h.
- The cloud broker uses a self-signed TLS certificate (same as Hookii's mobile app). The bridge validates the *connection* but does **not** verify the certificate chain. This is a knowing trade-off; it's the only way to talk to their broker without official PKI.
- All other traffic stays on your LAN: the bridge publishes only to the local Mosquitto broker.

## Support

Issues, feature requests and "it worked / it didn't work on my model" reports: <https://github.com/torvalstrom/hookii-bridge-ha-addon/issues>
