"""Turns lock reports into doorbell rings: run the script, fire events, keep a feed."""

import asyncio
import logging
import time
from collections import deque

from .errors import HomeAssistantError
from .ha import HomeAssistant
from .options import Options, Rule
from .store import Store
from .tuya import Report

_LOGGER = logging.getLogger(__name__)

EVENT_PRESSED = "tuya_doorbell_pressed"
EVENT_REPORT = "tuya_lock_report"
FEED_LIMIT = 60
ALL_FEED_LIMIT = 100
SCRIPT_CHECK_TTL = 15


class Doorbell:
    def __init__(self, opts: Options, ha: HomeAssistant, store: Store):
        self.opts = opts
        self.ha = ha
        self.store = store
        # Every report from a watched lock, newest first (memory only).
        self.feed: deque[dict] = deque(maxlen=FEED_LIMIT)
        # Every report from any device, to find which one reports the doorbell.
        self.all_feed: deque[dict] = deque(maxlen=ALL_FEED_LIMIT)
        self._last_ring: dict[str, float] = {}
        self._queue: asyncio.Queue[Report] = asyncio.Queue()
        self._script_info: dict | None = None
        self._script_checked = 0.0

    def submit(self, report: Report) -> None:
        """Called on the event loop for each report; handled in arrival order by run()."""
        self._queue.put_nowait(report)

    async def run(self) -> None:
        while True:
            report = await self._queue.get()
            try:
                await self._handle(report)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Handling %s = %r from %s failed", report.code, report.value, report.lock_name)

    def match(self, code: str, value) -> Rule | None:
        return next((rule for rule in self.opts.doorbell_codes if rule.matches(code, value)), None)

    async def _handle(self, report: Report) -> None:
        rule = self.match(report.code, report.value) if report.watched else None
        entry = {
            "ts": report.ts,
            "lock_id": report.lock_id,
            "lock_name": report.lock_name,
            "code": report.code,
            "value": report.value,
            "dp_id": report.dp_id,
            "watched": report.watched,
            "doorbell": rule is not None,
            "result": None,
            "ok": None,
        }
        self.all_feed.appendleft(entry)
        if not report.watched:
            return  # another device: shown on the page, never rings
        self.feed.appendleft(entry)
        _LOGGER.info(
            "%s reported %s = %r%s",
            report.lock_name,
            report.code,
            report.value,
            f"  -> doorbell (matches '{rule}')" if rule else "",
        )

        if rule:
            last = self._last_ring.get(report.lock_id)
            now = time.monotonic()
            if last is not None and now - last < self.opts.cooldown_seconds:
                entry["result"] = f"Ignored: rang {now - last:.0f} s ago (cooldown)"
                _LOGGER.info("Ignoring a repeat doorbell report from %s (cooldown)", report.lock_name)
            else:
                self._last_ring[report.lock_id] = now
                entry.update(await self.ring(report.lock_id, report.lock_name, report.code, report.value))

        # After the script, so a doorbell press isn't delayed by this.
        await self.ha.fire_event(
            EVENT_REPORT,
            {
                "lock_id": report.lock_id,
                "lock_name": report.lock_name,
                "code": report.code,
                "value": report.value,
                "dp_id": report.dp_id,
            },
        )

    async def ring(self, lock_id: str, lock_name: str, code: str, value, test: bool = False) -> dict:
        """Run the script and fire tuya_doorbell_pressed. Returns {"result", "ok"}."""
        variables = {"lock_id": lock_id, "lock_name": lock_name, "code": code, "value": value, "test": test}
        _LOGGER.info("Doorbell pressed on %s%s", lock_name, " (test)" if test else "")
        ok = True
        if self.opts.script:
            try:
                await self.ha.run_script(self.opts.script, variables)
                result = f"Ran {self.opts.script}"
            except HomeAssistantError as err:
                ok = False
                result = f"Running {self.opts.script} failed: {err}"
                _LOGGER.error("%s", result)
        else:
            result = "No script set, so only the event was fired"
            _LOGGER.warning("No script is set on the Configuration tab - only %s was fired", EVENT_PRESSED)
        await self.ha.fire_event(EVENT_PRESSED, variables)
        self.store.add_ring({"ts": time.time(), **variables, "result": result, "ok": ok})
        return {"result": result, "ok": ok}

    async def script_info(self) -> dict:
        """Whether the configured script exists in Home Assistant (cached briefly)."""
        if not self.opts.script:
            return {"entity_id": "", "exists": False, "name": ""}
        if self._script_info is None or time.monotonic() - self._script_checked > SCRIPT_CHECK_TTL:
            try:
                state = await self.ha.state(self.opts.script)
                self._script_info = {
                    "entity_id": self.opts.script,
                    "exists": state is not None,
                    "name": ((state or {}).get("attributes") or {}).get("friendly_name") or "",
                }
            except HomeAssistantError as err:
                self._script_info = {"entity_id": self.opts.script, "exists": None, "name": "", "error": str(err)}
            self._script_checked = time.monotonic()
        return self._script_info
