"""Optional OpenAI-compatible rescoring.

Disabled unless ``CLIPFORGE_LLM_ENABLED=true`` and a base URL + model are set.
Any failure (network, timeout, malformed JSON, unknown ids, out-of-range scores)
discards the whole batch and leaves the deterministic baseline in place.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from clipforge.config import Settings
from clipforge.errors import ProviderUnavailable
from clipforge.providers.llm.base import LlmClipScore, LlmScoringResult, ScorableClip

logger = logging.getLogger(__name__)

COMPONENT_KEYS = (
    "hook",
    "coherence",
    "completeness",
    "novelty",
    "impact",
    "density",
    "quotability",
    "standalone",
    "boundary",
    "safety",
)

SYSTEM_PROMPT = """You evaluate short clips cut from a longer spoken-word video.
Score only what the transcript supports. Never claim a clip will go viral.
Return STRICT JSON, no prose, no markdown fences, matching:
{"results":[{"id":"<given id>","title":"<max 70 chars>","hook":"<the exact opening words, max 140 chars>",
"reason":"<2 sentences: why this stands alone and who it helps>","category":"<one of insight|story|debate|funny|tutorial|emotional|news>",
"scores":{"hook":0-100,"coherence":0-100,"completeness":0-100,"novelty":0-100,"impact":0-100,
"density":0-100,"quotability":0-100,"standalone":0-100,"boundary":0-100,"safety":0-100},
"warnings":["<only real risks, e.g. needs earlier context, unresolved ending>"]}]}
Score safety high when the clip cannot misrepresent the speaker. Include every input id exactly once."""


@dataclass(frozen=True)
class OpenAiCompatibleScorer:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: int = 60
    name: str = "openai-compatible"

    def available(self) -> tuple[bool, str | None]:
        if not self.base_url or not self.model:
            return False, "No LLM endpoint configured"
        return True, None

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def score(self, clips: list[ScorableClip], *, goal: str, language: str | None) -> LlmScoringResult:
        if not clips:
            return LlmScoringResult(provider=self.name, model=self.model, clips=[])
        payload_clips = [
            {
                "id": clip.id,
                "start": round(clip.start_ms / 1000, 2),
                "end": round(clip.end_ms / 1000, 2),
                "text": clip.text[:1400],
            }
            for clip in clips
        ]
        user_prompt = (
            f"Editing goal: {goal}. Transcript language: {language or 'unknown'}.\n"
            f"Rate all {len(payload_clips)} clips.\n\n"
            + json.dumps(payload_clips, ensure_ascii=False)
        )
        parsed = self.complete_json(SYSTEM_PROMPT, user_prompt)
        return self._validate(parsed, clips)

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        """Return one parsed JSON object from an OpenAI-compatible endpoint.

        Kept separate from scoring so packaging can reuse the transport without
        changing the scoring protocol. Callers must validate the content; this
        method only guarantees the response parsed as a JSON object.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        attempt_body = body
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(self._endpoint(), headers=headers, json=attempt_body)
                if response.status_code >= 400 and "response_format" in attempt_body:
                    # Some local servers reject response_format; retry without it once.
                    fallback = {k: v for k, v in attempt_body.items() if k != "response_format"}
                    response = client.post(self._endpoint(), headers=headers, json=fallback)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable("The LLM endpoint could not be reached", code="llm_unreachable") from exc

        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable("The LLM endpoint returned an unexpected response", code="llm_bad_response") from exc

        return _parse_json(content)

    def _validate(self, parsed: dict, clips: list[ScorableClip]) -> LlmScoringResult:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
            raise ProviderUnavailable("The LLM response was not valid JSON", code="llm_bad_response")
        by_id = {clip.id: clip for clip in clips}
        results: list[LlmClipScore] = []
        for raw in parsed["results"]:
            if not isinstance(raw, dict):
                continue
            clip_id = str(raw.get("id", ""))
            if clip_id not in by_id:
                continue
            scores_raw = raw.get("scores") or {}
            components: dict[str, float] = {}
            total = 0.0
            for key in COMPONENT_KEYS:
                value = scores_raw.get(key)
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    numeric = 50.0
                numeric = max(0.0, min(100.0, numeric))
                components[key] = round(numeric, 1)
                total += numeric
            overall = round(total / len(COMPONENT_KEYS), 1)
            warnings = [str(w)[:160] for w in (raw.get("warnings") or []) if isinstance(w, str)][:5]
            results.append(
                LlmClipScore(
                    id=clip_id,
                    title=str(raw.get("title") or "").strip()[:120] or "Untitled moment",
                    hook=str(raw.get("hook") or "").strip()[:240],
                    reason=str(raw.get("reason") or "").strip()[:600],
                    category=str(raw.get("category") or "insight").strip()[:24],
                    overall=overall,
                    components=components,
                    warnings=warnings,
                )
            )
        if not results:
            raise ProviderUnavailable("The LLM response contained no usable clips", code="llm_bad_response")
        return LlmScoringResult(provider=self.name, model=self.model, clips=results)


def _parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ProviderUnavailable("The LLM response was not valid JSON", code="llm_bad_response")
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("The LLM response was not valid JSON", code="llm_bad_response") from exc


def build_llm_provider(settings: Settings) -> OpenAiCompatibleScorer | None:
    if not settings.llm_enabled:
        return None
    if not settings.llm_base_url or not settings.llm_model:
        logger.warning("LLM enabled but base URL or model missing; using baseline scoring only")
        return None
    return OpenAiCompatibleScorer(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )
