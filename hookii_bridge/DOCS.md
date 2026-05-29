# Hookii Bridge

> ⚠️ **Requires Hookii BETA firmware `1.6.8.4-beta` or newer on every mower this add-on talks to.**
>
> The new cloud protocol this add-on speaks lives on `iot.beta.hookii.com`, which is **only live on the Hookii BETA firmware channel**. Mowers on the stable channel won't push STATUS through this bridge at all - the REST login will succeed but no STATUS topics will ever arrive.
>
> **Before you install:** open the Hookii mobile app → your mower → settings → firmware → switch to the Beta channel, and let it update to `1.6.8.4-beta` or newer. Do this for every mower you want bridged. Joining the Beta channel is a Hookii setting, not a Home Assistant one; this add-on cannot do it for you.
>
> If you're not comfortable running BETA firmware on your mower, this add-on is **not** for you yet - wait until Hookii promotes the new protocol to the stable channel.

This add-on logs in to Hookii's cloud with your account, keeps the new (May 2026) JWT-gated heartbeat protocol alive, and republishes your mower's STATUS to your **own Mosquitto broker** on the legacy `hookii/details/device/<serial>` topic that the original community integrations used. Any existing Home Assistant template sensors, automations or dashboards that read from that topic keep working unchanged.

## What you need before installing

You need to have these set up in Home Assistant already:

1. **Your mower is on Hookii BETA firmware `1.6.8.4-beta` or newer** (see banner above). Verify in the Hookii mobile app under each mower → Settings → Firmware. If you don't see a `…-beta` suffix on the version, you're still on stable and the add-on cannot work.
2. **Mosquitto broker add-on** (Settings → Add-ons → Add-on Store → "Mosquitto broker" → Install + Start). The community version published by Home Assistant is fine.
3. **MQTT integration** (Settings → Devices & Services → Add Integration → MQTT). Point it at your Mosquitto broker.
4. A **dedicated MQTT user** for the bridge. In Home Assistant: Settings → People → Users → Add user → give it a username like `hookii` and a strong password. Then in the Mosquitto broker add-on → Configuration → make sure users are auto-loaded from HA (default).
5. **Your Hookii account credentials** (the email + password you log in to the Hookii mobile app with). Read the "How to enter your password" section below before you paste it.
6. **Your mower's serial number(s).** You can read these in the Hookii app under each mower → Device info, or off the sticker on the underside of the mower. They look like `HKX1EB100JD25010115`.

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

## Install

1. Add this repository to Home Assistant:
   - Settings → Add-ons → Add-on Store → **⋮** → Repositories → paste

     ```
     https://github.com/conscientsystems/hookii-bridge-ha-addon
     ```

   - Click Add → Close → reload.
2. Find **Hookii Bridge** under "Conscient Systems Add-ons" → click → **Install**.
3. Switch to the **Configuration** tab and fill in:

   | Field | What to put |
   |---|---|
   | `hookii_email` | Your Hookii account email. Capital letters in the address are fine - the add-on auto-lowercases before sending to Hookii (their beta server is case-sensitive on user lookup, but you don't have to think about that). |
   | `hookii_password` | Either your cleartext password, or its uppercase MD5 hash (see above). |
   | `mower_serials` | Your mower serial number(s). Multiple are comma-separated, e.g. `HKX1EB100JD25010115,HKX2EB100JD24080170`. |
   | `local_mqtt_host` | Leave as `core-mosquitto` if you use the official broker add-on. |
   | `local_mqtt_port` | `1883` |
   | `local_mqtt_user` | The dedicated MQTT user you created above. |
   | `local_mqtt_pass` | That user's password. |
   | `heartbeat_seconds` | `15` (default; only change if you know why). |
   | `log_level` | `INFO` (use `DEBUG` only when troubleshooting). |

4. Save → switch to the **Info** tab → click **Start**.
5. Open the **Log** tab and confirm you see lines like:

   ```
   INFO hookii-bridge [addon] login OK, jwt-len=…
   INFO hookii-bridge [addon] cloud-mqtt connected as Android_<your-email>_…, subscribing to N serial(s)
   INFO hookii-bridge [addon] SUB hk/server/mower/push/+/HKX…
   INFO hookii-bridge [addon] heartbeat thread starting interval=15s
   ```

   If you see `REST login failed` with `code: 5, msg: 该用户未注册`, your email is not registered on `iot.beta.hookii.com`. Try the credentials in the Hookii mobile app first. If you see `code: 2, msg: hookii-agent参数错误`, that's a Hookii server change and we'll need to update the add-on.

## Wire up Home Assistant sensors

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

- **REST login fails with `code: 5, msg: 该用户未注册` ("user not registered").** Since v1.0.4 the add-on auto-lowercases your email before sending it to Hookii (their beta server is case-sensitive), so this should not happen from a case mismatch alone. If you still see it, double-check the email matches the one you log in to the official Hookii app with, and that the account is registered on the *beta* environment (not just on Hookii's stable cloud).
- **REST login OK, MQTT connected, but no `RX hk/server/mower/push/...` lines ever appear.** The single most common cause: at least one of your mowers is still on stable firmware. The add-on connects to `iot.beta.hookii.com`, which is only populated by mowers on Hookii BETA firmware `1.6.8.4-beta` or newer. Open the Hookii app, confirm each mower's firmware has the `…-beta` suffix, and let pending updates install before retrying.
- **No sensors update at all.** Double-check the bridge logs show `cloud-mqtt connected` AND a `RX hk/server/mower/push/...` line within ~30 s. If the second is missing, the heartbeat isn't being accepted — verify your account works in the official Hookii app first.
- **REST login OK but `mowing_zero` / serial mismatch.** Open the bridge log, look for `learned model=… for serial=…`. If your serial never shows up there, you typed the wrong one in `mower_serials`. Check it against the Hookii app.
- **Sensor values are stuck.** Try setting `log_level: DEBUG`, restart the add-on, watch the logs and reload your YAML.
- **Multi-mower setup.** Each entry in `mower_serials` is independent. The bridge subscribes per-serial.

## Privacy and safety

- Your Hookii credentials never leave the add-on container. They're sent once to Hookii's REST API to obtain a JWT, then that JWT is reused for ~6 h.
- The cloud broker uses a self-signed TLS certificate (same as Hookii's mobile app). The bridge validates the *connection* but does **not** verify the certificate chain. This is a knowing trade-off; it's the only way to talk to their broker without official PKI.
- All other traffic stays on your LAN: the bridge publishes only to the local Mosquitto broker.

## Support

Issues, feature requests and "it worked / it didn't work on my model" reports: <https://github.com/conscientsystems/hookii-bridge-ha-addon/issues>
