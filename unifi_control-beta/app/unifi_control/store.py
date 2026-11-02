"""The app's own state (grants, permissions, visibility, history) in /data."""

import json
import logging
import os
import time

_LOGGER = logging.getLogger(__name__)

STATE_FILE = os.environ.get("UNIFI_CONTROL_STATE", "/data/state.json")
HISTORY_LIMIT = 500


def _empty() -> dict:
    return {
        "version": 1,
        # port forward _id -> {"group_id", "name", "visible", "open_until", "open_by", ...}
        "rules": {},
        # [{"id", "rule_id", "ip", "user_id", "user_name", "note", "created_at", "expires_at", "preexisting"}]
        "grants": [],
        # wlanconf _id -> {"visible"}
        "wifi": {},
        # HA user id -> {"features": {...}}; users without an entry follow "defaults"
        "users": {},
        "defaults": None,
        "history": [],
    }


class Store:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        data = _empty()
        try:
            with open(self.path, encoding="utf-8") as fh:
                data.update(json.load(fh))
        except FileNotFoundError:
            pass
        except ValueError as err:
            # Keep the broken file for inspection instead of silently losing
            # track of grants that still need to expire.
            backup = f"{self.path}.corrupt-{int(time.time())}"
            os.replace(self.path, backup)
            _LOGGER.error("State file was unreadable (%s); moved it to %s and started fresh", err, backup)
        return data

    def save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    @property
    def rules(self) -> dict:
        return self.data["rules"]

    @property
    def grants(self) -> list:
        return self.data["grants"]

    @property
    def wifi(self) -> dict:
        return self.data["wifi"]

    @property
    def users(self) -> dict:
        return self.data["users"]

    @property
    def history(self) -> list:
        return self.data["history"]

    def add_history(self, entry: dict) -> None:
        self.history.append(entry)
        del self.history[:-HISTORY_LIMIT]
