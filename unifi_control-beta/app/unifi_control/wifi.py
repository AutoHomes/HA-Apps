"""WiFi networks (UniFi "wlanconf") and their passwords."""

import io
import logging

import segno

from .errors import BadRequest, NotFound
from .events import Events
from .store import Store
from .unifi import UnifiClient
from .users import User

_LOGGER = logging.getLogger(__name__)


def _changeable(wlan: dict) -> bool:
    """Only WPA-Personal networks with a single shared password have one to change."""
    return wlan.get("security") == "wpapsk" and not wlan.get("private_preshared_keys_enabled")


def _security_label(wlan: dict) -> str:
    security = wlan.get("security")
    if security == "open":
        return "Open"
    if security == "wpaeap":
        return "Enterprise"
    if security != "wpapsk":
        return str(security or "Unknown").upper()
    if wlan.get("private_preshared_keys_enabled"):
        return "Private pre-shared keys"
    if wlan.get("wpa3_support"):
        return "WPA2/WPA3" if wlan.get("wpa3_transition") else "WPA3"
    return "WPA2"


def _qr_escape(value: str) -> str:
    for char in "\\;,\":":
        value = value.replace(char, "\\" + char)
    return value


def validate_passphrase(password) -> str:
    if not isinstance(password, str):
        raise BadRequest("Enter a password.")
    if not 8 <= len(password) <= 63:
        raise BadRequest("WiFi passwords must be 8 to 63 characters long.")
    if any(not 32 <= ord(c) <= 126 for c in password):
        raise BadRequest("Use plain letters, digits, spaces and standard symbols only.")
    return password


class WifiManager:
    def __init__(self, unifi: UnifiClient, store: Store, events: Events):
        self.unifi = unifi
        self.store = store
        self.events = events

    def _visible(self, wlan_id: str) -> bool:
        return bool((self.store.wifi.get(wlan_id) or {}).get("visible"))

    async def _get(self, user: User, wlan_id: str) -> dict:
        if not user.is_admin and not self._visible(wlan_id):
            raise NotFound("That WiFi network isn't available in this app.")
        return await self.unifi.wlan(wlan_id)

    async def overview(self, user: User) -> dict:
        networks = []
        for wlan in await self.unifi.wlans():
            visible = self._visible(wlan["_id"])
            if not user.is_admin and not visible:
                continue
            networks.append(
                {
                    "id": wlan["_id"],
                    "ssid": wlan.get("name") or "(no name)",
                    "enabled": wlan.get("enabled", True),
                    "security": _security_label(wlan),
                    "has_password": _changeable(wlan),
                    "hidden_ssid": bool(wlan.get("hide_ssid")),
                    "guest": bool(wlan.get("is_guest")),
                    "visible": visible,
                }
            )
        networks.sort(key=lambda n: n["ssid"].lower())
        return {"networks": networks}

    async def _viewable(self, user: User, wlan_id: str) -> dict:
        user.require("wifi_view")
        wlan = await self._get(user, wlan_id)
        if not _changeable(wlan):
            raise BadRequest("This network doesn't use a single shared password.")
        return wlan

    async def password(self, user: User, wlan_id: str) -> dict:
        wlan = await self._viewable(user, wlan_id)
        return {"ssid": wlan.get("name", ""), "password": wlan.get("x_passphrase", "")}

    async def qr_svg(self, user: User, wlan_id: str) -> str:
        """A "scan to join" code, the format phone cameras understand."""
        wlan = await self._viewable(user, wlan_id)
        payload = f"WIFI:T:WPA;S:{_qr_escape(wlan.get('name', ''))};P:{_qr_escape(wlan.get('x_passphrase', ''))};"
        if wlan.get("hide_ssid"):
            payload += "H:true;"
        payload += ";"
        # A standalone SVG file (with xmlns), not svg_inline(): it's served as an <img>.
        buf = io.BytesIO()
        segno.make(payload, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
        return buf.getvalue().decode()

    async def set_password(self, user: User, wlan_id: str, password) -> None:
        user.require("wifi_change")
        password = validate_passphrase(password)
        wlan = await self._get(user, wlan_id)
        if not _changeable(wlan):
            raise BadRequest("This network doesn't use a single shared password, so there's nothing to change.")
        async with self.unifi.write_lock:
            await self.unifi.set_wlan_passphrase(wlan_id, password)
        ssid = wlan.get("name", "?")
        await self.events.record(
            "wifi_password",
            f"{user.name} changed the WiFi password for {ssid}. Devices on it will need the new password to reconnect.",
            title="🔑 WiFi password changed", actor=user, ssid=ssid,
        )

    def set_visible(self, user: User, wlan_id: str, visible: bool) -> None:
        user.require_admin()
        self.store.wifi.setdefault(wlan_id, {})["visible"] = bool(visible)
        self.store.save()
