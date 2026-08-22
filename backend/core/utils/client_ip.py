"""Single trusted resolution of the requesting client's IP address.

Rate limiting, login throttling, SMS quotas and audit records all key on the
caller's address, so a caller that can choose its own address can choose its own
quota bucket and audit trail. ``X-Forwarded-For`` is therefore honored only when
the immediate peer is a configured trusted proxy, and the client is the
right-most forwarded address that is not itself a trusted proxy.

``HTTP_TRUSTED_PROXIES`` accepts a comma-separated list of IPs or CIDRs and
defaults to loopback, matching the reverse proxy running on the same host. The
same value is handed to Uvicorn's ``forwarded_allow_ips`` so the transport layer
and the application agree on who may rewrite the client address.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any

UNKNOWN_CLIENT_IP = "unknown"

_DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128"

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _configured_trusted_proxies() -> str:
    raw = (os.getenv("HTTP_TRUSTED_PROXIES", "") or "").strip()
    return raw or _DEFAULT_TRUSTED_PROXIES


def forwarded_allow_ips() -> str:
    """Return the Uvicorn ``forwarded_allow_ips`` value for this deployment."""

    return _configured_trusted_proxies()


def trusted_proxy_networks() -> tuple[_Network, ...]:
    networks: list[_Network] = []
    for part in _configured_trusted_proxies().replace(";", ",").split(","):
        candidate = part.strip()
        if not candidate:
            continue
        if candidate == "*":
            # An explicit wildcard means every peer may rewrite the client
            # address; represent it as the two full address spaces.
            return (
                ipaddress.ip_network("0.0.0.0/0"),
                ipaddress.ip_network("::/0"),
            )
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _as_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    # Strip an IPv6 zone id and the brackets some proxies keep in the header.
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    candidate = candidate.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def is_trusted_proxy(host: str) -> bool:
    address = _as_address(host)
    if address is None:
        return False
    return any(address in network for network in trusted_proxy_networks())


def resolve_client_ip(request: Any) -> str:
    """Return the caller's address, trusting proxy headers only when configured."""

    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "").strip()
    if not peer:
        return UNKNOWN_CLIENT_IP
    if not is_trusted_proxy(peer):
        return peer

    headers = getattr(request, "headers", None)
    forwarded = ""
    if headers is not None:
        forwarded = str(headers.get("x-forwarded-for") or "").strip()
    if not forwarded:
        return peer

    # Right-most entry is the address seen by the closest proxy; walk left past
    # our own proxies so a client-supplied prefix cannot impersonate anything.
    for candidate in reversed(forwarded.split(",")):
        address = _as_address(candidate)
        if address is None:
            continue
        if is_trusted_proxy(str(address)):
            continue
        return str(address)
    return peer


__all__ = [
    "UNKNOWN_CLIENT_IP",
    "forwarded_allow_ips",
    "is_trusted_proxy",
    "resolve_client_ip",
    "trusted_proxy_networks",
]
