"""Network signals for the guard: which address the client really has, and privacy-safe keys for it.

The client IP comes from X-Forwarded-For counted from the right: each trusted proxy appends the address it
saw, so the entry TRUSTED_PROXY_HOPS from the right is the one our own first proxy recorded. Anything to
its left was sent by the client and can say anything.

Stored IPs are keyed HMACs (SESSION_SECRET), never plain sha256: a plain hash of an IPv4 address is undone
by hashing all 4 billion of them. The network key (IPv4 /24, IPv6 /48) groups one bot's devices: a bot
that rotates its device id on every request still comes from the same few networks.
"""

import hashlib
import hmac
import ipaddress

from starlette.requests import Request

from app.config import get_settings


def client_ip(forwarded_for: str | None, peer: str | None, hops: int) -> str | None:
    if hops > 0 and forwarded_for:
        entries = [e.strip() for e in forwarded_for.split(",") if e.strip()]
        if len(entries) >= hops:
            return _valid(entries[-hops])
    return _valid(peer)


def request_ip(request: Request) -> str | None:
    """client_ip() for an incoming request, with this deploy's TRUSTED_PROXY_HOPS."""
    return client_ip(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
        get_settings().trusted_proxy_hops,
    )


def _valid(ip: str | None) -> str | None:
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _keyed(kind: str, value: str) -> str:
    key = get_settings().session_secret.encode()
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:32]


def ip_hash(ip: str | None) -> str | None:
    return _keyed("ip", ip) if ip else None


def network(ip: str | None) -> str | None:
    """IPv4 /24 or IPv6 /48 (one home, office or small hosting block)."""
    if not ip:
        return None
    addr = ipaddress.ip_address(ip)
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def ip_net_hash(ip: str | None) -> str | None:
    net = network(ip)
    return _keyed("net", net) if net else None
