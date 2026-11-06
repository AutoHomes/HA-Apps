#!/usr/bin/with-contenv bashio

bashio::log.info "tuya_doorbell: run.sh starting"

cd /app || exit 1
exec python3 -u -m tuya_doorbell
