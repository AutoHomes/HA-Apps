"""The app's Configuration tab, as written by the Supervisor to options.json."""

import json
import logging
import os
import re
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

OPTIONS_FILE = os.environ.get("UNIFI_CONTROL_OPTIONS", "/data/options.json")


@dataclass(frozen=True)
class Options:
    connection_mode: str = "local"
    local_host: str = ""
    local_port: int = 443
    local_username: str = ""
    local_password: str = ""
    local_api_key: str = ""
    local_verify_ssl: bool = False
    cloud_api_key: str = ""
    cloud_console_id: str = ""
    site: str = "default"
    force_provision: bool = False
    notify_services: tuple[str, ...] = ()
    grant_durations: tuple[int, ...] = (1, 2, 5)
    public_ip_lookup: bool = True
    wifi_password_length: int = 16
    log_level: str = "info"


def _clean_host(value: str) -> tuple[str, int | None]:
    """Accept "192.168.1.1", "https://unifi.lan/" or "unifi.lan:8443"."""
    host = re.sub(r"^[a-z]+://", "", value.strip(), flags=re.I).split("/")[0]
    match = re.fullmatch(r"(.+):(\d+)", host)
    if match and "]" not in match.group(1) and ":" not in match.group(1):
        return match.group(1), int(match.group(2))
    return host, None


def load_options(path: str = OPTIONS_FILE) -> Options:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        _LOGGER.warning("%s not found - running with default options", path)
        raw = {}
    except ValueError as err:
        _LOGGER.error("%s is not valid JSON (%s) - running with default options", path, err)
        raw = {}

    defaults = Options()

    def text(key: str) -> str:
        value = raw.get(key)
        return str(value).strip() if value is not None else getattr(defaults, key)

    host, port_in_host = _clean_host(text("local_host"))

    durations = set()
    for item in raw.get("grant_durations") or []:
        try:
            if int(item) > 0:
                durations.add(int(item))
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring invalid grant duration %r", item)

    services = tuple(
        name for name in (str(s).strip().removeprefix("notify.") for s in raw.get("notify_services") or []) if name
    )

    mode = text("connection_mode").lower()
    if mode not in ("local", "cloud"):
        _LOGGER.warning("Unknown connection_mode %r - using local", mode)
        mode = "local"

    return Options(
        connection_mode=mode,
        local_host=host,
        local_port=port_in_host or int(raw.get("local_port") or defaults.local_port),
        local_username=text("local_username"),
        local_password=str(raw.get("local_password") or ""),
        local_api_key=text("local_api_key"),
        local_verify_ssl=bool(raw.get("local_verify_ssl", defaults.local_verify_ssl)),
        cloud_api_key=text("cloud_api_key"),
        cloud_console_id=text("cloud_console_id"),
        site=text("site") or defaults.site,
        force_provision=bool(raw.get("force_provision", defaults.force_provision)),
        notify_services=services,
        grant_durations=tuple(sorted(durations)) or defaults.grant_durations,
        public_ip_lookup=bool(raw.get("public_ip_lookup", defaults.public_ip_lookup)),
        wifi_password_length=min(63, max(8, int(raw.get("wifi_password_length") or defaults.wifi_password_length))),
        log_level=text("log_level").lower() or defaults.log_level,
    )
