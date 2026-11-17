"""Editorial planning: grounded hooks, retention structure and safe pacing.

The modules here are deliberately independent of the renderer. They turn a
transcript plus a chosen candidate span into an explainable plan, and the render
spec stores that plan verbatim so a preview and a final export describe the same
edit.
"""

from clipforge.editorial.narrative import build_retention_plan
from clipforge.editorial.pacing import PACING_PROFILES, plan_timing, resolve_pacing

__all__ = ["PACING_PROFILES", "build_retention_plan", "plan_timing", "resolve_pacing"]
