"""The web UI and its JSON API, served only to Home Assistant's Ingress."""

import io
import logging
import os
from pathlib import Path

import segno
from aiohttp import web

from . import __version__
from .errors import AppError, BadRequest, Forbidden

_LOGGER = logging.getLogger(__name__)

# Ingress requests always arrive from the Supervisor. Anything else on the
# internal network could fake the X-Remote-User-* headers, so it's refused.
TRUSTED_PROXIES = set(os.environ.get("TUYA_DOORBELL_TRUSTED_PROXIES", "172.30.32.2").split(","))
HEADER_USER_ID = "X-Remote-User-Id"
STATIC_DIR = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}
MAX_VALUE_CHARS = 300

routes = web.RouteTableDef()


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=NO_STORE)


@web.middleware
async def guard(request: web.Request, handler):
    if request.remote not in TRUSTED_PROXIES:
        _LOGGER.warning("Refused a request from %s - only Home Assistant's Ingress may connect", request.remote)
        return web.Response(status=403, text="Open this app from the Home Assistant sidebar.")
    try:
        if request.path.startswith("/api/"):
            # A JSON body can't be sent cross-site without a CORS preflight,
            # which this app never answers - cheap CSRF protection.
            if request.method not in ("GET", "HEAD") and request.content_type != "application/json":
                raise BadRequest("Expected a JSON request.")
            user_id = request.headers.get(HEADER_USER_ID)
            if not user_id:
                raise Forbidden("Open this app from the Home Assistant sidebar - it can't tell who you are otherwise.")
            if not await request.app["ctx"].ha.is_admin(user_id):
                raise Forbidden("Only Home Assistant admins can use this app.")
        return await handler(request)
    except web.HTTPException:
        raise
    except AppError as err:
        return _json({"error": str(err)}, status=err.status)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Unexpected error handling %s %s", request.method, request.path)
        return _json({"error": "Something went wrong - see the app's Log tab for details."}, status=500)


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise BadRequest("Invalid JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("Expected a JSON object.")
    return data


def _ctx(request: web.Request):
    return request.app["ctx"]


def _short(value):
    """Keep huge values (e.g. a picture a video lock attaches) out of the page."""
    if isinstance(value, (dict, list)):
        text = str(value)
        return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    return value


# ------------------------------------------------------------------ pages


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------- status


@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    opts = ctx.opts
    return _json(
        {
            "version": __version__,
            "tuya": ctx.tuya.status(),
            "devices": ctx.tuya.devices(),
            "settings": {
                "script": await ctx.doorbell.script_info(),
                "doorbell_codes": [str(rule) for rule in opts.doorbell_codes],
                "locks": list(opts.locks),
                "cooldown_seconds": opts.cooldown_seconds,
            },
            "feed": [{**entry, "value": _short(entry["value"])} for entry in ctx.doorbell.feed],
            "all_feed": [{**entry, "value": _short(entry["value"])} for entry in ctx.doorbell.all_feed],
            "rings": [{**entry, "value": _short(entry.get("value"))} for entry in ctx.store.rings[:20]],
        }
    )


# ---------------------------------------------------------------- sign-in


@routes.post("/api/login")
async def login(request: web.Request) -> web.Response:
    data = await _body(request)
    await _ctx(request).tuya.begin_login(data.get("user_code"))
    return _json({"ok": True})


@routes.get("/api/login/qr.svg")
async def login_qr(request: web.Request) -> web.Response:
    payload = _ctx(request).tuya.login_qr_payload()
    if not payload:
        raise BadRequest("No sign-in in progress.")
    # A standalone SVG file (with xmlns), not svg_inline(): it's served as an <img>.
    buf = io.BytesIO()
    segno.make(payload, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff", xmldecl=False)
    return web.Response(text=buf.getvalue().decode(), content_type="image/svg+xml", headers=NO_STORE)


@routes.post("/api/login/check")
async def login_check(request: web.Request) -> web.Response:
    return _json({"state": await _ctx(request).tuya.check_login()})


@routes.post("/api/login/cancel")
async def login_cancel(request: web.Request) -> web.Response:
    _ctx(request).tuya.cancel_login()
    return _json({"ok": True})


@routes.post("/api/logout")
async def logout(request: web.Request) -> web.Response:
    await _ctx(request).tuya.logout()
    return _json({"ok": True})


# ---------------------------------------------------------------- actions


@routes.post("/api/devices/refresh")
async def refresh_devices(request: web.Request) -> web.Response:
    tuya = _ctx(request).tuya
    if not _ctx(request).store.auth:
        raise BadRequest("Sign in to Smart Life first.")
    tuya.reconnect()
    return _json({"ok": True})


@routes.post("/api/test")
async def test_ring(request: web.Request) -> web.Response:
    result = await _ctx(request).doorbell.ring("test", "Test from the app", "doorbell", True, test=True)
    return _json(result)


def build_app(ctx) -> web.Application:
    app = web.Application(middlewares=[guard])
    app["ctx"] = ctx
    app.add_routes(routes)
    app.router.add_static("/static", STATIC_DIR)
    return app
