"""Central platform constraints.

Editorial duration is intentionally not the same thing as a platform maximum.
A 60-90 second Extended clip is a good piece of storytelling; TikTok's upload
limit is much longer. Keeping both in one place avoids the common bug where the
"target length" silently becomes a hard technical limit.

These values are configuration, not a promise that a platform will never change
its rules. Each entry records where it came from and when it was checked. Set
``CLIPFORGE_PLATFORM_LIMITS_JSON`` to a JSON object to override any field
without changing code; the merged result is returned by :func:`platform_config`.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

CONFIG_VERSION = "2026-11-17"

PLATFORM_LIMITS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "label": "TikTok",
        "aspects": ["9:16", "1:1"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 600_000,
        "recommended_max_ms": 180_000,
        "dimensions": {"9:16": (1080, 1920), "1:1": (1080, 1080)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.10, "bottom_pct": 0.80},
        "source": "https://support.tiktok.com/en/using-tiktok/creating-videos/video-length",
        "checked_at": CONFIG_VERSION,
    },
    "instagram_reels": {
        "label": "Instagram Reels",
        "aspects": ["9:16"],
        "min_duration_ms": 3_000,
        "max_duration_ms": 180_000,
        "recommended_max_ms": 90_000,
        "dimensions": {"9:16": (1080, 1920)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.12, "bottom_pct": 0.78},
        "source": "https://help.instagram.com/270447560766967",
        "checked_at": CONFIG_VERSION,
    },
    "youtube_shorts": {
        "label": "YouTube Shorts",
        "aspects": ["9:16", "1:1"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 180_000,
        "recommended_max_ms": 90_000,
        "dimensions": {"9:16": (1080, 1920), "1:1": (1080, 1080)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.10, "bottom_pct": 0.82},
        "source": "https://support.google.com/youtube/answer/10059070",
        "checked_at": CONFIG_VERSION,
    },
    "youtube": {
        "label": "YouTube",
        "aspects": ["16:9", "9:16", "1:1"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 900_000,
        "recommended_max_ms": 900_000,
        "dimensions": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.08, "bottom_pct": 0.88},
        "source": "https://support.google.com/youtube/answer/10059070",
        "checked_at": CONFIG_VERSION,
    },
    "linkedin": {
        "label": "LinkedIn",
        "aspects": ["1:1", "16:9", "9:16"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 900_000,
        "recommended_max_ms": 180_000,
        "dimensions": {"1:1": (1080, 1080), "16:9": (1920, 1080), "9:16": (1080, 1920)},
        "fps": [24, 25, 30],
        "caption_safe_area": {"top_pct": 0.08, "bottom_pct": 0.86},
        "source": "https://www.linkedin.com/help/linkedin/answer/a507508",
        "checked_at": CONFIG_VERSION,
    },
    "x": {
        "label": "X",
        "aspects": ["16:9", "1:1", "9:16"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 600_000,
        "recommended_max_ms": 140_000,
        "dimensions": {"16:9": (1920, 1080), "1:1": (1080, 1080), "9:16": (1080, 1920)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.08, "bottom_pct": 0.82},
        "source": "https://help.x.com/en/using-x/x-videos",
        "checked_at": CONFIG_VERSION,
    },
    "podcast": {
        "label": "Podcast / audio-first",
        "aspects": ["16:9", "1:1", "9:16"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 900_000,
        "recommended_max_ms": 900_000,
        "dimensions": {"16:9": (1920, 1080), "1:1": (1080, 1080), "9:16": (1080, 1920)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.08, "bottom_pct": 0.88},
        "source": "internal",
        "checked_at": CONFIG_VERSION,
    },
    "internal": {
        "label": "Internal review",
        "aspects": ["16:9", "9:16", "1:1"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 900_000,
        "recommended_max_ms": 900_000,
        "dimensions": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.05, "bottom_pct": 0.92},
        "source": "internal",
        "checked_at": CONFIG_VERSION,
    },
    "other": {
        "label": "Other",
        "aspects": ["16:9", "9:16", "1:1"],
        "min_duration_ms": 1_000,
        "max_duration_ms": 900_000,
        "recommended_max_ms": 900_000,
        "dimensions": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
        "fps": [24, 25, 30, 60],
        "caption_safe_area": {"top_pct": 0.05, "bottom_pct": 0.92},
        "source": "internal",
        "checked_at": CONFIG_VERSION,
    },
}


# Metadata limits are separate from video limits. They are also configuration:
# a platform can change a caption limit without changing how long a clip may be.
PACKAGING_PROFILES: dict[str, dict[str, Any]] = {
    "tiktok": {
        "label": "TikTok",
        "title_max": 100,
        "title_recommended": 60,
        "description_short_max": 150,
        "description_long_max": 2_200,
        "caption_max": 2_200,
        "caption_recommended": 150,
        "caption_hard_limit_unit": "UTF-16 code units",
        "hashtag_max": 30,
        "hashtag_recommended": 5,
        "hashtag_visible": 5,
        "keyword_max": 12,
        "thumbnail_text_max": 40,
        "sources": [
            "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
            "https://support.tiktok.com/en/using-tiktok/creating-videos",
        ],
        "checked_at": CONFIG_VERSION,
        "notes": "TikTok has no separate public video title in the direct-post API; the title here is for internal review and thumbnail copy.",
    },
    "youtube_shorts": {
        "label": "YouTube Shorts",
        "title_max": 100,
        "title_recommended": 70,
        "description_short_max": 200,
        "description_long_max": 5_000,
        "caption_max": 5_000,
        "caption_recommended": 200,
        "caption_hard_limit_unit": "characters",
        "hashtag_max": 60,
        "hashtag_recommended": 3,
        "hashtag_visible": 3,
        "keyword_max": 12,
        "thumbnail_text_max": 40,
        "sources": [
            "https://support.google.com/youtube/answer/57407",
            "https://support.google.com/youtube/answer/6390658",
            "https://developers.google.com/youtube/v3/docs/videos",
        ],
        "checked_at": CONFIG_VERSION,
        "notes": "Only the first three hashtags are shown above the title. Search keywords are not a verified ranking signal.",
    },
    "instagram_reels": {
        "label": "Instagram Reels",
        "title_max": 100,
        "title_recommended": 70,
        "description_short_max": 150,
        "description_long_max": 2_200,
        "caption_max": 2_200,
        "caption_recommended": 150,
        "caption_hard_limit_unit": "characters",
        "hashtag_max": 30,
        "hashtag_recommended": 5,
        "hashtag_visible": 5,
        "keyword_max": 10,
        "thumbnail_text_max": 40,
        "sources": [
            "https://developers.facebook.com/docs/instagram-platform/content-publishing",
            "https://help.instagram.com/1038071743007909",
        ],
        "checked_at": CONFIG_VERSION,
        "notes": "Reels use one caption. Hashtag limits are a product convention, not an official ranking guarantee.",
    },
    "cross_platform": {
        "label": "Cross-platform",
        "title_max": 100,
        "title_recommended": 60,
        "description_short_max": 150,
        "description_long_max": 2_200,
        "caption_max": 2_200,
        "caption_recommended": 150,
        "caption_hard_limit_unit": "characters",
        "hashtag_max": 30,
        "hashtag_recommended": 5,
        "hashtag_visible": 5,
        "keyword_max": 10,
        "thumbnail_text_max": 40,
        "sources": ["internal: conservative intersection of supported platforms"],
        "checked_at": CONFIG_VERSION,
        "notes": "Uses the strictest supported limit so one draft can be copied to any platform.",
    },
}


def _load_overrides() -> dict:
    raw = os.environ.get("CLIPFORGE_PLATFORM_LIMITS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_packaging_overrides() -> dict:
    raw = os.environ.get("CLIPFORGE_PACKAGING_PROFILES_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def platform_config() -> dict[str, dict[str, Any]]:
    """Return the merged, read-only-by-convention platform configuration."""
    merged = copy.deepcopy(PLATFORM_LIMITS)
    for key, value in _load_overrides().items():
        if isinstance(value, dict) and key in merged:
            merged[key].update(value)
    return merged


def platform_for(platform: str | None) -> dict[str, Any]:
    config = platform_config()
    return config.get(platform or "other") or config["other"]


def platform_summary() -> dict:
    return {
        "version": CONFIG_VERSION,
        "note": "Platform rules change. Values are central configuration and must be checked before publishing.",
        "platforms": platform_config(),
        "packaging": packaging_summary()["packaging"],
    }


def packaging_profiles() -> dict[str, dict[str, Any]]:
    """Return packaging metadata limits merged with environment overrides."""
    merged = copy.deepcopy(PACKAGING_PROFILES)
    for key, value in _load_packaging_overrides().items():
        if isinstance(value, dict) and key in merged:
            merged[key].update(value)
    return merged


def packaging_profile(platform: str | None) -> dict[str, Any]:
    config = packaging_profiles()
    return config.get(platform or "cross_platform") or config["cross_platform"]


def packaging_summary() -> dict:
    return {
        "version": CONFIG_VERSION,
        "note": "Metadata limits are configuration, not a promise that a platform will not change them.",
        "packaging": packaging_profiles(),
    }


def safe_area_for(platform: str | None, aspect: str = "9:16") -> dict:
    """Return a conservative caption band for a platform and aspect."""
    entry = platform_for(platform)
    guard = float(entry.get("caption_safe_area", {}).get("top_pct", 0.08))
    bottom = float(entry.get("caption_safe_area", {}).get("bottom_pct", 0.88))
    if aspect == "9:16":
        # Vertical platforms put UI over the lower edge; keep captions above it.
        bottom = min(bottom, 0.82)
    return {
        "top_pct": max(0.0, min(guard, 0.45)),
        "bottom_pct": max(0.50, min(bottom, 0.96)),
    }
