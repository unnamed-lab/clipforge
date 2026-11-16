"""Content discovery and rights-aware library helpers."""

from clipforge.library.connectors import (
    ConnectorUnavailable,
    build_connectors,
    check_source,
    connector_report,
    detect_platform,
    search,
    youtube_oembed,
)

__all__ = [
    "ConnectorUnavailable",
    "build_connectors",
    "check_source",
    "connector_report",
    "detect_platform",
    "search",
    "youtube_oembed",
]
