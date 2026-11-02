"""Home Assistant Core, reached through the Supervisor's proxy."""

import asyncio
import logging
import os
import time

import aiohttp

from .errors import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
USERS_TTL = 60


class HomeAssistant:
    def __init__(self, notify_services: tuple[str, ...]):
        self.notify_services = notify_services
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._http: aiohttp.ClientSession | None = None
        self._users: list[dict] | None = None
        self._users_at = 0.0
        self._users_lock = asyncio.Lock()

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    async def users(self, force: bool = False) -> list[dict]:
        """All Home Assistant users (needs admin rights, which the Supervisor has)."""
        async with self._users_lock:
            if not force and self._users is not None and time.monotonic() - self._users_at < USERS_TTL:
                return self._users
            try:
                async with asyncio.timeout(15):
                    self._users = await self._ws_command({"type": "config/auth/list"})
            except (aiohttp.ClientError, TimeoutError) as err:
                raise HomeAssistantError(f"Couldn't load Home Assistant users: {err or 'timed out'}") from err
            self._users_at = time.monotonic()
            return self._users

    async def _ws_command(self, command: dict):
        async with self._session().ws_connect(f"{SUPERVISOR_URL}/core/websocket") as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": self._token})
            reply = await ws.receive_json()
            if reply.get("type") != "auth_ok":
                raise HomeAssistantError("Home Assistant refused the app's API token")
            await ws.send_json({"id": 1, **command})
            while True:
                reply = await ws.receive_json()
                if reply.get("id") == 1:
                    break
            if not reply.get("success"):
                raise HomeAssistantError(f"Home Assistant: {reply.get('error', {}).get('message', 'command failed')}")
            return reply["result"]

    async def notify(self, title: str, message: str) -> list[str]:
        """Send to every configured notify service; returns the ones that failed."""
        failed = []
        for name in self.notify_services:
            payload = {"title": title, "message": message}
            if name.startswith("mobile_app_"):
                payload["data"] = {"group": "unifi-control"}
            try:
                async with self._session().post(f"{SUPERVISOR_URL}/core/api/services/notify/{name}", json=payload) as resp:
                    if resp.status >= 400:
                        raise HomeAssistantError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
            except (aiohttp.ClientError, TimeoutError, HomeAssistantError) as err:
                _LOGGER.warning("Notification via notify.%s failed: %s", name, err)
                failed.append(name)
        return failed

    async def fire_event(self, event_type: str, data: dict) -> None:
        try:
            async with self._session().post(f"{SUPERVISOR_URL}/core/api/events/{event_type}", json=data) as resp:
                if resp.status >= 400:
                    _LOGGER.warning("Firing %s failed: HTTP %s", event_type, resp.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Firing %s failed: %s", event_type, err)
