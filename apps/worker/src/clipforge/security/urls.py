"""Remote URL validation.

Two layers of protection:

1. Static validation (scheme, host shape, port) before any network call.
2. Address validation: every A/AAAA answer for the host must be a public unicast
   address. Redirects are followed manually, hop by hop, and each hop is
   revalidated before the next request is made.

This removes the obvious SSRF surface (localhost, RFC1918, link-local, CGNAT,
metadata endpoints). It does *not* defend against a DNS-rebinding attacker who
changes the answer between our lookup and the downloader's; for a loopback-only
local tool that residual risk is accepted and documented.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from clipforge.errors import ValidationFailed

ALLOWED_SCHEMES = {"http", "https"}
FORBIDDEN_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")
MAX_REDIRECTS = 5
RESOLVE_TIMEOUT_SECONDS = 6.0

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]

# Extractors that yt-dlp supports well and that serve ordinary public pages.
KNOWN_PROVIDERS = {
    "youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com",
    "vimeo.com", "player.vimeo.com",
    "dailymotion.com", "dai.ly",
    "twitch.tv", "clips.twitch.tv",
    "soundcloud.com",
    "facebook.com", "fb.watch",
    "ted.com",
    "archive.org",
    "bitchute.com", "rumble.com",
    "streamable.com",
}


@dataclass(frozen=True)
class ValidatedUrl:
    original: str
    resolved: str
    host: str
    is_known_provider: bool


def is_private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if address.is_private or address.is_loopback or address.is_link_local:
        return True
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return True
    return any(address in network for network in _PRIVATE_NETWORKS)


def _assert_host_allowed(host: str) -> None:
    lowered = host.lower().rstrip(".")
    if not lowered:
        raise ValidationFailed("The URL has no host", code="url_invalid")
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None and is_private_address(str(literal)):
        raise ValidationFailed("Private and local network addresses are blocked", code="url_blocked")
    if lowered == "localhost" or lowered.endswith(FORBIDDEN_HOST_SUFFIXES):
        raise ValidationFailed("That host is not reachable from this app", code="url_blocked")
    if lowered in {"metadata.google.internal", "metadata", "instance-data"}:
        raise ValidationFailed("That host is not reachable from this app", code="url_blocked")


def resolve_host_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationFailed("Could not resolve that host name", code="url_unresolvable") from exc
    addresses = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValidationFailed("Could not resolve that host name", code="url_unresolvable")
    return addresses


def validate_url_shape(url: str) -> ValidatedUrl:
    candidate = (url or "").strip()
    if not candidate:
        raise ValidationFailed("Enter a video URL", code="url_invalid")
    if len(candidate) > 2048:
        raise ValidationFailed("That URL is too long", code="url_invalid")
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValidationFailed("Only http and https URLs can be imported", code="url_scheme")
    if not parts.hostname:
        raise ValidationFailed("That URL has no host", code="url_invalid")
    if parts.username or parts.password:
        raise ValidationFailed("URLs with embedded credentials are not accepted", code="url_invalid")
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    if port not in {80, 443, 8080, 8443}:
        raise ValidationFailed("That port is not allowed for imports", code="url_port")
    _assert_host_allowed(parts.hostname)

    host = parts.hostname.lower()
    known = any(host == provider or host.endswith("." + provider) for provider in KNOWN_PROVIDERS)
    return ValidatedUrl(original=candidate, resolved=candidate, host=host, is_known_provider=known)


def _assert_public_addresses(host: str, port: int, *, allow_literal_private: bool = False) -> None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_private_address(str(literal)) and not allow_literal_private:
            raise ValidationFailed("Private and local network addresses are blocked", code="url_blocked")
        return
    for address in resolve_host_addresses(host, port):
        if is_private_address(address):
            raise ValidationFailed("That host resolves to a private network address", code="url_blocked")


def validate_and_resolve(url: str, *, timeout: float = RESOLVE_TIMEOUT_SECONDS) -> ValidatedUrl:
    """Validate a URL and follow redirects manually, revalidating each hop."""
    initial = validate_url_shape(url)
    parts = urlsplit(initial.resolved)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    _assert_public_addresses(host, port)

    current = initial.resolved
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                response = client.get(current, headers={"User-Agent": "Mozilla/5.0 (compatible; Clipforge/0.1)"})
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                from urllib.parse import urljoin

                nxt = urljoin(current, location)
                hop = validate_url_shape(nxt)
                hop_parts = urlsplit(hop.resolved)
                hop_port = hop_parts.port or (443 if hop_parts.scheme.lower() == "https" else 80)
                _assert_public_addresses(hop_parts.hostname or "", hop_port)
                current = hop.resolved
            else:
                raise ValidationFailed("That URL redirects too many times", code="url_redirect")
    except httpx.HTTPError:
        # A network failure here is not fatal: yt-dlp may still reach the host with
        # its own transport. We keep the statically validated URL.
        current = initial.resolved

    final_parts = urlsplit(current)
    final_host = (final_parts.hostname or "").lower()
    known = any(final_host == provider or final_host.endswith("." + provider) for provider in KNOWN_PROVIDERS)
    return ValidatedUrl(original=initial.original, resolved=current, host=final_host, is_known_provider=known)
