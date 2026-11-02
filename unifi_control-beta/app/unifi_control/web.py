"""The web UI and its JSON API, served only to Home Assistant's Ingress."""

import ipaddress
import logging
import os
from pathlib import Path

from aiohttp import web

from . import __version__
from .errors import AppError, BadRequest
from .users import FEATURES

_LOGGER = logging.getLogger(__name__)

# Ingress requests always arrive from the Supervisor. Anything else on the
# internal network could fake the X-Remote-User-* headers, so it's refused.
TRUSTED_PROXIES = set(os.environ.get("UNIFI_CONTROL_TRUSTED_PROXIES", "172.30.32.2").split(","))
STATIC_DIR = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}

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
            request["user"] = await request.app["ctx"].users.identify(request)
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


def _client_ips(request: web.Request) -> list[dict]:
    """The browser's address(es) as Home Assistant saw them.

    Core appends the connecting address to X-Forwarded-For, then the
    Supervisor appends Core's own address - so drop the last hop. Anything
    before that came from a reverse proxy in front of Home Assistant.
    """
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()][:-1]
    seen, out = set(), []
    for hop in hops:
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if ip.version == 4 and str(ip) not in seen:
            seen.add(str(ip))
            out.append({"ip": str(ip), "public": ip.is_global})
    out.sort(key=lambda entry: not entry["public"])
    return out


def _ctx(request: web.Request):
    return request.app["ctx"]


# ------------------------------------------------------------------ pages


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- general


@routes.get("/api/me")
async def me(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    return _json(
        {
            "user": request["user"].to_json(),
            "labels": FEATURES,
            "settings": {
                "mode": ctx.opts.connection_mode,
                "durations": list(ctx.opts.grant_durations),
                "public_ip_lookup": ctx.opts.public_ip_lookup,
                "wifi_password_length": ctx.opts.wifi_password_length,
                "notify_services": list(ctx.opts.notify_services) if request["user"].is_admin else [],
            },
            "client_ips": _client_ips(request),
            "version": __version__,
        }
    )


@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    unifi = _ctx(request).unifi
    info = {"configured": unifi.configured, "mode": unifi.mode, "site": unifi.site}
    try:
        sysinfo = await unifi.sysinfo()
        info.update(ok=True, controller=unifi.kind_label, version=sysinfo.get("version"))
    except AppError as err:
        info.update(ok=False, controller=unifi.kind_label, error=str(err))
    return _json(info)


@routes.get("/api/history")
async def history(request: web.Request) -> web.Response:
    request["user"].require_admin()
    return _json({"history": list(reversed(_ctx(request).store.history))[:200]})


# ------------------------------------------------------------------ ports


@routes.get("/api/ports")
async def ports(request: web.Request) -> web.Response:
    return _json(await _ctx(request).access.overview(request["user"]))


@routes.post("/api/ports/{rule_id}/grant")
async def grant(request: web.Request) -> web.Response:
    data = await _body(request)
    result = await _ctx(request).access.grant(
        request["user"], request.match_info["rule_id"], str(data.get("ip", "")), data.get("hours"), str(data.get("note") or "")
    )
    return _json({"grant": result})


@routes.post("/api/ports/{rule_id}/open")
async def open_any(request: web.Request) -> web.Response:
    data = await _body(request)
    await _ctx(request).access.open_any(request["user"], request.match_info["rule_id"], data.get("hours"))
    return _json({"ok": True})


@routes.post("/api/ports/{rule_id}/close")
async def close_any(request: web.Request) -> web.Response:
    await _ctx(request).access.close_any(request["user"], request.match_info["rule_id"])
    return _json({"ok": True})


@routes.post("/api/grants/{grant_id}/revoke")
async def revoke(request: web.Request) -> web.Response:
    await _ctx(request).access.revoke(request["user"], request.match_info["grant_id"])
    return _json({"ok": True})


@routes.post("/api/admin/ports/{rule_id}/manage")
async def manage(request: web.Request) -> web.Response:
    await _ctx(request).access.manage(request["user"], request.match_info["rule_id"])
    return _json({"ok": True})


@routes.post("/api/admin/ports/{rule_id}/unmanage")
async def unmanage(request: web.Request) -> web.Response:
    await _ctx(request).access.unmanage(request["user"], request.match_info["rule_id"])
    return _json({"ok": True})


@routes.post("/api/admin/ports/{rule_id}/visibility")
async def port_visibility(request: web.Request) -> web.Response:
    data = await _body(request)
    _ctx(request).access.set_visible(request["user"], request.match_info["rule_id"], data.get("visible"))
    return _json({"ok": True})


# ------------------------------------------------------------------- wifi


@routes.get("/api/wifi")
async def wifi(request: web.Request) -> web.Response:
    return _json(await _ctx(request).wifi.overview(request["user"]))


@routes.get("/api/wifi/{wlan_id}/password")
async def wifi_password(request: web.Request) -> web.Response:
    return _json(await _ctx(request).wifi.password(request["user"], request.match_info["wlan_id"]))


@routes.get("/api/wifi/{wlan_id}/qr.svg")
async def wifi_qr(request: web.Request) -> web.Response:
    svg = await _ctx(request).wifi.qr_svg(request["user"], request.match_info["wlan_id"])
    return web.Response(text=svg, content_type="image/svg+xml", headers=NO_STORE)


@routes.post("/api/wifi/{wlan_id}/password")
async def wifi_set_password(request: web.Request) -> web.Response:
    data = await _body(request)
    await _ctx(request).wifi.set_password(request["user"], request.match_info["wlan_id"], data.get("password"))
    return _json({"ok": True})


@routes.post("/api/admin/wifi/{wlan_id}/visibility")
async def wifi_visibility(request: web.Request) -> web.Response:
    data = await _body(request)
    _ctx(request).wifi.set_visible(request["user"], request.match_info["wlan_id"], data.get("visible"))
    return _json({"ok": True})


# ------------------------------------------------------------------ admin


@routes.get("/api/admin/users")
async def users(request: web.Request) -> web.Response:
    request["user"].require_admin()
    return _json(await _ctx(request).users.admin_list())


@routes.post("/api/admin/users/{user_id}")
async def set_user(request: web.Request) -> web.Response:
    request["user"].require_admin()
    data = await _body(request)
    _ctx(request).users.set_user_features(request.match_info["user_id"], data.get("features"))
    return _json({"ok": True})


@routes.post("/api/admin/defaults")
async def set_defaults(request: web.Request) -> web.Response:
    request["user"].require_admin()
    data = await _body(request)
    _ctx(request).users.set_defaults(data.get("features"))
    return _json({"ok": True})


@routes.post("/api/admin/test-notification")
async def test_notification(request: web.Request) -> web.Response:
    user = request["user"]
    user.require_admin()
    ha = _ctx(request).ha
    if not ha.notify_services:
        raise BadRequest("No notify services are set up - add one under 'Notify services' on the Configuration tab.")
    failed = await ha.notify("✅ UniFi Control", f"Test notification sent by {user.name}. Notifications are working.")
    if failed:
        raise BadRequest(f"Failed: {', '.join('notify.' + f for f in failed)} - check the name(s) and the app's Log tab.")
    return _json({"ok": True})


def build_app(ctx) -> web.Application:
    app = web.Application(middlewares=[guard])
    app["ctx"] = ctx
    app.add_routes(routes)
    app.router.add_static("/static", STATIC_DIR)
    return app
