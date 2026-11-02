"""History log + Home Assistant event + admin phone notification for each action."""

import logging
import time
from datetime import datetime

from .ha import HomeAssistant
from .store import Store

_LOGGER = logging.getLogger(__name__)

EVENT_TYPE = "unifi_control_event"


def when(ts: float) -> str:
    """Local wall-clock time, with the day added when it isn't today."""
    moment = datetime.fromtimestamp(ts).astimezone()
    if moment.date() == datetime.now().astimezone().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%a %d %b %H:%M")


def hours_label(hours: int | None) -> str:
    if hours is None:
        return "no expiry"
    return f"{hours} hour" if hours == 1 else f"{hours} hours"


class Events:
    def __init__(self, ha: HomeAssistant, store: Store):
        self.ha = ha
        self.store = store

    async def record(self, action: str, summary: str, *, title: str | None = None, actor=None, **details) -> None:
        """Log an action; pass `title` to also push it to the admin's phone."""
        entry = {"ts": time.time(), "action": action, "summary": summary, **details}
        if actor is not None:
            entry["actor"] = actor.name
            entry["actor_id"] = actor.id
        _LOGGER.info("%s", summary)
        self.store.add_history(entry)
        self.store.save()
        await self.ha.fire_event(EVENT_TYPE, entry)
        if title:
            await self.ha.notify(title, summary)
