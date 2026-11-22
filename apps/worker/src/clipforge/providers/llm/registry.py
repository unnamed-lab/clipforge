"""Provider-independent model registry.

No adapter is required for Clipforge to work. The registry answers three
questions without pretending: what is configured, whether it is local or
remote, and whether it is usable for a structured editorial decision.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

from clipforge.config import Settings
from clipforge.providers.llm.openai_compat import OpenAiCompatibleScorer

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434/v1"


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host in LOCAL_HOSTS:
        return True
    if host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ProviderSelection:
    adapter: OpenAiCompatibleScorer | None
    info: dict
    available: bool
    reason: str | None

    @property
    def usable(self) -> bool:
        return self.available and self.adapter is not None


class LlmProviderRegistry:
    """Resolve the configured provider without assuming one vendor."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._selection: ProviderSelection | None = None

    def _editorial_model(self) -> str:
        return (
            self.settings.llm_editorial_model
            or self.settings.llm_model
            or ""
        ).strip()

    def _build(self) -> ProviderSelection:
        provider = self.settings.resolved_llm_provider
        base_url = self.settings.llm_base_url.strip()
        model = self._editorial_model()
        if provider == "none":
            return ProviderSelection(
                None,
                self._info("none", None, None, "none", False),
                False,
                "No language model is configured. Deterministic editorial rules will be used.",
            )
        if provider == "ollama":
            base_url = base_url or OLLAMA_DEFAULT_URL
            if not base_url.rstrip("/").endswith("/v1"):
                base_url = base_url.rstrip("/") + "/v1"
        if provider not in {"ollama", "openai_compatible", "openai-compatible"}:
            return ProviderSelection(
                None,
                self._info(provider, base_url or None, model or None, "unknown", False),
                False,
                f"Unsupported provider '{provider}'.",
            )
        if not base_url or not model:
            return ProviderSelection(
                None,
                self._info(provider, base_url or None, model or None, "unknown", False),
                False,
                "The configured provider needs both a base URL and a model ID.",
            )
        privacy = "local" if is_loopback(base_url) else "remote"
        if privacy == "remote" and not self.settings.llm_allow_remote:
            return ProviderSelection(
                None,
                self._info(provider, base_url, model, privacy, True),
                False,
                "A remote provider is configured but remote inference is not allowed. "
                "Set CLIPFORGE_LLM_ALLOW_REMOTE=true only if sending transcript excerpts off this device is acceptable.",
            )
        adapter = OpenAiCompatibleScorer(
            base_url=base_url,
            model=model,
            api_key=self.settings.llm_api_key,
            timeout_seconds=self.settings.llm_timeout_seconds,
            name="ollama" if provider == "ollama" else "openai-compatible",
            provider_id="ollama" if provider == "ollama" else "openai_compatible",
            privacy=privacy,
        )
        return ProviderSelection(
            adapter,
            self._info(provider, base_url, model, privacy, True),
            True,
            None,
        )

    def _info(self, provider: str, base_url: str | None, model: str | None,
              privacy: str, configured: bool) -> dict:
        return {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "privacy": privacy,
            "configured": configured,
            "structured_output": provider != "none",
            "context_limit": None,
            "editorial_enabled": bool(self.settings.llm_editorial_enabled),
        }

    def select(self) -> ProviderSelection:
        if self._selection is None:
            self._selection = self._build()
        return self._selection

    def describe(self) -> list[dict]:
        selection = self.select()
        info = dict(selection.info)
        info["available"] = selection.available
        info["reason"] = selection.reason
        return [info]

    def health(self) -> dict:
        selection = self.select()
        if not selection.usable or selection.adapter is None:
            return {
                **selection.info,
                "available": False,
                "detail": selection.reason or "No provider is usable.",
            }
        ok, detail = selection.adapter.probe()
        return {**selection.info, "available": ok, "detail": detail}


def build_registry(settings: Settings) -> LlmProviderRegistry:
    return LlmProviderRegistry(settings)
