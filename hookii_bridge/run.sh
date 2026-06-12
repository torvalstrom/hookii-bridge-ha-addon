#!/usr/bin/with-contenv bashio
# Dual-mode launcher:
#   - HA add-on (Supervisor present)         → read /data/options.json via bashio
#   - Standalone k3s / docker (no Supervisor) → trust env vars already set
#
# A single image now covers both deployment shapes. The Supervisor probe is
# what flips the script between them: if `bashio::supervisor.ping` answers,
# we know an add-on host is wrapping us and we pull config from
# /data/options.json. Otherwise we trust the env vars the operator
# (Deployment yaml, docker -e, compose env block) already injected.
set -e

# Supervisor presence is detected via the SUPERVISOR_TOKEN env var, which is
# only injected when running under HA Supervisor. Probing `bashio::supervisor.ping`
# directly triggers bashio internals that reference SUPERVISOR_TOKEN, and on
# some base images that combines with `set -u` to kill the script before our
# probe even returns - so we check the env var FIRST and only then call
# bashio.
if [ -n "${SUPERVISOR_TOKEN:-}" ] && command -v bashio >/dev/null 2>&1; then
  # Hosted as an HA add-on - hydrate env from options.json.
  HOOKII_EMAIL=$(bashio::config 'hookii_email')
  HOOKII_PASSWORD=$(bashio::config 'hookii_password')
  MOWER_SERIALS=$(bashio::config 'mower_serials')
  LOCAL_MQTT_HOST=$(bashio::config 'local_mqtt_host')
  LOCAL_MQTT_PORT=$(bashio::config 'local_mqtt_port')
  LOCAL_MQTT_USER=$(bashio::config 'local_mqtt_user')
  LOCAL_MQTT_PASS=$(bashio::config 'local_mqtt_pass')
  HEARTBEAT_SEC=$(bashio::config 'heartbeat_seconds')
  LOG_LEVEL=$(bashio::config 'log_level')
  HOOKII_AGENT=$(bashio::config 'hookii_agent')
  ENABLE_DISCOVERY=$(bashio::config 'enable_discovery')
  DISCOVERY_PREFIX=$(bashio::config 'discovery_prefix')
  # Server environment (beta|prod) + optional explicit host overrides.
  HOOKII_ENV=$(bashio::config 'hookii_env')
  HOOKII_REST_HOST_OPT=$(bashio::config 'hookii_rest_host')
  HOOKII_MQTT_HOST_OPT=$(bashio::config 'hookii_mqtt_host')
  # Cloud MQTT broker creds (prod broker needs a different static pair than beta).
  HOOKII_MQTT_USER_OPT=$(bashio::config 'hookii_mqtt_user')
  HOOKII_MQTT_PASS_OPT=$(bashio::config 'hookii_mqtt_pass')

  if [ -z "${HOOKII_EMAIL}" ] || [ -z "${HOOKII_PASSWORD}" ]; then
    echo "FATAL: hookii_email and hookii_password are required - configure the add-on first." >&2
    exit 1
  fi
  if [ -z "${MOWER_SERIALS}" ]; then
    echo "FATAL: mower_serials is required (comma-separated serial numbers)." >&2
    exit 1
  fi
  # The add-on form is single-account; collapse into the multi-account env
  # shape the Python expects ("addon" is the per-run label used in logs).
  export HOOKII_ACCOUNTS="addon:${HOOKII_EMAIL}:${HOOKII_PASSWORD}"
  export HOOKII_SERIALS_ADDON="${MOWER_SERIALS}"
  # bashio writes booleans as "true"/"false"; bridge.py reads "1"/"0".
  if [ "${ENABLE_DISCOVERY}" = "true" ] || [ "${ENABLE_DISCOVERY}" = "1" ]; then
    export ENABLE_DISCOVERY=1
  else
    export ENABLE_DISCOVERY=0
  fi
fi

# From here down both modes are identical. Validate the minimum the Python
# entrypoint needs regardless of where the values came from.
if [ -z "${HOOKII_ACCOUNTS}" ]; then
  echo "FATAL: HOOKII_ACCOUNTS is required - multi-account spec" >&2
  echo "       label1:email1:password1[;label2:email2:password2...]" >&2
  echo "       For single-account add-on use, set HOOKII_EMAIL + HOOKII_PASSWORD" >&2
  echo "       in /data/options.json and a Supervisor will wrap them for you." >&2
  exit 1
fi
if [ -z "${LOCAL_MQTT_USER}" ] || [ -z "${LOCAL_MQTT_PASS}" ]; then
  echo "FATAL: LOCAL_MQTT_USER / LOCAL_MQTT_PASS are required - must match a user on your broker." >&2
  exit 1
fi

export LOCAL_MQTT_HOST="${LOCAL_MQTT_HOST:-core-mosquitto}"
export LOCAL_MQTT_PORT="${LOCAL_MQTT_PORT:-1883}"
export LOCAL_MQTT_USER
export LOCAL_MQTT_PASS
export HEARTBEAT_SEC="${HEARTBEAT_SEC:-1.5}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
# Leave HOOKII_AGENT unset if the operator didn't pick one - bridge.py has a
# known-good default (a PCAP-verified Xiaomi fingerprint). The Hookii server
# returns "hookii-agent参数错误" if the format is wrong, so we MUST NOT shadow
# the Python default with a placeholder string here.
if [ -n "${HOOKII_AGENT:-}" ]; then
  export HOOKII_AGENT
fi
export ENABLE_DISCOVERY="${ENABLE_DISCOVERY:-1}"
export DISCOVERY_PREFIX="${DISCOVERY_PREFIX:-homeassistant}"

# Hookii server environment: "beta" (default) or "prod". bridge.py maps this to
# iot.beta.hookii.com vs iot.hookii.com. Explicit host overrides win if set
# (empty add-on fields stay empty -> bridge.py uses the env preset). Standalone
# k3s/docker users can just set HOOKII_ENV / HOOKII_REST_HOST / HOOKII_MQTT_HOST
# directly in their Deployment env.
export HOOKII_ENV="${HOOKII_ENV:-beta}"
if [ -n "${HOOKII_REST_HOST_OPT:-}" ]; then
  export HOOKII_REST_HOST="${HOOKII_REST_HOST_OPT}"
fi
if [ -n "${HOOKII_MQTT_HOST_OPT:-}" ]; then
  export HOOKII_MQTT_HOST="${HOOKII_MQTT_HOST_OPT}"
fi
# Cloud MQTT broker credential overrides. bridge.py reads HOOKII_MQTT_USER /
# HOOKII_MQTT_PASS (defaulting to the beta broker's static pair). Set both when
# pointing at the prod broker, which rejects the beta pair ("Bad user name or
# password"). Only export when non-empty so beta users keep the working default.
if [ -n "${HOOKII_MQTT_USER_OPT:-}" ]; then
  export HOOKII_MQTT_USER="${HOOKII_MQTT_USER_OPT}"
fi
if [ -n "${HOOKII_MQTT_PASS_OPT:-}" ]; then
  export HOOKII_MQTT_PASS="${HOOKII_MQTT_PASS_OPT}"
fi

# Legacy local topic shape - existing HA template sensors, n8n flows and
# Lovelace cards keep working unchanged across both deployment modes. NB
# the braces inside the default strings collide with bash's ${VAR:-...}
# parameter expansion (`{serial}}` closes the expansion prematurely), so
# we set the default the safe way with an if-block.
if [ -z "${LOCAL_TOPIC_FMT:-}" ]; then
  export LOCAL_TOPIC_FMT="hookii/details/device/{serial}"
fi
if [ -z "${CMD_TOPIC_FMT:-}" ]; then
  export CMD_TOPIC_FMT="hookii/cmd/{serial}/{action}"
fi

echo "Starting Hookii Bridge: broker=${LOCAL_MQTT_HOST}:${LOCAL_MQTT_PORT} accounts=$(echo "$HOOKII_ACCOUNTS" | awk -F: '{print $1; for(i=4;i<=NF;i+=3) print $i}' | tr '\n' ',')"
exec python3 /opt/bridge.py
