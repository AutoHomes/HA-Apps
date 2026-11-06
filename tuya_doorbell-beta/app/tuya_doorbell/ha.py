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
ADMIN_GROUP = "system-admin"


class HomeAssistant:
    def __init__(self):
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

    # ------------------------------------------------------------ scripts

    async def run_script(self, entity_id: str, variables: dict) -> None:
        """Start a script without waiting for it to finish (script.turn_on)."""
        body = {"entity_id": entity_id, "variables": variables}
        try:
            async with self._session().post(f"{SUPERVISOR_URL}/core/api/services/script/turn_on", json=body) as resp:
                if resp.status >= 400:
                    raise HomeAssistantError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(str(err) or "timed out") from err

    async def state(self, entity_id: str) -> dict | None:
        """An entity's current state, or None if Home Assistant doesn't have it."""
        try:
            async with self._session().get(f"{SUPERVISOR_URL}/core/api/states/{entity_id}") as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    raise HomeAssistantError(f"HTTP {resp.status}")
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(str(err) or "timed out") from err

    async def fire_event(self, event_type: str, data: dict) -> None:
        try:
            async with self._session().post(f"{SUPERVISOR_URL}/core/api/events/{event_type}", json=data) as resp:
                if resp.status >= 400:
                    _LOGGER.warning("Firing %s failed: HTTP %s", event_type, resp.status)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Firing %s failed: %s", event_type, err)

    # -------------------------------------------------------------- users

    async def is_admin(self, user_id: str) -> bool:
        user = await self._find_user(user_id)
        if user is None or not user.get("is_active", True):
            return False
        return bool(user.get("is_owner")) or ADMIN_GROUP in (user.get("group_ids") or [])

    async def _find_user(self, user_id: str) -> dict | None:
        for force in (False, True):  # a user created in the last minute needs a refresh
            match = next((u for u in await self._all_users(force) if u.get("id") == user_id), None)
            if match is not None:
                return match
        return None

    async def _all_users(self, force: bool) -> list[dict]:
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
