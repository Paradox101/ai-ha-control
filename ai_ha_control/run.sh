#!/usr/bin/with-contenv sh
# with-contenv: s6-overlay starts CMD with an empty environment; this restores
# the container variables (SUPERVISOR_TOKEN, HOSTNAME, ...).
set -e
cd /opt/ai_ha_control
exec python3 -m app.main
