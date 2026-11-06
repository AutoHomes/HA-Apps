"""Exceptions that map straight onto an HTTP error for the web UI."""


class AppError(Exception):
    """Something the user should see as a plain message."""

    status = 400


class BadRequest(AppError):
    status = 400


class Forbidden(AppError):
    status = 403


class TuyaError(AppError):
    """Tuya's cloud (or the path to it) failed."""

    status = 502


class HomeAssistantError(AppError):
    """Home Assistant's own API (via the Supervisor) failed."""

    status = 503
