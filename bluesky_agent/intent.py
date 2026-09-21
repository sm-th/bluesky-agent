"""Fail-open routing of research turns into typed presentation modes."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .config import Config
from .models import Turn


class ResearchMode(str, Enum):
    """The supported shapes of a research response."""

    SOURCE_BRIEF = "SOURCE_BRIEF"
    QUESTION_ANSWER = "QUESTION_ANSWER"
    NOTE_EXPLORE = "NOTE_EXPLORE"


_ALL_MODES = tuple(ResearchMode)
_ONE_HOT = {
    mode: MappingProxyType({candidate: float(candidate is mode) for candidate in _ALL_MODES})
    for mode in _ALL_MODES
}
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_QUESTION_RE = re.compile(
    r"(?:\?|^\s*(?:who|what|when|where|why|how|which|is|are|am|was|were|do|does|did|can|could|would|should|will|has|have|had)\b)",
    re.IGNORECASE,
)
_SOURCE_DIRECTIVE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:read|summari[sz]e|review|assess|analy[sz]e|extract|brief|digest)\b",
    re.IGNORECASE,
)
_DIRECTIVE_QUESTION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:read|summari[sz]e|review|assess|analy[sz]e|extract|brief|digest)\b[^?\n]*\b(?:why|how|whether|what|who|when|where|which)\b",
    re.IGNORECASE,
)
_GATEWAY_DIAGNOSTIC_RE = re.compile(
    r"\bM101\b|no (?:model )?provider|provider (?:is )?(?:not )?connected|gateway diagnostic",
    re.IGNORECASE,
)
_MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class RoutingDecision:
    """An immutable mode selection and the evidence needed to audit it."""

    mode: ResearchMode
    confidence: float
    probabilities: Mapping[ResearchMode, float]
    source: str
    model: str | None = None
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        probabilities = dict(self.probabilities)
        if set(probabilities) != set(_ALL_MODES):
            raise ValueError("probabilities must contain every research mode")
        if not _is_probability(self.confidence):
            raise ValueError("confidence must be between zero and one")
        for probability in probabilities.values():
            if not _is_probability(probability):
                raise ValueError("probabilities must be between zero and one")
        object.__setattr__(self, "probabilities", MappingProxyType(probabilities))

    @property
    def provider(self) -> str:
        """Compatibility name for the recorded decision source."""

        return self.source


@dataclass(frozen=True)
class _StructuralFacts:
    has_url: bool
    explicit_question: bool
    source_directive: bool
    url_only: bool
    is_reply: bool


_ModelRequest = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class IntentRouter:
    """Apply authoritative structural rules before optional Manifest routing."""

    __slots__ = ("_config", "_request", "_unavailable_reason")

    def __init__(
        self,
        config: Config,
        request: _ModelRequest | None,
        unavailable_reason: str | None = None,
    ) -> None:
        self._config = config
        self._request = request
        self._unavailable_reason = unavailable_reason

    def __repr__(self) -> str:
        state = "configured" if self._request is not None else "unavailable"
        return f"IntentRouter(model={self._config.intent_model!r}, gateway={state})"

    @classmethod
    def from_environment(
        cls,
        config: Config,
        environ: Mapping[str, str] | None = None,
    ) -> IntentRouter:
        """Create a router from Manifest's OpenAI-compatible environment."""

        env = os.environ if environ is None else environ
        base_url = env.get("OPENAI_BASE_URL", "").strip()
        api_key = env.get("OPENAI_API_KEY", "").strip()
        if not base_url or not api_key:
            return cls(config, None, "credentials_absent")

        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            return cls(config, None, "invalid_gateway_url")
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        timeout = config.intent_timeout

        def request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            http_request = urllib.request.Request(
                endpoint,
                data=encoded,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "bluesky-research-agent/1",
                },
            )
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError("intent gateway returned a non-success status")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("intent gateway response was too large")
            decoded = json.loads(body.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("intent gateway response was not an object")
            return decoded

        return cls(config, request)

    def route(
        self,
        turn: Turn,
        parent_mode: ResearchMode | None = None,
    ) -> RoutingDecision:
        """Select a mode without ever allowing model failure to block research."""

        facts = _structural_facts(turn.text, turn.parent_turn_uri is not None)
        if facts.explicit_question:
            return _deterministic(ResearchMode.QUESTION_ANSWER, "structural")
        if facts.url_only or facts.source_directive:
            return _deterministic(ResearchMode.SOURCE_BRIEF, "structural")
        inherited = _coerce_mode(parent_mode)
        if facts.is_reply and inherited is not None and not facts.explicit_question and not facts.source_directive:
            return _deterministic(inherited, "inherited")

        if self._request is None:
            return _fallback(self._config.intent_model, self._unavailable_reason or "gateway_unavailable")

        payload = _model_payload(turn.text, facts, self._config.intent_model)
        try:
            response = self._request(payload)
            return self._parse_response(response)
        except Exception:
            return _fallback(self._config.intent_model, "gateway_failure")

    def _parse_response(self, response: Mapping[str, Any]) -> RoutingDecision:
        if _contains_gateway_diagnostic(response):
            return _fallback(self._config.intent_model, "gateway_diagnostic")
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("response must contain exactly one choice")
            choice_record = choices[0]
            if not isinstance(choice_record, dict):
                raise ValueError("choice must be an object")
            message = choice_record["message"]
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise ValueError("message content must be a string")
            content = message["content"]
            if _GATEWAY_DIAGNOSTIC_RE.search(content):
                return _fallback(self._config.intent_model, "gateway_diagnostic")
            answer = json.loads(content)
            mode, confidence, probabilities = _validated_answer(answer)
            response_model = response.get("model", self._config.intent_model)
            if not isinstance(response_model, str) or not response_model.strip():
                raise ValueError("model must be a non-empty string")
            if "jev" not in response_model.casefold():
                return _fallback(response_model, "unexpected_model")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _fallback(self._config.intent_model, "malformed_response")

        if confidence < self._config.intent_confidence_threshold:
            return RoutingDecision(
                mode=ResearchMode.NOTE_EXPLORE,
                confidence=confidence,
                probabilities=probabilities,
                source="fallback",
                model=response_model,
                fallback_reason="low_confidence",
            )
        return RoutingDecision(
            mode=mode,
            confidence=confidence,
            probabilities=probabilities,
            source="manifest",
            model=response_model,
        )


def _structural_facts(text: str, is_reply: bool) -> _StructuralFacts:
    urls = _URL_RE.findall(text)
    without_urls = _URL_RE.sub("", text)
    explicit_question = bool(_QUESTION_RE.search(without_urls) or _DIRECTIVE_QUESTION_RE.search(without_urls))
    source_directive = bool(_SOURCE_DIRECTIVE_RE.search(without_urls))
    remainder = re.sub(r"[\s\.,:;!?'\"`()\[\]{}<>—–-]+", "", without_urls)
    return _StructuralFacts(
        has_url=bool(urls),
        explicit_question=explicit_question,
        source_directive=source_directive,
        url_only=bool(urls) and not remainder,
        is_reply=is_reply,
    )


def _model_payload(text: str, facts: _StructuralFacts, model: str) -> dict[str, Any]:
    labels = [mode.value for mode in _ALL_MODES]
    schema = {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": labels},
            "probabilities": {
                "type": "object",
                "properties": {label: {"type": "number", "minimum": 0, "maximum": 1} for label in labels},
                "required": labels,
                "additionalProperties": False,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["choice", "probabilities", "confidence"],
        "additionalProperties": False,
    }
    facts_payload = {
        "text": text,
        "has_url": facts.has_url,
        "explicit_question": facts.explicit_question,
        "source_directive": facts.source_directive,
        "is_reply": facts.is_reply,
    }
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Classify one research turn. SOURCE_BRIEF reads or assesses supplied sources; "
                    "QUESTION_ANSWER answers a substantive question; NOTE_EXPLORE investigates or "
                    "connects a note, claim, or topic. Return only the requested JSON."
                ),
            },
            {"role": "user", "content": json.dumps(facts_payload, ensure_ascii=False, separators=(",", ":"))},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "research_intent", "strict": True, "schema": schema},
        },
    }


def _validated_answer(answer: Any) -> tuple[ResearchMode, float, Mapping[ResearchMode, float]]:
    if not isinstance(answer, dict) or set(answer) != {"choice", "probabilities", "confidence"}:
        raise ValueError("answer has an invalid shape")
    mode = ResearchMode(answer["choice"])
    confidence = answer["confidence"]
    raw_probabilities = answer["probabilities"]
    if not _is_probability(confidence) or not isinstance(raw_probabilities, dict):
        raise ValueError("answer has invalid confidence or probabilities")
    if set(raw_probabilities) != {candidate.value for candidate in _ALL_MODES}:
        raise ValueError("answer does not contain exactly the supported modes")
    probabilities: dict[ResearchMode, float] = {}
    for candidate in _ALL_MODES:
        probability = raw_probabilities[candidate.value]
        if not _is_probability(probability):
            raise ValueError("answer has an invalid probability")
        probabilities[candidate] = float(probability)
    if abs(sum(probabilities.values()) - 1.0) > 0.01:
        raise ValueError("probabilities do not sum to one")
    if probabilities[mode] != max(probabilities.values()):
        raise ValueError("selected mode does not have the highest probability")
    return mode, float(confidence), probabilities


def _is_probability(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _coerce_mode(value: ResearchMode | None) -> ResearchMode | None:
    if value is None:
        return None
    try:
        return ResearchMode(value)
    except (TypeError, ValueError):
        return None


def _deterministic(mode: ResearchMode, source: str) -> RoutingDecision:
    return RoutingDecision(mode=mode, confidence=1.0, probabilities=_ONE_HOT[mode], source=source)


def _fallback(model: str, reason: str) -> RoutingDecision:
    mode = ResearchMode.NOTE_EXPLORE
    return RoutingDecision(
        mode=mode,
        confidence=1.0,
        probabilities=_ONE_HOT[mode],
        source="fallback",
        model=model,
        fallback_reason=reason,
    )


def _contains_gateway_diagnostic(response: Mapping[str, Any]) -> bool:
    try:
        rendered = json.dumps(response, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return bool(_GATEWAY_DIAGNOSTIC_RE.search(rendered))
