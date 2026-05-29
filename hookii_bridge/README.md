# Hookii Bridge

> ⚠️ **Requires Hookii BETA firmware `1.6.8.4-beta` or newer on every mower.** This add-on talks to `iot.beta.hookii.com`, which is only live on the BETA channel. Mowers on the stable firmware channel will not work. Switch each mower to the Beta channel in the Hookii mobile app first.

Cloud bridge for Hookii Neomow robot mowers (May 2026 protocol).

The original community workaround — "just MQTT-subscribe to `hookii/details/device/<serial>`" — stopped working when Hookii migrated their cloud to a JWT-gated heartbeat protocol on `iot.beta.hookii.com`. This add-on logs in to your Hookii account, keeps the heartbeat alive, normalises the payload back to the legacy shape and republishes it to **your own Mosquitto broker** on the original topic. Any existing Home Assistant template-sensors, automations, dashboards, n8n flows etc. keep working without modification.

For full setup instructions, see [DOCS.md](DOCS.md).
