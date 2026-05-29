#!/usr/bin/with-contenv bashio
# Read the add-on options and start the bridge.

set -e

HOOKII_EMAIL=$(bashio::config 'hookii_email')
HOOKII_PASSWORD=$(bashio::config 'hookii_password')
MOWER_SERIALS=$(bashio::config 'mower_serials')
LOCAL_MQTT_HOST=$(bashio::config 'local_mqtt_host')
LOCAL_MQTT_PORT=$(bashio::config 'local_mqtt_port')
LOCAL_MQTT_USER=$(bashio::config 'local_mqtt_user')
LOCAL_MQTT_PASS=$(bashio::config 'local_mqtt_pass')
HEARTBEAT_SEC=$(bashio::config 'heartbeat_seconds')
LOG_LEVEL=$(bashio::config 'log_level')

if bashio::var.is_empty "${HOOKII_EMAIL}" || bashio::var.is_empty "${HOOKII_PASSWORD}"; then
  bashio::log.fatal "hookii_email and hookii_password are required - configure the add-on first."
  exit 1
fi
if bashio::var.is_empty "${MOWER_SERIALS}"; then
  bashio::log.fatal "mower_serials is required (comma-separated serial numbers, e.g. HKX1EB100JD25010115)."
  exit 1
fi
if bashio::var.is_empty "${LOCAL_MQTT_USER}" || bashio::var.is_empty "${LOCAL_MQTT_PASS}"; then
  bashio::log.fatal "local_mqtt_user / local_mqtt_pass are required - they must match a user on your Mosquitto broker."
  exit 1
fi

# Bridge expects a single-account spec ("addon" is the log label for this user).
export HOOKII_ACCOUNTS="addon:${HOOKII_EMAIL}:${HOOKII_PASSWORD}"
export HOOKII_SERIALS_ADDON="${MOWER_SERIALS}"
export LOCAL_MQTT_HOST="${LOCAL_MQTT_HOST}"
export LOCAL_MQTT_PORT="${LOCAL_MQTT_PORT}"
export LOCAL_MQTT_USER="${LOCAL_MQTT_USER}"
export LOCAL_MQTT_PASS="${LOCAL_MQTT_PASS}"
export HEARTBEAT_SEC="${HEARTBEAT_SEC:-15}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# Topic format matches the legacy "hookii/details/device/<serial>" so existing
# HA template sensors + automations + n8n flows keep working unchanged.
export LOCAL_TOPIC_FMT="hookii/details/device/{serial}"

bashio::log.info "Starting Hookii Bridge: serials=${MOWER_SERIALS} broker=${LOCAL_MQTT_HOST}:${LOCAL_MQTT_PORT}"
exec python3 /opt/bridge.py
