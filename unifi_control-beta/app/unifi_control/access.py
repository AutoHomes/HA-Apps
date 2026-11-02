"""Timed port-forward access, backed by a UniFi IP list (firewall address group).

A port forward is "managed" once its source is limited to an IP list. Granting
access adds an IP to that list; when the grant expires (or is revoked) the IP is
taken out again - unless another grant still needs it, or it was already in
the list before this app touched it. "Open to anyone" temporarily turns the
source limit off and puts it back when the window ends.
"""

import ipaddress
import logging
import secrets
import time

from .errors import BadRequest, NotFound, UnifiError, UnifiNotFound
from .events import Events, hours_label, when
from .options import Options
from .store import Store
from .unifi import UnifiClient
from .users import User

_LOGGER = logging.getLogger(__name__)

# UniFi needs at least one entry in an IP list, and an empty list attached to
# a port forward is ambiguous. 192.0.2.1 is reserved for documentation
# (RFC 5737) and can never be a real internet source address.
PLACEHOLDER_IP = "192.0.2.1"
GROUP_PREFIX = "HA access - "
NOTE_LIMIT = 120
PROTO_LABELS = {"tcp_udp": "TCP/UDP", "tcp": "TCP", "udp": "UDP"}
# Wait this long after an action fails before trying it again.
RETRY_AFTER = 60


def rule_label(rule: dict) -> str:
    proto = PROTO_LABELS.get(rule.get("proto", ""), str(rule.get("proto", "")).upper())
    return f"{rule.get('name') or 'Unnamed rule'} ({proto} {rule.get('dst_port', '?')})"


def parse_source(value: str, allow_wide: bool) -> str:
    """Normalise an IPv4 address or CIDR range typed by a user."""
    value = (value or "").strip()
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise BadRequest(f"'{value}' isn't a valid IPv4 address or range (e.g. 203.0.113.7 or 203.0.113.0/24).") from None
    if network.version != 4:
        raise BadRequest("UniFi port forwarding is IPv4-only - enter an IPv4 address.")
    if network.prefixlen == 0:
        raise BadRequest("To let everyone in, use 'Open to anyone' instead.")
    if network.prefixlen < 24 and not allow_wide:
        raise BadRequest("Only admins can allow ranges wider than /24.")
    if network.prefixlen == 32:
        return str(network.network_address)
    return str(network)


class AccessManager:
    def __init__(self, unifi: UnifiClient, store: Store, events: Events, opts: Options):
        self.unifi = unifi
        self.store = store
        self.events = events
        self.opts = opts
        self._retry_at: dict[str, float] = {}

    # ------------------------------------------------------------ helpers

    def _holders(self, group_id: str, ip: str, exclude: dict | None = None) -> list[dict]:
        """Grants (on any rule sharing this IP list) that keep `ip` in it."""
        return [
            g
            for g in self.store.grants
            if g is not exclude
            and g["ip"] == ip
            and (self.store.rules.get(g["rule_id"]) or {}).get("group_id") == group_id
        ]

    def _managed(self, rule_id: str, user: User) -> dict:
        managed = self.store.rules.get(rule_id)
        if not managed or not (managed.get("visible") or user.is_admin):
            raise NotFound("That port forward isn't available in this app.")
        return managed

    def _check_hours(self, hours) -> int:
        if hours not in self.opts.grant_durations:
            raise BadRequest(f"Choose one of: {', '.join(hours_label(h) for h in self.opts.grant_durations)}.")
        return hours

    @staticmethod
    def _restrict(rule: dict, group_id: str) -> None:
        rule.update(
            src_limiting_enabled=True,
            src_limiting_type="firewall_group",
            src_firewall_group_id=group_id,
            src="any",
        )

    async def _provision(self) -> None:
        if not self.opts.force_provision:
            return
        try:
            count = await self.unifi.provision_gateways()
            _LOGGER.debug("Force-provisioned %s gateway(s)", count)
        except UnifiError as err:
            _LOGGER.warning("Force-provision failed (the change is saved and UniFi will push it on its own): %s", err)

    async def _remove_ip(self, group_id: str, ip: str) -> None:
        try:
            group = await self.unifi.firewall_group(group_id)
        except UnifiNotFound:
            _LOGGER.warning("IP list %s is gone from UniFi; nothing to remove %s from", group_id, ip)
            return
        members = group.get("group_members") or []
        remaining = [m for m in members if m != ip] or [PLACEHOLDER_IP]
        if remaining != members:
            group["group_members"] = remaining
            await self.unifi.update_firewall_group(group)

    async def _release(self, grant: dict) -> None:
        """Take a grant's IP back out of the list (call with write_lock held)."""
        managed = self.store.rules.get(grant["rule_id"])
        if managed and not grant.get("preexisting") and not self._holders(managed["group_id"], grant["ip"], exclude=grant):
            await self._remove_ip(managed["group_id"], grant["ip"])
        # Only forget the grant once UniFi has actually been updated.
        self.store.grants.remove(grant)

    # -------------------------------------------------------------- views

    async def overview(self, user: User) -> dict:
        rules = await self.unifi.port_forwards()
        groups = {}
        if user.is_admin:
            groups = {g["_id"]: g for g in await self.unifi.firewall_groups()}
        live_ids = set()
        out = []
        for rule in rules:
            live_ids.add(rule["_id"])
            managed = self.store.rules.get(rule["_id"])
            if not user.is_admin and not (managed and managed.get("visible")):
                continue
            out.append(self._rule_view(rule, managed, groups, user))
        if user.is_admin:
            for rule_id, managed in self.store.rules.items():
                if rule_id not in live_ids:
                    out.append({"id": rule_id, "name": managed.get("name", "?"), "managed": True, "status": "missing",
                                "visible": managed.get("visible", False), "grants": self._grants_view(rule_id, user)})
        return {"rules": out}

    def _grants_view(self, rule_id: str, user: User) -> list[dict]:
        return [
            {key: g.get(key) for key in ("id", "ip", "user_name", "note", "created_at", "expires_at")}
            | {"mine": g["user_id"] == user.id}
            for g in sorted(self.store.grants, key=lambda g: g["created_at"])
            if g["rule_id"] == rule_id and (user.is_admin or g["user_id"] == user.id)
        ]

    def _rule_view(self, rule: dict, managed: dict | None, groups: dict, user: User) -> dict:
        view = {
            "id": rule["_id"],
            "name": rule.get("name") or "Unnamed rule",
            "label": rule_label(rule),
            "proto": PROTO_LABELS.get(rule.get("proto", ""), rule.get("proto", "")),
            "dst_port": rule.get("dst_port"),
            "fwd": rule.get("fwd"),
            "fwd_port": rule.get("fwd_port"),
            "enabled": rule.get("enabled", True),
            "managed": managed is not None,
        }
        if managed is None:
            if rule.get("src_limiting_enabled"):
                view["source"] = "IP list" if rule.get("src_limiting_type") == "firewall_group" else rule.get("src") or "limited"
            else:
                view["source"] = "anyone"
            view["status"] = "unmanaged"
            return view

        restricted = (
            rule.get("src_limiting_enabled")
            and rule.get("src_limiting_type") == "firewall_group"
            and rule.get("src_firewall_group_id") == managed["group_id"]
        )
        if managed.get("open_until"):
            status = "open" if not rule.get("src_limiting_enabled") else "restricted"
        else:
            status = "restricted" if restricted else "drift"
        view.update(
            status=status,
            visible=managed.get("visible", False),
            open_until=managed.get("open_until"),
            open_by=managed.get("open_by"),
            grants=self._grants_view(rule["_id"], user),
        )
        if user.is_admin:
            group = groups.get(managed["group_id"]) or {}
            view["group"] = {
                "name": group.get("name", "(missing)"),
                "members": [m for m in group.get("group_members") or [] if m != PLACEHOLDER_IP],
            }
        return view

    # ----------------------------------------------------- user actions

    async def grant(self, user: User, rule_id: str, ip_text: str, hours, note: str) -> dict:
        user.require("port_grant")
        managed = self._managed(rule_id, user)
        if hours is None:
            user.require("port_permanent")
        else:
            self._check_hours(hours)
        ip = parse_source(ip_text, allow_wide=user.is_admin)
        note = (note or "").strip()[:NOTE_LIMIT]
        now = time.time()
        expires_at = now + hours * 3600 if hours else None

        async with self.unifi.write_lock:
            rule = await self.unifi.port_forward(rule_id)
            group = await self.unifi.firewall_group(managed["group_id"])
            members = list(group.get("group_members") or [])
            existing = next(
                (g for g in self.store.grants if g["rule_id"] == rule_id and g["ip"] == ip and g["user_id"] == user.id),
                None,
            )
            holders = self._holders(managed["group_id"], ip)
            preexisting = ip in members and (not holders or any(h.get("preexisting") for h in holders))
            # Update UniFi first so a failure there never leaves a grant on
            # record that isn't actually in effect.
            if ip not in members:
                group["group_members"] = [m for m in members if m != PLACEHOLDER_IP] + [ip]
                await self.unifi.update_firewall_group(group)
            if existing:
                existing.update(expires_at=expires_at, note=note or existing.get("note", ""))
                grant = existing
            else:
                grant = {
                    "id": secrets.token_hex(6),
                    "rule_id": rule_id,
                    "ip": ip,
                    "user_id": user.id,
                    "user_name": user.name,
                    "note": note,
                    "created_at": now,
                    "expires_at": expires_at,
                    "preexisting": preexisting,
                }
                self.store.grants.append(grant)
            self.store.save()
        await self._provision()

        until = f"until {when(expires_at)} ({hours_label(hours)})" if expires_at else "with no expiry"
        verb = "renewed access for" if existing else "granted"
        summary = f"{user.name} {verb} {ip} to {rule_label(rule)} {until}."
        if note:
            summary += f" Note: {note}"
        await self.events.record("grant", summary, title="🔓 Port access granted", actor=user,
                                 rule=rule_label(rule), ip=ip, expires_at=expires_at)
        return grant

    async def revoke(self, user: User, grant_id: str) -> None:
        grant = next((g for g in self.store.grants if g["id"] == grant_id), None)
        if grant is None:
            raise NotFound("That access has already ended.")
        if not user.is_admin and grant["user_id"] != user.id:
            raise NotFound("That access has already ended.")
        name = (self.store.rules.get(grant["rule_id"]) or {}).get("name", "?")
        async with self.unifi.write_lock:
            await self._release(grant)
            self.store.save()
        await self._provision()
        granted_by = "" if grant["user_id"] == user.id else f" (granted by {grant['user_name']})"
        await self.events.record("revoke", f"{user.name} revoked {grant['ip']} from {name}{granted_by}.",
                                 title="🔒 Port access revoked", actor=user, rule=name, ip=grant["ip"])

    async def open_any(self, user: User, rule_id: str, hours) -> None:
        user.require("port_open_any")
        managed = self._managed(rule_id, user)
        self._check_hours(hours)  # always time-limited, admins included
        until = time.time() + hours * 3600
        async with self.unifi.write_lock:
            rule = await self.unifi.port_forward(rule_id)
            if rule.get("src_limiting_enabled"):
                rule["src_limiting_enabled"] = False
                await self.unifi.update_port_forward(rule)
            managed.update(open_until=until, open_by=user.name, open_by_id=user.id)
            self.store.save()
        await self._provision()
        await self.events.record(
            "open",
            f"{user.name} opened {rule_label(rule)} to ANY IP until {when(until)} ({hours_label(hours)}).",
            title="⚠️ Port opened to anyone", actor=user, rule=rule_label(rule), expires_at=until,
        )

    async def close_any(self, user: User | None, rule_id: str) -> None:
        """Put the IP-list restriction back. user=None means the window expired."""
        if user is not None:
            user.require("port_open_any")
            managed = self._managed(rule_id, user)
        else:
            managed = self.store.rules[rule_id]
        opened_by = managed.get("open_by") or "someone"
        label = managed.get("name", "?")
        async with self.unifi.write_lock:
            try:
                rule = await self.unifi.port_forward(rule_id)
                label = rule_label(rule)
                self._restrict(rule, managed["group_id"])
                await self.unifi.update_port_forward(rule)
            except UnifiNotFound:
                _LOGGER.warning("Port forward %s was deleted in UniFi; just ending the open window", rule_id)
            managed.update(open_until=None, open_by=None, open_by_id=None)
            self.store.save()
        await self._provision()
        if user is None:
            summary = f"{label} is restricted to its IP list again (the open window started by {opened_by} ended)."
        else:
            summary = f"{user.name} closed {label} - only IPs on its list can connect again."
        await self.events.record("close", summary, title="🔒 Port closed again", actor=user, rule=label)

    # ---------------------------------------------------- admin actions

    async def manage(self, user: User, rule_id: str) -> None:
        """Limit a port forward to an IP list this app controls (also repairs drift)."""
        user.require_admin()
        current = self.store.rules.get(rule_id)
        async with self.unifi.write_lock:
            rule = await self.unifi.port_forward(rule_id)
            groups = {g["_id"]: g for g in await self.unifi.firewall_groups()}
            address_groups = {gid: g for gid, g in groups.items() if g.get("group_type") == "address-group"}

            group_id = None
            if current and current["group_id"] in address_groups:
                group_id = current["group_id"]
            elif (
                rule.get("src_limiting_enabled")
                and rule.get("src_limiting_type") == "firewall_group"
                and rule.get("src_firewall_group_id") in address_groups
            ):
                group_id = rule["src_firewall_group_id"]  # adopt the IP list it already uses
            else:
                name = (GROUP_PREFIX + (rule.get("name") or rule["_id"]))[:60]
                existing = next((gid for gid, g in address_groups.items() if g.get("name") == name), None)
                if existing:
                    group_id = existing
                else:
                    members = []
                    src = rule.get("src")
                    if rule.get("src_limiting_enabled") and rule.get("src_limiting_type", "ip") == "ip" and src and src != "any":
                        try:
                            members.append(parse_source(src, allow_wide=True))
                        except BadRequest:
                            _LOGGER.warning("Couldn't carry over the existing source '%s' of %s", src, rule_label(rule))
                    group = await self.unifi.create_address_group(name, members or [PLACEHOLDER_IP])
                    group_id = group["_id"]

            self._restrict(rule, group_id)
            await self.unifi.update_port_forward(rule)
            self.store.rules[rule_id] = {
                "group_id": group_id,
                "name": rule.get("name") or "Unnamed rule",
                "visible": (current or {}).get("visible", False),
                "open_until": None,
                "open_by": None,
                "open_by_id": None,
            }
            self.store.save()
        await self._provision()
        verb = "re-applied the IP-list restriction on" if current else "started managing"
        await self.events.record("manage", f"{user.name} {verb} {rule_label(rule)}.", actor=user, rule=rule_label(rule))

    async def unmanage(self, user: User, rule_id: str) -> None:
        """Stop managing: end this app's grants, re-close any open window, leave the IP list attached."""
        user.require_admin()
        managed = self.store.rules.get(rule_id)
        if managed is None:
            raise NotFound("That port forward isn't managed by this app.")
        async with self.unifi.write_lock:
            try:
                for grant in [g for g in self.store.grants if g["rule_id"] == rule_id]:
                    await self._release(grant)
                if managed.get("open_until"):
                    try:
                        rule = await self.unifi.port_forward(rule_id)
                        self._restrict(rule, managed["group_id"])
                        await self.unifi.update_port_forward(rule)
                    except UnifiNotFound:
                        pass
                del self.store.rules[rule_id]
            finally:
                self.store.save()  # keep whatever was already released, even on failure
        await self._provision()
        await self.events.record("unmanage", f"{user.name} stopped managing {managed.get('name', '?')}.", actor=user)

    def set_visible(self, user: User, rule_id: str, visible: bool) -> None:
        user.require_admin()
        managed = self.store.rules.get(rule_id)
        if managed is None:
            raise NotFound("Manage that port forward first.")
        managed["visible"] = bool(visible)
        self.store.save()

    # ------------------------------------------------------ background

    async def tick(self) -> None:
        """End whatever has expired. Runs every few seconds, and once at startup
        to catch anything that expired while the app was stopped."""
        now = time.time()
        changed = False

        for grant in [g for g in self.store.grants if g.get("expires_at") and g["expires_at"] <= now]:
            if self._retry_at.get(grant["id"], 0) > now:
                continue
            name = (self.store.rules.get(grant["rule_id"]) or {}).get("name", "?")
            try:
                async with self.unifi.write_lock:
                    await self._release(grant)
                    self.store.save()
            except UnifiError as err:
                await self._failed(grant["id"], f"Couldn't remove {grant['ip']} from {name}: {err}")
                continue
            changed = True
            self._retry_at.pop(grant["id"], None)
            late = " (the app was offline when it was due)" if now - grant["expires_at"] > 120 else ""
            await self.events.record(
                "expire",
                f"{grant['ip']} no longer has access to {name} - expired{late}. "
                f"It was granted by {grant['user_name']} at {when(grant['created_at'])}.",
                title="⏱️ Port access expired", rule=name, ip=grant["ip"],
            )

        for rule_id, managed in list(self.store.rules.items()):
            if not managed.get("open_until") or managed["open_until"] > now:
                continue
            if self._retry_at.get(rule_id, 0) > now:
                continue
            try:
                await self.close_any(None, rule_id)
            except UnifiError as err:
                name = managed.get("name", "?")
                await self._failed(rule_id, f"Couldn't close {name} again - it is STILL OPEN to anyone: {err}")
                continue
            self._retry_at.pop(rule_id, None)

        if changed:
            await self._provision()

    async def _failed(self, key: str, summary: str) -> None:
        first = key not in self._retry_at
        self._retry_at[key] = time.time() + RETRY_AFTER
        _LOGGER.error("%s", summary)
        if first:  # tell the admin once, then keep retrying quietly
            await self.events.record("error", summary + " Retrying every minute.", title="❗ UniFi Control needs attention")
