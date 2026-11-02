"""Who is using the app (from Home Assistant's Ingress) and what they may do."""

import logging
from dataclasses import dataclass, field

from aiohttp import web

from .errors import BadRequest, Forbidden
from .ha import HomeAssistant
from .store import Store

_LOGGER = logging.getLogger(__name__)

# Home Assistant's Ingress adds these to every request it proxies to the app.
HEADER_USER_ID = "X-Remote-User-Id"
HEADER_USER_NAME = "X-Remote-User-Name"
HEADER_USER_DISPLAY_NAME = "X-Remote-User-Display-Name"
ADMIN_GROUP = "system-admin"

FEATURES = {
    "port_grant": "Grant port access to an IP",
    "port_permanent": "Grant port access with no expiry",
    "port_open_any": "Open a port to anyone (time-limited)",
    "wifi_view": "View WiFi passwords",
    "wifi_change": "Change WiFi passwords",
}
DEFAULT_FEATURES = {
    "port_grant": True,
    "port_permanent": False,
    "port_open_any": False,
    "wifi_view": True,
    "wifi_change": False,
}


@dataclass(frozen=True)
class User:
    id: str
    name: str
    username: str
    is_admin: bool
    features: dict = field(default_factory=dict)

    def can(self, feature: str) -> bool:
        return self.is_admin or bool(self.features.get(feature))

    def require(self, feature: str) -> None:
        if not self.can(feature):
            raise Forbidden(f"You don't have permission to {FEATURES[feature][0].lower()}{FEATURES[feature][1:]}.")

    def require_admin(self) -> None:
        if not self.is_admin:
            raise Forbidden("Only Home Assistant admins can do that.")

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "is_admin": self.is_admin,
            "features": {key: self.can(key) for key in FEATURES},
        }


def _is_admin(ha_user: dict) -> bool:
    return bool(ha_user.get("is_owner")) or ADMIN_GROUP in (ha_user.get("group_ids") or [])


class UserManager:
    def __init__(self, ha: HomeAssistant, store: Store):
        self.ha = ha
        self.store = store

    def _defaults(self) -> dict:
        return {**DEFAULT_FEATURES, **(self.store.data.get("defaults") or {})}

    def features_for(self, user_id: str) -> dict:
        custom = (self.store.users.get(user_id) or {}).get("features") or {}
        return {**self._defaults(), **custom}

    async def identify(self, request: web.Request) -> User:
        user_id = request.headers.get(HEADER_USER_ID)
        if not user_id:
            raise Forbidden("Open this app from the Home Assistant sidebar - it can't tell who you are otherwise.")
        ha_user = await self._lookup(user_id)
        if ha_user is None or not ha_user.get("is_active", True):
            raise Forbidden("Your Home Assistant user isn't active.")
        name = (
            request.headers.get(HEADER_USER_DISPLAY_NAME)
            or ha_user.get("name")
            or request.headers.get(HEADER_USER_NAME)
            or "Unknown user"
        )
        return User(
            id=user_id,
            name=name,
            username=request.headers.get(HEADER_USER_NAME) or ha_user.get("username") or "",
            is_admin=_is_admin(ha_user),
            features=self.features_for(user_id),
        )

    async def _lookup(self, user_id: str) -> dict | None:
        users = await self.ha.users()
        match = next((u for u in users if u.get("id") == user_id), None)
        if match is None:
            # Probably a user created in the last minute - refresh once.
            users = await self.ha.users(force=True)
            match = next((u for u in users if u.get("id") == user_id), None)
        return match

    async def admin_list(self) -> dict:
        people = []
        for ha_user in await self.ha.users(force=True):
            if ha_user.get("system_generated"):
                continue
            people.append(
                {
                    "id": ha_user["id"],
                    "name": ha_user.get("name") or ha_user.get("username") or "Unnamed user",
                    "username": ha_user.get("username") or "",
                    "is_admin": _is_admin(ha_user),
                    "is_active": ha_user.get("is_active", True),
                    "custom": ha_user["id"] in self.store.users,
                    "features": self.features_for(ha_user["id"]),
                }
            )
        people.sort(key=lambda p: (not p["is_admin"], p["name"].lower()))
        return {"users": people, "defaults": self._defaults(), "labels": FEATURES}

    @staticmethod
    def _clean(features) -> dict:
        if not isinstance(features, dict):
            raise BadRequest("features must be an object")
        unknown = set(features) - set(FEATURES)
        if unknown:
            raise BadRequest(f"Unknown feature(s): {', '.join(sorted(unknown))}")
        return {key: bool(value) for key, value in features.items()}

    def set_user_features(self, user_id: str, features) -> None:
        """features=None puts the user back on the defaults."""
        if features is None:
            self.store.users.pop(user_id, None)
        else:
            self.store.users[user_id] = {"features": {**self.features_for(user_id), **self._clean(features)}}
        self.store.save()

    def set_defaults(self, features) -> None:
        self.store.data["defaults"] = {**self._defaults(), **self._clean(features)}
        self.store.save()
