"""Discovery connectors.

Only official platform APIs are used. A connector that cannot run explains
exactly what is missing instead of returning invented results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from clipforge.config import Settings

TIMEOUT_SECONDS = 12


class ConnectorUnavailable(RuntimeError):
    """Raised when a connector cannot be used right now, with a human reason."""


@dataclass
class DiscoveredSource:
    platform: str
    connector: str
    url: str
    title: str
    creator: str | None = None
    external_id: str | None = None
    duration_ms: int | None = None
    thumbnail_url: str | None = None
    language: str | None = None
    description: str | None = None
    published_at: str | None = None
    metadata_status: str = "api"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "connector": self.connector,
            "url": self.url,
            "title": self.title,
            "creator": self.creator,
            "external_id": self.external_id,
            "duration_ms": self.duration_ms,
            "thumbnail_url": self.thumbnail_url,
            "language": self.language,
            "description": self.description,
            "published_at": self.published_at,
            "metadata_status": self.metadata_status,
            "metadata": self.metadata,
        }


class Connector(Protocol):
    name: str

    def status(self) -> dict: ...

    def search(self, query: str, limit: int) -> list[DiscoveredSource]: ...

    def check(self, url: str, external_id: str | None) -> tuple[str, str]: ...


def _get_json(url: str, *, headers: dict | None = None, data: bytes | None = None) -> dict:
    request = Request(url, headers={"User-Agent": "Clipforge/0.1", **(headers or {})}, data=data)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("message") or (body.get("error") or {}).get("message") or ""
        except Exception:  # noqa: BLE001 - the body is best-effort diagnostics
            detail = ""
        raise ConnectorUnavailable(f"The platform rejected the request ({exc.code}). {detail}".strip()) from exc
    except URLError as exc:
        raise ConnectorUnavailable(f"The platform could not be reached ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise ConnectorUnavailable("The platform did not answer in time.") from exc


ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_iso_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = ISO_DURATION.match(value)
    if not match:
        return None
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    total = timedelta(
        days=parts["days"], hours=parts["hours"], minutes=parts["minutes"], seconds=parts["seconds"]
    )
    return int(total.total_seconds() * 1000) or None


TWITCH_DURATION = re.compile(r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$")


def parse_twitch_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = TWITCH_DURATION.match(value)
    if not match:
        return None
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    seconds = parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return seconds * 1000 or None


class YouTubeConnector:
    name = "youtube"

    def __init__(self, settings: Settings) -> None:
        self.key = settings.youtube_api_key

    def status(self) -> dict:
        return {
            "platform": "youtube",
            "name": "YouTube Data API v3",
            "operational": bool(self.key),
            "requires_credentials": True,
            "credential_env": "CLIPFORGE_YOUTUBE_API_KEY",
            "detail": (
                "Official YouTube Data API is configured. Search, metadata and availability checks use it."
                if self.key
                else "Add a YouTube Data API key to enable search and metadata refresh. "
                "Manual URL entry and local uploads still work."
            ),
            "can_import_media": False,
            "note": "The Data API does not provide media files. Import media you are authorised to use, "
                    "or upload a local copy.",
        }

    def _require(self) -> None:
        if not self.key:
            raise ConnectorUnavailable(
                "YouTube search needs an API key. Set CLIPFORGE_YOUTUBE_API_KEY and restart the worker. "
                "Manual URL entry and local uploads still work."
            )

    def search(self, query: str, limit: int) -> list[DiscoveredSource]:
        self._require()
        params = urlencode({
            "part": "snippet", "type": "video", "maxResults": min(25, limit), "q": query, "key": self.key,
        })
        payload = _get_json(f"https://www.googleapis.com/youtube/v3/search?{params}")
        ids = [item["id"]["videoId"] for item in payload.get("items", []) if item.get("id", {}).get("videoId")]
        if not ids:
            return []
        details = _get_json(
            "https://www.googleapis.com/youtube/v3/videos?"
            + urlencode({"part": "snippet,contentDetails", "id": ",".join(ids), "key": self.key})
        )
        found: list[DiscoveredSource] = []
        for item in details.get("items", []):
            snippet = item.get("snippet", {})
            found.append(
                DiscoveredSource(
                    platform="youtube",
                    connector="youtube",
                    url=f"https://www.youtube.com/watch?v={item['id']}",
                    title=snippet.get("title") or "(untitled)",
                    creator=snippet.get("channelTitle"),
                    external_id=item["id"],
                    duration_ms=parse_iso_duration(item.get("contentDetails", {}).get("duration")),
                    thumbnail_url=(snippet.get("thumbnails", {}).get("medium") or {}).get("url"),
                    language=snippet.get("defaultAudioLanguage"),
                    description=(snippet.get("description") or "")[:2000] or None,
                    published_at=snippet.get("publishedAt"),
                )
            )
        return found

    def check(self, url: str, external_id: str | None) -> tuple[str, str]:
        self._require()
        video_id = external_id or _youtube_id(url)
        if not video_id:
            return "unknown", "This link does not contain a YouTube video id."
        payload = _get_json(
            "https://www.googleapis.com/youtube/v3/videos?"
            + urlencode({"part": "snippet,status", "id": video_id, "key": self.key})
        )
        if not payload.get("items"):
            return "unavailable", "The video is private, deleted or otherwise unavailable from this region."
        status = payload["items"][0].get("status", {})
        if status.get("privacyStatus") != "public":
            return "unavailable", f"The video is {status.get('privacyStatus', 'not public')}."
        if status.get("uploadStatus") not in {None, "processed"}:
            return "unavailable", f"The upload status is {status.get('uploadStatus')}."
        return "available", "The platform reports this video as public."


class TwitchConnector:
    name = "twitch"

    def __init__(self, settings: Settings) -> None:
        self.client_id = settings.twitch_client_id
        self.client_secret = settings.twitch_client_secret
        self._token: str | None = None
        self._token_expires: datetime | None = None

    def status(self) -> dict:
        configured = bool(self.client_id and self.client_secret)
        return {
            "platform": "twitch",
            "name": "Twitch Helix API",
            "operational": configured,
            "requires_credentials": True,
            "credential_env": "CLIPFORGE_TWITCH_CLIENT_ID and CLIPFORGE_TWITCH_CLIENT_SECRET",
            "detail": (
                "Official Twitch Helix API is configured. Channel search and VOD availability use it."
                if configured
                else "Add Twitch client credentials to enable channel and VOD search. Manual URL entry still works."
            ),
            "can_import_media": False,
            "note": "Twitch media must be downloaded through an authorised export or uploaded locally.",
        }

    def _require(self) -> None:
        if not (self.client_id and self.client_secret):
            raise ConnectorUnavailable(
                "Twitch search needs client credentials. Set CLIPFORGE_TWITCH_CLIENT_ID and "
                "CLIPFORGE_TWITCH_CLIENT_SECRET, then restart the worker."
            )

    def _auth_headers(self) -> dict:
        if self._token and self._token_expires and datetime.utcnow() < self._token_expires:
            return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}
        params = urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        })
        payload = _get_json(f"https://id.twitch.tv/oauth2/token?{params}", data=b"")
        self._token = payload["access_token"]
        self._token_expires = datetime.utcnow() + timedelta(seconds=int(payload.get("expires_in", 3600)) - 60)
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    def search(self, query: str, limit: int) -> list[DiscoveredSource]:
        self._require()
        headers = self._auth_headers()
        payload = _get_json(
            "https://api.twitch.tv/helix/search/channels?"
            + urlencode({"query": query, "first": min(10, limit)}),
            headers=headers,
        )
        found: list[DiscoveredSource] = []
        for channel in payload.get("data", [])[:3]:
            videos = _get_json(
                "https://api.twitch.tv/helix/videos?"
                + urlencode({"user_id": channel["id"], "first": 5, "type": "archive"}),
                headers=headers,
            )
            for video in videos.get("data", []):
                found.append(
                    DiscoveredSource(
                        platform="twitch",
                        connector="twitch",
                        url=video.get("url") or f"https://www.twitch.tv/{channel['broadcaster_login']}",
                        title=video.get("title") or channel.get("display_name") or "(untitled)",
                        creator=channel.get("display_name"),
                        external_id=video.get("id"),
                        duration_ms=parse_twitch_duration(video.get("duration")),
                        thumbnail_url=(video.get("thumbnail_url") or "")
                        .replace("%{width}", "640").replace("%{height}", "360") or None,
                        language=channel.get("broadcaster_language"),
                        description=(video.get("description") or "")[:2000] or None,
                        published_at=video.get("published_at"),
                        metadata={"channel": channel.get("broadcaster_login")},
                    )
                )
            if not videos.get("data"):
                found.append(
                    DiscoveredSource(
                        platform="twitch",
                        connector="twitch",
                        url=f"https://www.twitch.tv/{channel['broadcaster_login']}",
                        title=channel.get("display_name") or channel["broadcaster_login"],
                        creator=channel.get("display_name"),
                        external_id=channel.get("id"),
                        thumbnail_url=channel.get("thumbnail_url"),
                        language=channel.get("broadcaster_language"),
                        description="Channel with no public VODs returned by the API.",
                        metadata_status="api",
                    )
                )
        return found[:limit]

    def check(self, url: str, external_id: str | None) -> tuple[str, str]:
        self._require()
        headers = self._auth_headers()
        video_id = external_id or _twitch_video_id(url)
        if not video_id:
            return "unknown", "This link does not identify a Twitch VOD."
        payload = _get_json(
            "https://api.twitch.tv/helix/videos?" + urlencode({"id": video_id}), headers=headers
        )
        if not payload.get("data"):
            return "unavailable", "The VOD is expired, deleted or private."
        return "available", "The platform reports this VOD as available."


def _youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/") or None
    query = dict(pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair)
    return query.get("v")


def _twitch_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.search(r"/videos/(\d+)", parsed.path)
    return match.group(1) if match else None


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "youtube"
    if host.endswith("twitch.tv"):
        return "twitch"
    return "other"


def youtube_oembed(url: str) -> dict | None:
    """Public oEmbed metadata, used when no API key is configured."""
    try:
        payload = _get_json(
            "https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"})
        )
    except ConnectorUnavailable:
        return None
    return {
        "title": payload.get("title"),
        "creator": payload.get("author_name"),
        "thumbnail_url": payload.get("thumbnail_url"),
    }


def build_connectors(settings: Settings) -> dict[str, Connector]:
    return {
        "youtube": YouTubeConnector(settings),
        "twitch": TwitchConnector(settings),
    }


def connector_report(settings: Settings) -> dict:
    connectors = build_connectors(settings)
    items = [connector.status() for connector in connectors.values()]
    items.append({
        "platform": "manual",
        "name": "Manual URL and local upload",
        "operational": True,
        "requires_credentials": False,
        "credential_env": None,
        "detail": "Paste a supported link for metadata, or upload a file you own. "
                  "Media import always requires a rights confirmation.",
        "can_import_media": False,
        "note": "Clipforge never downloads from a platform that does not offer an authorised export.",
    })
    return {
        "connectors": items,
        "operational": [item["platform"] for item in items if item["operational"]],
        "need_credentials": [item["platform"] for item in items if not item["operational"]],
    }


def search(settings: Settings, platform: str, query: str, limit: int) -> list[dict]:
    connectors = build_connectors(settings)
    connector = connectors.get(platform)
    if connector is None:
        raise ConnectorUnavailable(f"There is no '{platform}' connector. Available: {', '.join(connectors)}.")
    return [item.to_dict() for item in connector.search(query, limit)]


def check_source(settings: Settings, source: dict) -> tuple[str, str]:
    connectors = build_connectors(settings)
    connector = connectors.get(source.get("platform", ""))
    if connector is None:
        return "unknown", "This source has no connector that can check availability."
    return connector.check(source["url"], source.get("external_id"))


def escape(value: str) -> str:
    return quote(value, safe="")
