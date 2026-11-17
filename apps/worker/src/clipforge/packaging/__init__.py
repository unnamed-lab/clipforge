"""Grounded clip metadata and optional trend research.

The packaging layer never invents facts. Every title, description, caption and
keyword is derived from the final clip transcript, the editorial timeline, the
selected genre and the client's own profile. Trend research is a separate,
provider-aware service; when a provider is not configured the UI is told that
manual research is required instead of being shown fabricated numbers.
"""

from clipforge.packaging.models import PackagingDocument

__all__ = ["PackagingDocument"]
