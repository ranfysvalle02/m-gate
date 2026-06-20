"""Downstream egress allowlisting — the single source of truth for "may the
gateway open an outbound connection to this endpoint?".

Two gates consume this module:

- registration (`gateway/routers/admin/servers.py`): a friendly, fail-fast 422 when an
  operator/tenant saves a server whose endpoint is not permitted, and
- connect time (`services/egress_transport.py`): the authoritative,
  DNS-rebinding-proof gate that re-resolves and pins a validated IP on every
  connect.

Design goals:

- **Default-safe & backward compatible.** With no global *and* no tenant
  allowlist configured (and ``egress_default_deny`` off), enforcement is a no-op
  beyond the always-on SSRF denylist — existing deployments keep working.
- **Global is a ceiling, tenant narrows within it.** When both a global and a
  tenant allowlist are configured, an endpoint must satisfy *both* (set
  intersection). A configured list on its own governs alone.
- **One definition of "not publicly routable."** The SSRF denylist is imported
  from :mod:`services.server_guard` so the two gates can never diverge.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlparse

from config.settings import Settings
from services.server_guard import ip_is_disallowed

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class EgressNotAllowed(ValueError):
    """Raised when an endpoint is not permitted by the egress allowlist."""


def parse_allowlist(raw: str | Iterable[str] | None) -> list[str]:
    """Normalize a comma/space separated allowlist into a clean entry list.

    Hosts/patterns are lowercased (DNS is case-insensitive); CIDRs are left as
    written. Order is preserved and duplicates are dropped.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens: Iterable[str] = raw.replace(",", " ").split()
    else:
        tokens = []
        for item in raw:
            tokens.extend(str(item).replace(",", " ").split())
    seen: set[str] = set()
    entries: list[str] = []
    for token in tokens:
        entry = token.strip().lower()
        if not entry or entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries


def validate_entry(entry: str) -> str:
    """Validate a single allowlist entry, returning its normalized form.

    Accepts an exact host, a ``*.`` subdomain glob, an IP literal, or a CIDR.
    Raises :class:`EgressNotAllowed` for anything that cannot be interpreted as
    one of those, so a typo cannot silently widen (or void) a policy.
    """
    normalized = entry.strip().lower()
    if not normalized:
        raise EgressNotAllowed("Egress allowlist entries must not be empty.")
    if "/" in normalized:
        try:
            ipaddress.ip_network(normalized, strict=False)
        except ValueError as exc:
            raise EgressNotAllowed(f"Invalid CIDR egress entry '{entry}': {exc}") from exc
        return normalized
    # A bare IP literal is valid (treated as a /32 or /128 below).
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    # Otherwise it must look like a hostname or a "*." subdomain glob.
    candidate = normalized[2:] if normalized.startswith("*.") else normalized
    if not candidate or any(ch.isspace() for ch in candidate):
        raise EgressNotAllowed(f"Invalid host egress entry '{entry}'.")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.-_")
    if any(ch not in allowed for ch in candidate):
        raise EgressNotAllowed(f"Invalid host egress entry '{entry}'.")
    return normalized


def validate_entries(entries: Iterable[str]) -> list[str]:
    """Validate + normalize a collection of entries (drops duplicates)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        normalized = validate_entry(str(raw))
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


@dataclass
class _ParsedList:
    """A parsed allowlist split into host patterns and IP networks."""

    host_patterns: list[str] = field(default_factory=list)
    networks: list[IpNetwork] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.host_patterns and not self.networks

    def matches(self, host: str, ips: Iterable[IpAddress]) -> bool:
        host = (host or "").lower()
        for pattern in self.host_patterns:
            if "*" in pattern or "?" in pattern:
                if fnmatch(host, pattern):
                    return True
            elif host == pattern:
                return True
        if self.networks:
            for ip in ips:
                for network in self.networks:
                    if ip.version == network.version and ip in network:
                        return True
        return False


def _parse_list(entries: Iterable[str]) -> _ParsedList:
    host_patterns: list[str] = []
    networks: list[IpNetwork] = []
    for raw in entries:
        entry = str(raw).strip().lower()
        if not entry:
            continue
        if "/" in entry:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
                continue
            except ValueError:
                host_patterns.append(entry)
                continue
        try:
            ip = ipaddress.ip_address(entry)
            networks.append(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}", strict=False))
            continue
        except ValueError:
            host_patterns.append(entry)
    return _ParsedList(host_patterns=host_patterns, networks=networks)


@dataclass
class EgressRules:
    """Effective, parsed egress policy for one ``(tenant, global)`` pair."""

    enabled: bool
    default_deny: bool
    global_rule: _ParsedList
    tenant_rule: _ParsedList
    # When True the tenant gate is mandatory: an empty tenant allowlist denies
    # everything (effective set = tenant INTERSECT global, and ``∅ ∩ x = ∅``)
    # instead of inheriting the global ceiling. Used for sandbox code egress,
    # where every reachable host must be an explicit per-tenant grant.
    require_tenant_allowlist: bool = False

    @property
    def has_any_allowlist(self) -> bool:
        return not self.global_rule.is_empty or not self.tenant_rule.is_empty

    @property
    def is_active(self) -> bool:
        """Whether enforcement needs DNS resolution / IP checks at all.

        Inactive == no work beyond the caller's existing controls, preserving the
        legacy "publicly routable is fine" behavior for unconfigured deployments.
        """
        if not self.enabled:
            return False
        return self.default_deny or self.has_any_allowlist

    def evaluate(self, host: str, ips: Iterable[IpAddress]) -> None:
        """Raise :class:`EgressNotAllowed` if ``host``/``ips`` are not permitted.

        ``ips`` is the set of resolved addresses for ``host`` (may be empty when
        only host-pattern matching is relevant). Callers must have already
        screened ``ips`` against the SSRF denylist, but this also enforces it so
        the function is safe to call standalone.
        """
        if not self.is_active:
            return

        ip_list = list(ips)
        for ip in ip_list:
            if ip_is_disallowed(ip):
                raise EgressNotAllowed(
                    f"Endpoint host '{host}' resolves to disallowed address '{ip}'."
                )

        if self.require_tenant_allowlist and self.tenant_rule.is_empty:
            # Strict intersect: with no tenant grant the effective set is empty,
            # so deny rather than fall through to the global ceiling.
            raise EgressNotAllowed(
                f"Egress is deny-by-default for code tools and host '{host}' is not on the "
                "tenant egress allowlist. A tenant admin can add it."
            )

        if not self.has_any_allowlist:
            # default_deny with nothing allowed => deny everything.
            raise EgressNotAllowed(
                f"Egress is deny-by-default and host '{host}' is not on any allowlist."
            )

        if not self.global_rule.is_empty and not self.global_rule.matches(host, ip_list):
            raise EgressNotAllowed(
                f"Endpoint host '{host}' is not permitted by the global egress allowlist."
            )
        if not self.tenant_rule.is_empty and not self.tenant_rule.matches(host, ip_list):
            raise EgressNotAllowed(
                f"Endpoint host '{host}' is not permitted by the tenant egress allowlist."
            )


def build_rules(
    settings: Settings,
    *,
    tenant_allowlist: Iterable[str] | None = None,
    global_allowlist: str | Iterable[str] | None = None,
) -> EgressRules:
    """Assemble the effective :class:`EgressRules` from settings + a tenant list."""
    raw_global = settings.egress_global_allowlist if global_allowlist is None else global_allowlist
    global_entries = parse_allowlist(raw_global)
    tenant_entries = parse_allowlist(tenant_allowlist)
    return EgressRules(
        enabled=bool(settings.egress_allowlist_enabled),
        default_deny=bool(settings.egress_default_deny),
        global_rule=_parse_list(global_entries),
        tenant_rule=_parse_list(tenant_entries),
    )


def build_code_egress_rules(
    settings: Settings,
    *,
    tenant_allowlist: Iterable[str] | None = None,
    global_allowlist: str | Iterable[str] | None = None,
) -> EgressRules:
    """Egress rules for the sandbox ``context.http`` bridge: always fail-closed.

    Unlike :func:`build_rules` (which mirrors the operator's downstream-proxy
    toggle and can be a no-op when ``EGRESS_ALLOWLIST_ENABLED`` is off), code
    egress is **always active and deny-by-default**. The SSRF denylist and the
    allowlist intersection are therefore enforced on every sandbox HTTP call
    regardless of the deployment-wide egress toggle, so an unconfigured
    deployment cannot accidentally let tenant code reach arbitrary (or internal)
    hosts. An empty effective allowlist blocks everything.
    """
    raw_global = settings.egress_global_allowlist if global_allowlist is None else global_allowlist
    global_entries = parse_allowlist(raw_global)
    tenant_entries = parse_allowlist(tenant_allowlist)
    return EgressRules(
        enabled=True,
        default_deny=True,
        global_rule=_parse_list(global_entries),
        tenant_rule=_parse_list(tenant_entries),
        require_tenant_allowlist=True,
    )


def global_egress_ceiling(settings: Settings) -> list[str]:
    """The platform-wide egress ceiling (``EGRESS_GLOBAL_ALLOWLIST``), normalized."""
    return sorted(parse_allowlist(settings.egress_global_allowlist))


def effective_code_egress_hosts(
    tenant_allowlist: Iterable[str] | None, *, settings: Settings
) -> list[str]:
    """Best-effort effective host set for display: ``tenant ∩ global ceiling``.

    When no ceiling is configured the tenant grants stand alone. Uses normalized
    exact-entry intersection (the same approach as the pip policy summary); the
    runtime :class:`EgressRules` is authoritative and additionally honors glob /
    CIDR matching + SSRF screening.
    """
    tenant = sorted(set(parse_allowlist(tenant_allowlist)))
    ceiling = set(parse_allowlist(settings.egress_global_allowlist))
    if not ceiling:
        return tenant
    return [host for host in tenant if host in ceiling]


def _endpoint_host_port(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise EgressNotAllowed("Endpoints must use http or https.")
    if not parsed.hostname:
        raise EgressNotAllowed("Endpoint hostname is required.")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, port


def resolve_host(host: str, port: int) -> list[IpAddress]:
    """Resolve ``host`` to a de-duplicated list of IP addresses (blocking)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EgressNotAllowed(f"Unable to resolve endpoint host '{host}': {exc}") from exc
    seen: set[str] = set()
    ips: list[IpAddress] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if address in seen:
            continue
        seen.add(address)
        try:
            ips.append(ipaddress.ip_address(address))
        except ValueError as exc:
            raise EgressNotAllowed(
                f"Resolved address '{address}' for host '{host}' is invalid."
            ) from exc
    return ips


async def check_endpoint_allowed(
    endpoint: str,
    *,
    tenant_allowlist: Iterable[str] | None,
    global_allowlist: str | Iterable[str] | None = None,
    settings: Settings,
) -> None:
    """Registration-time gate: raise :class:`EgressNotAllowed` if not permitted.

    A no-op (no DNS lookup) when enforcement is inactive, so it adds nothing to
    the hot path of an unconfigured deployment.
    """
    rules = build_rules(
        settings,
        tenant_allowlist=tenant_allowlist,
        global_allowlist=global_allowlist,
    )
    if not rules.is_active:
        return
    host, port = _endpoint_host_port(endpoint)
    ips = await asyncio.to_thread(resolve_host, host, port)
    rules.evaluate(host, ips)
