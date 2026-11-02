"""Client for the UniFi Network application's API.

This is the same API the UniFi web UI uses, reached one of three ways:

* local UniFi OS console (UCG, UDM, UDR, UCK Gen2+):  https://<host>/proxy/network/api/...
* local legacy / self-hosted Network server:         https://<host>:8443/api/...
* UniFi cloud, via Ubiquiti's Site Manager connector:
      https://api.ui.com/v1/connector/consoles/<console id>/proxy/network/api/...
"""

import asyncio
import json
import logging

import aiohttp

from .errors import UnifiError, UnifiNotFound
from .options import Options

_LOGGER = logging.getLogger(__name__)

CLOUD_BASE = "https://api.ui.com/v1/connector/consoles"
GATEWAY_TYPES = {"ugw", "udm", "uxg"}
KIND_LABELS = {
    "unifi_os": "UniFi OS console (local)",
    "legacy": "UniFi Network server (local)",
    "cloud": "UniFi cloud (Site Manager connector)",
}
NOT_CONFIGURED = (
    "The UniFi connection isn't set up yet. An admin can fill it in on this app's "
    "Configuration tab (Settings → Apps → UniFi Control)."
)
BAD_LOGIN = (
    "UniFi rejected the username/password. Use a local admin account on the console "
    "(UI.com accounts protected by MFA can't sign in from apps), or an API key."
)


class UnifiClient:
    def __init__(self, opts: Options):
        self.opts = opts
        self.mode = opts.connection_mode
        self.site = opts.site
        # Held around every read-modify-write so two people granting access at
        # the same moment can't overwrite each other's IP-list change.
        self.write_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None
        self._kind: str | None = None
        self._csrf: str | None = None
        self._login_gen = 0

    # ----------------------------------------------------------- plumbing

    @property
    def configured(self) -> bool:
        if self.mode == "cloud":
            return bool(self.opts.cloud_api_key and self.opts.cloud_console_id)
        return bool(self.opts.local_host and (self.opts.local_api_key or (self.opts.local_username and self.opts.local_password)))

    @property
    def kind_label(self) -> str | None:
        return KIND_LABELS.get(self._kind or "")

    @property
    def _api_key(self) -> str:
        return self.opts.cloud_api_key if self.mode == "cloud" else self.opts.local_api_key

    @property
    def _ssl(self):
        # None = normal certificate checks; False = accept the console's self-signed cert
        return None if self.mode == "cloud" or self.opts.local_verify_ssl else False

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            # unsafe=True: keep cookies for bare IP hosts like 192.168.1.1
            self._http = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    def _host_base(self) -> str:
        port = "" if self.opts.local_port == 443 else f":{self.opts.local_port}"
        return f"https://{self.opts.local_host}{port}"

    def _api_base(self) -> str:
        if self._kind == "cloud":
            return f"{CLOUD_BASE}/{self.opts.cloud_console_id}/proxy/network"
        if self._kind == "unifi_os":
            return f"{self._host_base()}/proxy/network"
        return self._host_base()

    def _unreachable(self, err: Exception) -> UnifiError:
        where = "api.ui.com" if self.mode == "cloud" else self._host_base()
        return UnifiError(f"Can't reach UniFi at {where}: {err or 'timed out'}")

    async def _ensure_kind(self) -> None:
        if self._kind:
            return
        if self.mode == "cloud":
            self._kind = "cloud"
        elif self._api_key:
            self._kind = "unifi_os"  # API keys only exist on UniFi OS
        else:
            # UniFi OS answers "/" itself; the legacy server redirects to /manage.
            try:
                async with self._session().get(f"{self._host_base()}/", allow_redirects=False, ssl=self._ssl) as resp:
                    self._kind = "unifi_os" if resp.status == 200 else "legacy"
            except (aiohttp.ClientError, TimeoutError) as err:
                raise self._unreachable(err) from err
        _LOGGER.info("Talking to: %s", self.kind_label)

    async def _login(self, seen_gen: int) -> None:
        async with self._login_lock:
            if self._login_gen != seen_gen:
                return  # another request already signed in again meanwhile
            for attempt in (0, 1):
                if self._kind == "unifi_os":
                    url = f"{self._host_base()}/api/auth/login"
                    body = {"username": self.opts.local_username, "password": self.opts.local_password, "rememberMe": True}
                else:
                    url = f"{self._host_base()}/api/login"
                    body = {"username": self.opts.local_username, "password": self.opts.local_password, "remember": True}
                try:
                    async with self._session().post(url, json=body, ssl=self._ssl) as resp:
                        status = resp.status
                        csrf = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
                except (aiohttp.ClientError, TimeoutError) as err:
                    raise self._unreachable(err) from err
                if status == 404 and attempt == 0:
                    # Guessed the wrong controller type - try the other login path once.
                    self._kind = "legacy" if self._kind == "unifi_os" else "unifi_os"
                    continue
                break
            if status in (400, 401, 403, 499):
                raise UnifiError(BAD_LOGIN)
            if status >= 400:
                raise UnifiError(f"UniFi login failed (HTTP {status})")
            if not csrf and self._kind == "legacy":
                cookie = self._session().cookie_jar.filter_cookies(self._host_base()).get("csrf_token")
                csrf = cookie.value if cookie else None
            self._csrf = csrf
            self._login_gen += 1
            _LOGGER.debug("Signed in to UniFi")

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-KEY"] = self._api_key
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        return headers

    async def request(self, method: str, path: str, payload=None):
        """Call a Network API path such as "/api/s/default/rest/portforward"."""
        if not self.configured:
            raise UnifiError(NOT_CONFIGURED)
        await self._ensure_kind()
        uses_login = not self._api_key
        for attempt in (0, 1):
            gen = self._login_gen
            if uses_login and gen == 0:
                await self._login(0)
                gen = self._login_gen
            try:
                async with self._session().request(
                    method, self._api_base() + path, json=payload, headers=self._headers(), ssl=self._ssl
                ) as resp:
                    status = resp.status
                    text = await resp.text()
                    if new_csrf := resp.headers.get("X-Updated-CSRF-Token"):
                        self._csrf = new_csrf
            except (aiohttp.ClientError, TimeoutError) as err:
                raise self._unreachable(err) from err
            if status == 401 and uses_login and attempt == 0:
                await self._login(gen)  # session expired
                continue
            return self._parse(method, path, status, text)

    def _parse(self, method: str, path: str, status: int, text: str):
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None
        meta = body.get("meta") if isinstance(body, dict) else None
        message = None
        if isinstance(meta, dict) and meta.get("rc", "ok") != "ok":
            message = meta.get("msg") or "unknown error"
        elif status >= 400:
            if isinstance(body, dict):
                message = body.get("message") or body.get("error")
            message = message or text.strip()[:200] or f"HTTP {status}"
        if not message:
            return body.get("data", body) if isinstance(body, dict) else body

        _LOGGER.debug("UniFi %s %s -> %s %s", method, path, status, message)
        if message in ("api.err.IdInvalid", "api.err.NotFound") or (status == 404 and "/rest/" in path):
            raise UnifiNotFound("That item no longer exists in UniFi.")
        if message == "api.err.NoSiteContext":
            raise UnifiError(f"UniFi has no site called '{self.site}'. Check the 'site' option.")
        if status == 401:
            raise UnifiError("UniFi rejected the API key." if self._api_key else BAD_LOGIN)
        if status == 403:
            raise UnifiError("The UniFi account/API key isn't allowed to do that - it needs admin rights in UniFi Network.")
        if status == 429:
            raise UnifiError("UniFi is rate-limiting requests - try again in a minute.")
        raise UnifiError(f"UniFi said: {message}")

    def _site(self, suffix: str) -> str:
        return f"/api/s/{self.site}/{suffix}"

    async def _one(self, path: str) -> dict:
        items = await self.request("GET", path)
        if not items:
            raise UnifiNotFound("That item no longer exists in UniFi.")
        return items[0]

    # -------------------------------------------------------------- calls

    async def sysinfo(self) -> dict:
        items = await self.request("GET", self._site("stat/sysinfo"))
        return items[0] if items else {}

    async def port_forwards(self) -> list[dict]:
        return await self.request("GET", self._site("rest/portforward"))

    async def port_forward(self, rule_id: str) -> dict:
        return await self._one(self._site(f"rest/portforward/{rule_id}"))

    async def update_port_forward(self, rule: dict) -> None:
        await self.request("PUT", self._site(f"rest/portforward/{rule['_id']}"), rule)

    async def firewall_groups(self) -> list[dict]:
        return await self.request("GET", self._site("rest/firewallgroup"))

    async def firewall_group(self, group_id: str) -> dict:
        return await self._one(self._site(f"rest/firewallgroup/{group_id}"))

    async def create_address_group(self, name: str, members: list[str]) -> dict:
        items = await self.request(
            "POST",
            self._site("rest/firewallgroup"),
            {"name": name, "group_type": "address-group", "group_members": members},
        )
        return items[0]

    async def update_firewall_group(self, group: dict) -> None:
        await self.request("PUT", self._site(f"rest/firewallgroup/{group['_id']}"), group)

    async def wlans(self) -> list[dict]:
        return await self.request("GET", self._site("rest/wlanconf"))

    async def wlan(self, wlan_id: str) -> dict:
        return await self._one(self._site(f"rest/wlanconf/{wlan_id}"))

    async def set_wlan_passphrase(self, wlan_id: str, passphrase: str) -> None:
        await self.request("PUT", self._site(f"rest/wlanconf/{wlan_id}"), {"x_passphrase": passphrase})

    async def provision_gateways(self) -> int:
        """Push config to the gateway(s) right away instead of waiting for UniFi to do it."""
        devices = await self.request("GET", self._site("stat/device"))
        gateways = [d for d in devices if d.get("type") in GATEWAY_TYPES and d.get("mac")]
        for device in gateways:
            await self.request("POST", self._site("cmd/devmgr"), {"cmd": "force-provision", "mac": device["mac"]})
        return len(gateways)
