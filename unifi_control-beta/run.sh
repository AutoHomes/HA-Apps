#!/usr/bin/with-contenv bashio

bashio::log.info "unifi_control: run.sh starting"

cd /app || exit 1
exec python3 -u -m unifi_control
