# Conscient Systems Home Assistant Add-ons

This repository contains a single Home Assistant add-on:

- **Hookii Bridge** — reverse-engineered cloud bridge for **Hookii Neomow** robot mowers so Home Assistant can read live STATUS (battery, position, knife RPM, motor temps, work mode, …) again after the 2026 cloud protocol change.

## Add this repository to your Home Assistant

1. In Home Assistant: **Settings → Add-ons → Add-on store**
2. Click the **⋮** menu (top right) → **Repositories**
3. Paste this URL:

   ```
   https://github.com/conscientsystems/hookii-bridge-ha-addon
   ```

4. Click **Add → Close**
5. Reload the Add-on store. **Hookii Bridge** appears under "Conscient Systems Add-ons".
6. Click it → **Install**, then follow the [Hookii Bridge setup guide](hookii_bridge/DOCS.md).

## Why does this exist?

In May 2026 Hookii migrated their cloud from a passive-subscribe MQTT bus to a JWT-gated heartbeat protocol on `iot.beta.hookii.com`. The old "just subscribe to `hookii/details/device/<serial>`" trick stopped working from one day to the next, and the official Hookii app became the only client that could see the new protocol.

This add-on logs in to Hookii's REST API with your account, opens an MQTT session to their new broker, keeps a heartbeat alive, normalises the payload back to the legacy shape, and republishes everything to **your own Mosquitto broker** on the original topic format. Any existing Home Assistant template-sensors, automations, dashboards or n8n flows that read from `hookii/details/device/<serial>` keep working unchanged.

## Disclaimer

This add-on is **not affiliated with Hookii**. It is a community workaround built so existing Home Assistant integrations keep working after a vendor-side protocol change. The cloud MQTT broker certificate is self-signed and validated insecurely — same trade-off the official mobile app makes.

Licensed Apache-2.0.
