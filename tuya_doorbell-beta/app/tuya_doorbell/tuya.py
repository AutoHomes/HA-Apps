"""Smart Life / Tuya cloud: QR sign-in, the device list, and the real-time report feed.

Built on Tuya's device-sharing SDK - the same sign-in and MQTT feed as Home
Assistant's own Tuya integration. That integration only subscribes to the
devices it created entities for, so an unsupported lock's reports (including
its doorbell button) never reach Home Assistant. This app subscribes to the
locks itself and reads every report raw, so even DPs the SDK doesn't know
about come through.

The SDK is synchronous (requests + a paho-mqtt thread): its calls run in
worker threads and MQTT messages are handed to the event loop.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from tuya_sharing import LoginControl, Manager, SharingTokenListener, strategy
from tuya_sharing.exceptions import ApiRequestException

from .errors import BadRequest, TuyaError
from .options import Options
from .store import Store

_LOGGER = logging.getLogger(__name__)

# Home Assistant's own Tuya integration (homeassistant/components/tuya/const.py).
# Smart Life only offers QR sign-in to known clients; this one is what the app
# presents itself as, exactly like the official integration.
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
QR_PREFIX = "tuyaSmart--qrLogin?token="

PROTOCOL_DEVICE_REPORT = 4
RETRY_DELAYS = (15, 30, 60, 120, 300)

# Tuya product categories that are locks. Devices outside these are still
# watched when they have one of the configured doorbell DPs.
LOCK_CATEGORIES = {
    "ms",  # residential lock
    "jtmspro",  # residential lock pro
    "jtmsbh",  # smart lock (keep-alive)
    "videolock",  # lock with camera
    "photolock",  # audio & video lock
    "gyms",  # business lock
    "hotelms",  # hotel lock
    "bxx",  # safe box
    "mk",  # access control
}


@dataclass(frozen=True)
class Report:
    """One data point a lock reported, e.g. doorbell = true."""

    lock_id: str
    lock_name: str
    code: str
    value: Any
    dp_id: int | None
    ts: float


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=str)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decode_status(device, item: dict) -> tuple[str, Any, int | None]:
    """(code, value, dp_id) for one entry of a device report.

    Devices report either by code ({"code": "doorbell", "value": true}) or by
    DP number ({"dpId": 19, "value": true}). DP numbers are translated with
    the device's own mapping; ones Tuya doesn't map become "dp_<number>"
    instead of being dropped, so they can still be used as a doorbell code.
    """
    value = item.get("value")
    dp_id = _int(item.get("dpId"))
    if item.get("code"):
        return str(item["code"]), value, dp_id
    if dp_id is None:
        return "unknown", value, None
    mapping = getattr(device, "local_strategy", None) or {}
    entry = mapping.get(dp_id) or mapping.get(str(dp_id))
    if not entry:
        return f"dp_{dp_id}", value, dp_id
    try:
        code, value = strategy.convert(entry["value_convert"], (entry["status_code"], value), entry["config_item"])
    except Exception:  # noqa: BLE001 - the SDK raises a bare Exception for unknown strategies
        code = entry.get("status_code")
    return str(code or f"dp_{dp_id}"), value, dp_id


def parse_reports(msg: dict, devices: dict) -> list[Report]:
    """The reports in one MQTT message, for devices in `devices` (id -> device)."""
    if not isinstance(msg, dict) or msg.get("protocol") != PROTOCOL_DEVICE_REPORT:
        return []
    data = msg.get("data") or {}
    device = devices.get(data.get("devId"))
    if device is None:
        return []
    now = time.time()
    reports = []
    for item in data.get("status") or []:
        if not isinstance(item, dict) or "value" not in item:
            continue
        code, value, dp_id = decode_status(device, item)
        reports.append(Report(device.id, getattr(device, "name", "") or device.id, code, _jsonable(value), dp_id, now))
    return reports


def device_codes(device) -> set[str]:
    """Every DP code Tuya says the device has."""
    codes = set(getattr(device, "status", None) or {})
    codes |= set(getattr(device, "status_range", None) or {})
    codes |= set(getattr(device, "function", None) or {})
    for entry in (getattr(device, "local_strategy", None) or {}).values():
        if isinstance(entry, dict) and entry.get("status_code"):
            codes.add(entry["status_code"])
    return codes


def _describe_error(err: Exception) -> str:
    if isinstance(err, ApiRequestException):
        return f"Tuya refused the request: {err.error_message} (code {err.error_code})"
    if isinstance(err, requests.RequestException):
        return f"Couldn't reach Tuya's cloud: {err.__class__.__name__}"
    if isinstance(err, (AttributeError, KeyError, TypeError)):
        # The SDK assumes a successful JSON reply; an HTTP error surfaces as this.
        return "Tuya's cloud returned an unexpected reply (see the Log tab)"
    return str(err) or err.__class__.__name__


class _TokenSaver(SharingTokenListener):
    """The SDK refreshes the access token itself (every ~2 h); keep the new one."""

    def __init__(self, store: Store):
        self.store = store

    def update_token(self, token_info: dict[str, Any]):
        _LOGGER.debug("Tuya access token refreshed")
        self.store.update_token(token_info)


class TuyaCloud:
    def __init__(self, opts: Options, store: Store, on_report: Callable[[Report], None]):
        self.opts = opts
        self.store = store
        self._on_report = on_report
        self._token_saver = _TokenSaver(store)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Guards _manager/_watched/_generation, which worker threads also touch.
        self._lock = threading.Lock()
        self._manager: Manager | None = None
        self._watched: dict[str, Any] = {}
        # Bumped on sign-out/reconnect so a connect still running in a worker
        # thread knows its result is stale and throws it away.
        self._generation = 0
        self._connect_task: asyncio.Task | None = None
        self._login: dict | None = None
        self._login_lock = asyncio.Lock()
        self.state = "signed_out"  # signed_out | connecting | connected | error
        self.error: str | None = None
        self.retry_at: float | None = None
        self.last_report_at: float | None = None

    # ---------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.store.auth:
            self.reconnect()
        else:
            _LOGGER.info("Not signed in to Smart Life yet - open the app from the sidebar to sign in")

    async def stop(self) -> None:
        manager = self._detach()
        if self._connect_task:
            self._connect_task.cancel()
        if manager:
            await asyncio.to_thread(self._stop_mq, manager)

    def reconnect(self) -> None:
        """(Re)load the device list and (re)subscribe, in the background."""
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._connect_task = asyncio.create_task(self._connect_loop(generation))

    async def _connect_loop(self, generation: int) -> None:
        attempt = 0
        while self.store.auth:
            self.state, self.retry_at = "connecting", None
            try:
                await asyncio.to_thread(self._connect, generation)
            except Exception as err:  # pylint: disable=broad-except
                if generation != self._generation:
                    return
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                attempt += 1
                self.state, self.error, self.retry_at = "error", _describe_error(err), time.time() + delay
                _LOGGER.error("Couldn't connect to Smart Life: %s - retrying in %s s", self.error, delay)
                if not isinstance(err, (ApiRequestException, requests.RequestException)):
                    _LOGGER.debug("Connect failure details", exc_info=err)
                await asyncio.sleep(delay)
                continue
            if generation == self._generation:
                self.state, self.error = "connected", None
            return

    def _connect(self, generation: int) -> None:
        """Worker thread: sign in with the saved token, load devices, subscribe to the locks."""
        auth = self.store.auth
        if not auth:
            raise TuyaError("Not signed in")
        manager = Manager(
            CLIENT_ID,
            auth["user_code"],
            auth["terminal_id"],
            auth["endpoint"],
            auth["token_info"],
            self._token_saver,
        )
        manager.update_device_cache()

        watched = {}
        for device in list(manager.device_map.values()):
            # set_up is the SDK's "subscribe to this device's reports" flag.
            device.set_up = self.watch_reason(device) is not None
            if device.set_up:
                watched[device.id] = device

        manager.refresh_mq()
        manager.mq.add_message_listener(self._on_mq_message)

        with self._lock:
            stale = generation != self._generation
            if not stale:
                old, self._manager, self._watched = self._manager, manager, watched
        if stale:
            self._stop_mq(manager)
            return
        if old:
            self._stop_mq(old)

        if watched:
            names = ", ".join(f"{d.name} ({d.category})" for d in watched.values())
            _LOGGER.info("Listening to %s of %s Smart Life device(s): %s", len(watched), len(manager.device_map), names)
        else:
            _LOGGER.warning(
                "None of your %s Smart Life device(s) look like a lock. Add your lock's name or device ID under "
                "'Locks' on the Configuration tab (the app's page lists every device).",
                len(manager.device_map),
            )

    def _detach(self) -> Manager | None:
        with self._lock:
            self._generation += 1
            manager, self._manager, self._watched = self._manager, None, {}
        return manager

    @staticmethod
    def _stop_mq(manager: Manager) -> None:
        try:
            if manager.mq is not None:
                manager.mq.stop()
        except Exception as err:  # noqa: BLE001 - best effort while shutting down
            _LOGGER.debug("Stopping the Tuya MQTT client failed: %s", err)

    # --------------------------------------------------------- the feed

    def watch_reason(self, device) -> str | None:
        """Why a device's reports are listened to, or None if they aren't."""
        if self.opts.locks:
            wanted = {item.lower() for item in self.opts.locks}
            if device.id.lower() in wanted or (getattr(device, "name", "") or "").strip().lower() in wanted:
                return "Listed under Locks"
            return None
        codes = device_codes(device)
        if any(rule.code in codes for rule in self.opts.doorbell_codes):
            return "Has a doorbell data point"
        if getattr(device, "category", None) in LOCK_CATEGORIES:
            return "Lock"
        return None

    def _on_mq_message(self, msg: dict) -> None:
        """paho's network thread - must never raise."""
        try:
            with self._lock:
                watched = self._watched
            reports = parse_reports(msg, watched)
            if reports:
                _LOGGER.debug("Tuya message: %s", msg)
                loop = self._loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(self._deliver, reports)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Couldn't read a Tuya message: %s", msg)

    def _deliver(self, reports: list[Report]) -> None:
        self.last_report_at = time.time()
        for report in reports:
            self._on_report(report)

    # ---------------------------------------------------------- sign-in

    async def begin_login(self, user_code: str) -> None:
        user_code = str(user_code or "").strip()
        if not user_code:
            raise BadRequest("Enter the user code from the Smart Life app.")
        control = LoginControl()
        try:
            response = await asyncio.to_thread(control.qr_code, CLIENT_ID, SCHEMA, user_code)
        except (requests.RequestException, ValueError) as err:
            raise TuyaError(f"Couldn't reach Tuya's cloud: {err.__class__.__name__}") from err
        if not response.get("success") or not (response.get("result") or {}).get("qrcode"):
            reason = response.get("msg") or response.get("code") or "unknown error"
            raise BadRequest(f"Smart Life didn't accept that user code ({reason}). Check it and try again.")
        self._login = {
            "user_code": user_code,
            "token": response["result"]["qrcode"],
            "control": control,
            "started": time.time(),
        }
        _LOGGER.info("Smart Life sign-in started - waiting for the QR code to be scanned")

    def login_qr_payload(self) -> str | None:
        return QR_PREFIX + self._login["token"] if self._login else None

    def cancel_login(self) -> None:
        self._login = None

    async def check_login(self) -> str:
        """'none' (no sign-in pending), 'waiting' (not scanned/confirmed yet) or 'done'."""
        async with self._login_lock:
            login = self._login
            if not login:
                return "none"
            try:
                ok, info = await asyncio.to_thread(login["control"].login_result, login["token"], CLIENT_ID, login["user_code"])
            except (requests.RequestException, ValueError) as err:
                _LOGGER.debug("Checking the Smart Life sign-in failed: %s", err)
                return "waiting"
            if not ok:
                _LOGGER.debug("Smart Life sign-in not confirmed yet: %s", info.get("msg") or info.get("code"))
                return "waiting"
            if self._login is not login:
                return "none"
            self._login = None
            self.store.set_auth(
                {
                    "user_code": login["user_code"],
                    "terminal_id": info["terminal_id"],
                    "endpoint": info["endpoint"],
                    "username": info.get("username") or "",
                    "token_info": {
                        "t": info["t"],
                        "uid": info["uid"],
                        "expire_time": info["expire_time"],
                        "access_token": info["access_token"],
                        "refresh_token": info["refresh_token"],
                    },
                }
            )
            _LOGGER.info("Signed in to Smart Life%s", f" as {info['username']}" if info.get("username") else "")
            self.reconnect()
            return "done"

    async def logout(self) -> None:
        """Forget the sign-in, and tell Tuya to revoke this app's token."""
        self._login = None
        if self._connect_task:
            self._connect_task.cancel()
        manager = self._detach()
        self.store.set_auth(None)
        self.state, self.error, self.retry_at = "signed_out", None, None
        if manager:
            await asyncio.to_thread(self._unload, manager)
        _LOGGER.info("Signed out of Smart Life")

    def _unload(self, manager: Manager) -> None:
        self._stop_mq(manager)
        try:
            manager.unload()
        except Exception as err:  # noqa: BLE001 - best effort; the token expires anyway
            _LOGGER.warning("Couldn't revoke the Smart Life token (it expires on its own): %s", err)

    # ----------------------------------------------------------- status

    def mqtt_connected(self) -> bool:
        client = getattr(getattr(self._manager, "mq", None), "client", None)
        return bool(client and client.is_connected())

    def devices(self) -> list[dict]:
        manager = self._manager
        if manager is None:
            return []
        try:
            # The SDK's MQTT thread adds/removes devices when they're (un)paired.
            all_devices = list(manager.device_map.values())
        except RuntimeError:
            return []
        out = []
        for device in all_devices:
            codes = device_codes(device)
            out.append(
                {
                    "id": device.id,
                    "name": getattr(device, "name", "") or device.id,
                    "category": getattr(device, "category", "") or "",
                    "product": getattr(device, "product_name", "") or "",
                    "online": bool(getattr(device, "online", False)),
                    "watched": device.id in self._watched,
                    "reason": self.watch_reason(device),
                    "codes": sorted(codes),
                    "doorbell_codes": sorted(r.code for r in self.opts.doorbell_codes if r.code in codes),
                }
            )
        out.sort(key=lambda d: (not d["watched"], d["name"].lower()))
        return out

    def status(self) -> dict:
        auth = self.store.auth
        return {
            "state": self.state if auth else "signed_out",
            "error": self.error,
            "retry_at": self.retry_at,
            "account": (auth or {}).get("username") or "",
            "live": self.mqtt_connected(),
            "last_report_at": self.last_report_at,
            "login_pending": self._login is not None,
            "login_started": self._login["started"] if self._login else None,
        }
