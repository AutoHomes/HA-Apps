"""Entry point: python3 -m unifi_control"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from aiohttp import web

from . import __version__
from .access import AccessManager
from .errors import AppError
from .events import Events
from .ha import HomeAssistant
from .options import Options, load_options
from .store import Store
from .unifi import UnifiClient
from .users import UserManager
from .web import build_app
from .wifi import WifiManager

_LOGGER = logging.getLogger("unifi_control")

PORT = 8099  # ingress_port in config.yaml
TICK_SECONDS = 15


@dataclass
class Context:
    opts: Options
    store: Store
    ha: HomeAssistant
    unifi: UnifiClient
    users: UserManager
    access: AccessManager
    wifi: WifiManager


async def _background(ctx: Context) -> None:
    try:
        info = await ctx.unifi.sysinfo()
        _LOGGER.info("Connected to UniFi Network %s, site '%s'", info.get("version", "?"), ctx.unifi.site)
    except AppError as err:
        _LOGGER.error("UniFi isn't reachable yet: %s", err)
    while True:
        try:
            await ctx.access.tick()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Expiry check failed")
        await asyncio.sleep(TICK_SECONDS)


async def _lifecycle(app: web.Application):
    ctx: Context = app["ctx"]
    task = asyncio.create_task(_background(ctx))
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await ctx.unifi.close()
    await ctx.ha.close()


def main() -> None:
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, opts.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s (%(name)s) %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _LOGGER.info("UniFi Control %s starting (%s mode)", __version__, opts.connection_mode)

    store = Store()
    ha = HomeAssistant(opts.notify_services)
    unifi = UnifiClient(opts)
    events = Events(ha, store)
    ctx = Context(
        opts=opts,
        store=store,
        ha=ha,
        unifi=unifi,
        users=UserManager(ha, store),
        access=AccessManager(unifi, store, events, opts),
        wifi=WifiManager(unifi, store, events),
    )
    app = build_app(ctx)
    app.cleanup_ctx.append(_lifecycle)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
