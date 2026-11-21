"""Targeted additional moment discovery.

Discovery reuses the transcript, source and brief of an existing analysis. It
never re-transcribes or re-downloads, and it never replaces a candidate the user
has already seen or edited.
"""

from clipforge.discovery.criteria import (
    DISCOVERY_MOMENT_TYPES,
    DiscoveryCriteria,
    build_discovery_brief,
    normalise_criteria,
)
from clipforge.discovery.service import run_discovery

__all__ = [
    "DISCOVERY_MOMENT_TYPES",
    "DiscoveryCriteria",
    "build_discovery_brief",
    "normalise_criteria",
    "run_discovery",
]
