"""Smart Life / Tuya cloud: QR sign-in, the device list, and the real-time report feed.

Built on Tuya's device-sharing SDK - the same sign-in and MQTT feed as Home
Assistant's own Tuya integration. That integration only subscribes to the
devices it created entities for, so an unsupported lock's reports (including
its doorbell button) never reach Home Assistant. This app runs its own feed:

- every device on the account is subscribed, so you can see the feed works
  and spot a doorbell that's reported by a separate device (e.g. the camera
  half of a video lock);
- locks are subscribed to both of Tuya's per-device topics ("/pen" carries
  raw DP numbers, "/sta" carries status codes), because which one carries a
  lock's events varies;
- reports are read raw, so DPs the SDK can't name still come through.

The SDK is synchronous (requests + a paho-mqtt thread): its calls run in
worker threads and MQTT messages are handed to the event loop.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests
from tuya_sharing import LoginControl, Manager, SharingTokenListener, strategy
from tuya_sharing.exceptions import ApiRequestException
from tuya_sharing.mq import SharingMQ

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
SUBSCRIBE_BATCH = 20
# The same report arriving on both of a lock's topics counts once.
DEDUPE_SECONDS = 3
LOG_MESSAGE_CHARS = 1500

MQTT_CONNECT_ERRORS = {
    1: "unacceptable protocol version",
    2: "client ID rejected",
    3: "server unavailable",
    4: "bad username or password",
    5: "not authorised",
}

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
    """One data point a device reported, e.g. doorbell = true."""

    lock_id: str
    lock_name: str
    code: str
    value: Any
    dp_id: int | None
    ts: float
    watched: bool = True
    dp_ts: int | None = None  # the device's own timestamp for the report, when Tuya sends one


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


def message_device_id(msg) -> str | None:
    """The device a feed message is about, if any."""
    if not isinstance(msg, dict):
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    biz = data.get("bizData")
    return data.get("devId") or (biz.get("devId") if isinstance(biz, dict) else None)


def decode_status(device, item: dict, dp_map: dict | None = None) -> tuple[str, Any, int | None]:
    """(code, value, dp_id) for one entry of a device report.

    Devices report either by code ({"code": "doorbell", "value": true}) or by
    DP number ({"dpId": 24, "value": true}). DP numbers are translated with
    the device's DP map; ones Tuya doesn't map become "dp_<number>" instead of
    being dropped, so they can still be used as a doorbell code.
    """
    value = item.get("value")
    dp_id = _int(item.get("dpId"))
    if item.get("code"):
        return str(item["code"]), value, dp_id
    if dp_id is None:
        return "unknown", value, None
    mapping = dp_map or getattr(device, "local_strategy", None) or {}
    entry = mapping.get(dp_id) or mapping.get(str(dp_id))
    if not entry:
        return f"dp_{dp_id}", value, dp_id
    try:
        code, value = strategy.convert(entry["value_convert"], (entry["status_code"], value), entry["config_item"])
    except Exception:  # noqa: BLE001 - the SDK raises a bare Exception for unknown strategies
        code = entry.get("status_code")
    return str(code or f"dp_{dp_id}"), value, dp_id


def parse_reports(msg: dict, devices: dict, dp_maps: dict | None = None, watched: set | dict | None = None) -> list[Report]:
    """The reports in one feed message, for devices in `devices` (id -> device)."""
    if not isinstance(msg, dict) or msg.get("protocol") != PROTOCOL_DEVICE_REPORT:
        return []
    data = msg.get("data") or {}
    if not isinstance(data, dict):
        return []
    device = devices.get(data.get("devId"))
    if device is None:
        return []
    status = data.get("status")
    if not isinstance(status, list):
        return []
    dp_map = (dp_maps or {}).get(device.id)
    is_watched = True if watched is None else device.id in watched
    now = time.time()
    reports = []
    for item in status:
        if not isinstance(item, dict) or "value" not in item:
            continue
        code, value, dp_id = decode_status(device, item, dp_map)
        name = getattr(device, "name", "") or device.id
        reports.append(Report(device.id, name, code, _jsonable(value), dp_id, now, is_watched, _int(item.get("t"))))
    return reports


def device_codes(device, dp_map: dict | None = None) -> set[str]:
    """Every DP code Tuya says the device has."""
    codes = set(getattr(device, "status", None) or {})
    codes |= set(getattr(device, "status_range", None) or {})
    codes |= set(getattr(device, "function", None) or {})
    for mapping in (getattr(device, "local_strategy", None) or {}, dp_map or {}):
        for entry in mapping.values():
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


def _short_json(msg) -> str:
    try:
        text = json.dumps(msg, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = repr(msg)
    return text if len(text) <= LOG_MESSAGE_CHARS else text[:LOG_MESSAGE_CHARS] + "…"


class FeedStats:
    """What has come through the live feed, for the Log tab and the app's page.

    Written from paho's thread, read on the event loop; plain counters are fine
    under the GIL and dict() copies are atomic.
    """

    def __init__(self):
        self.connected_at: float | None = None
        self.subscribed = 0
        self.refused: list[str] = []
        self.messages = 0
        self.last_message_at: float | None = None
        self.per_device: dict[str, dict] = {}

    def note_message(self, device_id: str | None) -> None:
        now = time.time()
        self.messages += 1
        self.last_message_at = now
        if device_id:
            entry = self.per_device.get(device_id) or {"count": 0, "last": None}
            self.per_device[device_id] = {"count": entry["count"] + 1, "last": now}

    def to_json(self) -> dict:
        return {
            "connected_at": self.connected_at,
            "subscribed": self.subscribed,
            "refused": len(self.refused),
            "messages": self.messages,
            "last_message_at": self.last_message_at,
        }


class FeedMQ(SharingMQ):
    """The SDK's MQTT client, subscribing every device (locks on both topics) and saying what Tuya accepted."""

    def __init__(self, customer_api, owner_ids: list, devices: list, both_topics: set[str], stats: FeedStats):
        super().__init__(customer_api, owner_ids, devices)
        self.both_topics = both_topics
        self.stats = stats
        self._pending: dict[int, tuple[str, list[str]]] = {}
        self._extras_in_flight = False
        self._extras_disabled = False

    def _on_connect(self, mqttc, user_data, flags, rc):
        if rc != 0:
            _LOGGER.warning("Tuya's live feed refused the connection: %s (rc=%s)", MQTT_CONNECT_ERRORS.get(rc, "unknown"), rc)
            super()._on_connect(mqttc, user_data, flags, rc)  # rc 5: the SDK reconnects with fresh credentials
            return
        self._pending.clear()
        self.stats.connected_at = time.time()
        self.stats.subscribed = 0
        self.stats.refused = []
        topics = [self.mq_config.owner_topic.format(ownerId=owner) for owner in self.owner_ids]
        topics += [self.subscribe_topic(dev.id, dev.support_local) for dev in self.device]
        for start in range(0, len(topics), SUBSCRIBE_BATCH):
            self._subscribe(mqttc, "main", topics[start : start + SUBSCRIBE_BATCH])
        extras = []
        if not self._extras_disabled:
            extras = [self.subscribe_topic(dev.id, not dev.support_local) for dev in self.device if dev.id in self.both_topics]
        if extras:
            self._extras_in_flight = True
            self._subscribe(mqttc, "extra", extras)
        _LOGGER.info(
            "Connected to Tuya's live feed - subscribing to %s device(s) and %s home(s)%s",
            len(self.device),
            len(self.owner_ids),
            f", plus {len(extras)} lock(s) on their second topic" if extras else "",
        )

    def _subscribe(self, mqttc, kind: str, topics: list[str]) -> None:
        # Runs on paho's thread inside on_connect, so the SUBACK can't arrive
        # before the mid is recorded.
        result, mid = mqttc.subscribe([(topic, 0) for topic in topics])
        if result == 0:
            self._pending[mid] = (kind, topics)
        else:
            _LOGGER.warning("Couldn't send a subscription to Tuya (paho error %s)", result)

    def _on_subscribe(self, mqttc, user_data, mid, granted_qos):
        kind, topics = self._pending.pop(mid, (None, []))
        if kind is None:
            return  # a device the SDK subscribed on its own (newly paired)
        refused = [topic for topic, qos in zip(topics, granted_qos, strict=False) if qos == 128]
        accepted = len(topics) - len(refused)
        if kind == "extra":
            self._extras_in_flight = False
            if refused:
                _LOGGER.info("Tuya doesn't offer a second topic for %s lock(s) - that's fine", len(refused))
            if accepted:
                _LOGGER.info("Listening to %s lock(s) on both of Tuya's topics", accepted)
        else:
            self.stats.subscribed += accepted
            if refused:
                self.stats.refused.extend(refused)
                _LOGGER.warning("Tuya refused %s of %s subscription(s): %s", len(refused), len(topics), ", ".join(refused))
        if not self._pending:
            _LOGGER.info(
                "Tuya accepted %s subscription(s)%s - waiting for reports",
                self.stats.subscribed,
                f", refused {len(self.stats.refused)}" if self.stats.refused else "",
            )

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            self.stats.connected_at = None
            if self._extras_in_flight:
                # A broker that drops clients for a refused subscription: stop asking.
                self._extras_disabled = True
                _LOGGER.warning("Tuya closed the feed after the second-topic subscriptions - reconnecting without them")
            else:
                _LOGGER.warning("Lost the connection to Tuya's live feed (rc=%s) - reconnecting", rc)
        self._extras_in_flight = False
        super()._on_disconnect(client, userdata, rc)


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
        # Guards _manager/_devices/_watched/_dp_maps/_generation, which worker threads also touch.
        self._lock = threading.Lock()
        self._manager: Manager | None = None
        self._devices: dict[str, Any] = {}
        self._watched: dict[str, Any] = {}
        self._dp_maps: dict[str, dict] = {}
        # Bumped on sign-out/reconnect so a connect still running in a worker
        # thread knows its result is stale and throws it away.
        self._generation = 0
        self._connect_task: asyncio.Task | None = None
        self._login: dict | None = None
        self._login_lock = asyncio.Lock()
        self._recent: dict[tuple, tuple[float, int | None]] = {}
        self.stats = FeedStats()
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
        """Worker thread: sign in with the saved token, load devices, start the feed."""
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

        devices = {dev.id: dev for dev in list(manager.device_map.values()) if getattr(dev, "id", None)}
        watched = {dev_id: dev for dev_id, dev in devices.items() if self.watch_reason(dev) is not None}
        dp_maps = {dev_id: self._load_dp_map(manager, dev) for dev_id, dev in watched.items()}
        for dev_id, dev in watched.items():
            self._log_lock(dev, dp_maps[dev_id])

        mq = FeedMQ(
            manager.customer_api,
            [home.id for home in manager.user_homes],
            list(devices.values()),
            set(watched),
            self.stats,
        )
        mq.add_message_listener(manager.on_message)  # keeps the SDK's online/offline/rename handling
        mq.add_message_listener(self._on_mq_message)
        # Attached before it's installed: a sign-out that detaches this manager
        # from here on stops this feed, and a stopped feed's start() is a no-op.
        manager.mq = mq

        with self._lock:
            stale = generation != self._generation
            if not stale:
                old = self._manager
                self._manager, self._devices, self._watched, self._dp_maps = manager, devices, watched, dp_maps
        if stale:
            return
        mq.start()
        if old:
            self._stop_mq(old)

        if watched:
            names = ", ".join(f"{d.name} ({d.category})" for d in watched.values())
            _LOGGER.info("Listening to %s of %s Smart Life device(s): %s", len(watched), len(devices), names)
        else:
            _LOGGER.warning(
                "None of your %s Smart Life device(s) look like a lock. Add your lock's name or device ID under "
                "'Locks' on the Configuration tab (the app's page lists every device).",
                len(devices),
            )

    @staticmethod
    def _load_dp_map(manager: Manager, device) -> dict:
        """DP number -> {status_code, value_convert, config_item}, for every DP Tuya names.

        The SDK only keeps this for devices it decodes locally; locks often
        aren't, and then their raw "/pen" reports would only be "dp_<n>".
        """
        if getattr(device, "support_local", False) and getattr(device, "local_strategy", None):
            return dict(device.local_strategy)
        try:
            response = manager.customer_api.get(f"/v1.0/m/life/devices/{device.id}/status")
        except Exception as err:  # noqa: BLE001 - optional extra detail; any failure just means fewer names
            _LOGGER.debug("Couldn't load the DP list of %s: %s", device.name, err)
            return {}
        result = (response or {}).get("result") or {}
        dp_map = {}
        for rel in result.get("dpStatusRelationDTOS") or []:
            dp_id = _int(rel.get("dpId")) if isinstance(rel, dict) else None
            if dp_id is None or not rel.get("statusCode"):
                continue
            dp_map[dp_id] = {
                "value_convert": rel.get("valueConvert") or "default",
                "status_code": rel["statusCode"],
                "config_item": {
                    "statusFormat": rel.get("statusFormat"),
                    "valueDesc": rel.get("valueDesc"),
                    "valueType": rel.get("valueType"),
                    "enumMappingMap": rel.get("enumMappingMap"),
                    "pid": result.get("productKey"),
                },
            }
        return dp_map

    def _log_lock(self, device, dp_map: dict) -> None:
        wanted = {rule.code for rule in self.opts.doorbell_codes}
        dp_of = {entry["status_code"]: dp for dp, entry in dp_map.items()}
        found = [
            f"{code} (DP {dp_of[code]})" if code in dp_of else code for code in sorted(wanted & device_codes(device, dp_map))
        ]
        _LOGGER.info(
            "%s (%s, %s): reports by %s; doorbell data point: %s",
            device.name,
            device.category,
            "online" if getattr(device, "online", False) else "offline/asleep",
            "DP number" if getattr(device, "support_local", False) else "code",
            ", ".join(found) if found else "none listed - press the button and watch Live activity",
        )
        if dp_map:
            _LOGGER.info(
                "%s data points: %s",
                device.name,
                ", ".join(f"{dp} {entry['status_code']}" for dp, entry in sorted(dp_map.items())),
            )

    def _detach(self) -> Manager | None:
        with self._lock:
            self._generation += 1
            manager = self._manager
            self._manager, self._devices, self._watched, self._dp_maps = None, {}, {}, {}
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
        """Why a device's reports count as a lock's, or None if they don't."""
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
            device_id = message_device_id(msg)
            self.stats.note_message(device_id)
            with self._lock:
                devices, watched, dp_maps = self._devices, self._watched, self._dp_maps
            if device_id in watched:
                _LOGGER.info("Message from %s: %s", watched[device_id].name, _short_json(msg))
            else:
                _LOGGER.debug("Tuya message: %s", _short_json(msg))
            reports = parse_reports(msg, devices, dp_maps, watched)
            if reports:
                loop = self._loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(self._deliver, reports)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Couldn't read a Tuya message: %s", _short_json(msg))

    def _deliver(self, reports: list[Report]) -> None:
        now = time.monotonic()
        if len(self._recent) > 500:
            self._recent = {k: v for k, v in self._recent.items() if now - v[0] < DEDUPE_SECONDS}
        for report in reports:
            key = (report.lock_id, report.code, json.dumps(report.value, sort_keys=True, default=str))
            seen_at, seen_ts = self._recent.get(key, (None, None))
            # Two copies of one report (one per topic) carry the same device
            # timestamp; two real presses don't. Without timestamps, go by time.
            same = report.dp_ts is None or seen_ts is None or report.dp_ts == seen_ts
            if seen_at is not None and now - seen_at < DEDUPE_SECONDS and same:
                _LOGGER.debug(
                    "Skipping %s = %r from %s: already received on the other topic", report.code, report.value, report.lock_name
                )
                continue
            self._recent[key] = (now, report.dp_ts)
            if report.watched:
                self.last_report_at = time.time()
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
        devices, watched, dp_maps = self._devices, self._watched, self._dp_maps
        per_device = dict(self.stats.per_device)
        out = []
        for device in devices.values():
            dp_map = dp_maps.get(device.id) or {}
            codes = device_codes(device, dp_map)
            doorbell_dps = {entry["status_code"]: dp for dp, entry in dp_map.items()}
            seen = per_device.get(device.id) or {}
            out.append(
                {
                    "id": device.id,
                    "name": getattr(device, "name", "") or device.id,
                    "category": getattr(device, "category", "") or "",
                    "product": getattr(device, "product_name", "") or "",
                    "online": bool(getattr(device, "online", False)),
                    "watched": device.id in watched,
                    "reason": self.watch_reason(device),
                    "codes": sorted(codes),
                    "doorbell_codes": sorted(r.code for r in self.opts.doorbell_codes if r.code in codes),
                    "doorbell_dps": {r.code: doorbell_dps[r.code] for r in self.opts.doorbell_codes if r.code in doorbell_dps},
                    "messages": seen.get("count", 0),
                    "last_message_at": seen.get("last"),
                }
            )
        out.sort(key=lambda d: (not d["watched"], d["name"].lower()))
        return out

    def status(self) -> dict:
        auth = self.store.auth
        per_device = dict(self.stats.per_device)
        return {
            "state": self.state if auth else "signed_out",
            "error": self.error,
            "retry_at": self.retry_at,
            "account": (auth or {}).get("username") or "",
            "live": self.mqtt_connected(),
            "last_report_at": self.last_report_at,
            "feed": {
                **self.stats.to_json(),
                "lock_messages": sum(per_device.get(dev_id, {}).get("count", 0) for dev_id in self._watched),
            },
            "login_pending": self._login is not None,
            "login_started": self._login["started"] if self._login else None,
        }
