"""The app's own state in /data: the Smart Life sign-in and recent doorbell rings."""

import json
import logging
import os
import threading
import time

_LOGGER = logging.getLogger(__name__)

STATE_FILE = os.environ.get("TUYA_DOORBELL_STATE", "/data/state.json")
RINGS_LIMIT = 50


def _empty() -> dict:
    return {
        "version": 1,
        # {"user_code", "terminal_id", "endpoint", "username", "token_info": {t, uid, expire_time,
        #  access_token, refresh_token}} - None while signed out
        "auth": None,
        # newest first: [{"ts", "lock_id", "lock_name", "code", "value", "test", "result", "ok"}]
        "rings": [],
    }


class Store:
    """Saved from the event loop and from the Tuya SDK's threads (token refresh), hence the lock."""

    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        data = _empty()
        try:
            with open(self.path, encoding="utf-8") as fh:
                data.update(json.load(fh))
        except FileNotFoundError:
            pass
        except ValueError as err:
            backup = f"{self.path}.corrupt-{int(time.time())}"
            os.replace(self.path, backup)
            _LOGGER.error("State file was unreadable (%s); moved it to %s and started fresh", err, backup)
        return data

    def save(self) -> None:
        with self._lock:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    @property
    def auth(self) -> dict | None:
        return self.data.get("auth")

    def set_auth(self, auth: dict | None) -> None:
        with self._lock:
            self.data["auth"] = auth
            self.save()

    def update_token(self, token_info: dict) -> None:
        with self._lock:
            if self.data.get("auth"):
                self.data["auth"] = {**self.data["auth"], "token_info": token_info}
                self.save()

    @property
    def rings(self) -> list:
        with self._lock:
            return list(self.data["rings"])

    def add_ring(self, entry: dict) -> None:
        with self._lock:
            self.data["rings"] = [entry, *self.data["rings"]][:RINGS_LIMIT]
            self.save()
