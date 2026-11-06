"""The app's Configuration tab, as written by the Supervisor to options.json."""

import json
import logging
import os
import re
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

OPTIONS_FILE = os.environ.get("TUYA_DOORBELL_OPTIONS", "/data/options.json")

# Values that mean "not pressed" when a rule has no "=value" part, e.g. a lock
# that reports doorbell=true on a press and doorbell=false straight after.
OFF_VALUES = {"", "false", "0", "off", "none", "null"}


def value_text(value) -> str:
    """A DP value as rules compare it: JSON-style booleans, lowercase, trimmed."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def is_on(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value_text(value) not in OFF_VALUES


@dataclass(frozen=True)
class Rule:
    """One "doorbell code" entry: `code` (any "on" value) or `code=value` (exact)."""

    code: str
    value: str | None = None

    def matches(self, code: str, value) -> bool:
        if code != self.code:
            return False
        if self.value is None:
            return is_on(value)
        return value_text(value) == self.value

    def __str__(self) -> str:
        return self.code if self.value is None else f"{self.code}={self.value}"

    @classmethod
    def parse(cls, text: str) -> "Rule | None":
        code, sep, value = str(text).partition("=")
        code = code.strip().lower()
        if not code:
            return None
        return cls(code, value.strip().lower() if sep else None)


@dataclass(frozen=True)
class Options:
    script: str = ""
    locks: tuple[str, ...] = ()
    doorbell_codes: tuple[Rule, ...] = (Rule("doorbell"),)
    cooldown_seconds: int = 10
    log_level: str = "info"


def _clean_script(value) -> str:
    """Accept "script.front_door", "front_door" or "Script.Front_Door"."""
    name = str(value or "").strip().lower()
    if not name:
        return ""
    if "." not in name:
        name = f"script.{name}"
    if not re.fullmatch(r"script\.[a-z0-9_]+", name):
        _LOGGER.error("'%s' isn't a script entity ID (expected something like script.doorbell) - ignoring it", value)
        return ""
    return name


def load_options(path: str = OPTIONS_FILE) -> Options:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        _LOGGER.warning("%s not found - running with default options", path)
        raw = {}
    except ValueError as err:
        _LOGGER.error("%s is not valid JSON (%s) - running with default options", path, err)
        raw = {}

    defaults = Options()

    rules = []
    for item in raw.get("doorbell_codes") or []:
        rule = Rule.parse(item)
        if rule and rule not in rules:
            rules.append(rule)

    locks = tuple(dict.fromkeys(s for s in (str(x).strip() for x in raw.get("locks") or []) if s))

    try:
        cooldown = int(raw.get("cooldown_seconds", defaults.cooldown_seconds))
    except (TypeError, ValueError):
        cooldown = defaults.cooldown_seconds

    return Options(
        script=_clean_script(raw.get("script")),
        locks=locks,
        doorbell_codes=tuple(rules) or defaults.doorbell_codes,
        cooldown_seconds=min(300, max(0, cooldown)),
        log_level=str(raw.get("log_level") or defaults.log_level).strip().lower(),
    )
