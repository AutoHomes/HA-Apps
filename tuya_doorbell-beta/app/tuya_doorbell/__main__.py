"""Entry point: python3 -m tuya_doorbell"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from aiohttp import web

from . import __version__
from .doorbell import Doorbell
from .ha import HomeAssistant
from .options import Options, load_options
from .store import Store
from .tuya import TuyaCloud
from .web import build_app

_LOGGER = logging.getLogger("tuya_doorbell")

PORT = 8099  # ingress_port in config.yaml


@dataclass
class Context:
    opts: Options
    store: Store
    ha: HomeAssistant
    tuya: TuyaCloud
    doorbell: Doorbell


async def _check_script(ctx: Context) -> None:
    info = await ctx.doorbell.script_info()
    if info["entity_id"] and info["exists"] is False:
        _LOGGER.warning(
            "%s doesn't exist in Home Assistant yet - create it, or fix 'Script' on the Configuration tab", info["entity_id"]
        )


async def _lifecycle(app: web.Application):
    ctx: Context = app["ctx"]
    worker = asyncio.create_task(ctx.doorbell.run())
    await ctx.tuya.start()
    checker = asyncio.create_task(_check_script(ctx))
    yield
    checker.cancel()
    await ctx.tuya.stop()
    worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker
    await ctx.ha.close()


def main() -> None:
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, opts.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s (%(name)s) %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # The SDK adds its own console handler (duplicate lines) and logs tokens at
    # debug level - route it through ours and keep it to warnings and errors.
    sdk_logger = logging.getLogger("tuya_sharing")
    sdk_logger.handlers.clear()
    sdk_logger.setLevel(logging.WARNING)

    _LOGGER.info("Tuya Lock Doorbell %s starting", __version__)
    _LOGGER.info(
        "Doorbell codes: %s | script: %s | cooldown: %s s",
        ", ".join(str(r) for r in opts.doorbell_codes),
        opts.script or "(none)",
        opts.cooldown_seconds,
    )

    store = Store()
    ha = HomeAssistant()
    doorbell = Doorbell(opts, ha, store)
    tuya = TuyaCloud(opts, store, doorbell.submit)
    ctx = Context(opts=opts, store=store, ha=ha, tuya=tuya, doorbell=doorbell)
    app = build_app(ctx)
    app.cleanup_ctx.append(_lifecycle)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
