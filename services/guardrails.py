from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from config.settings import Settings, get_settings
from database.mongo import get_control_database
from database.seed import guardrail_signatures_seed
from services.embeddings import EmbeddingService, embedding_version_for, get_embedding_service

logger = logging.getLogger(__name__)

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine

    _PRESIDIO_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    AnalyzerEngine = None
    AnonymizerEngine = None
    _PRESIDIO_AVAILABLE = False

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"reveal\s+(the\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"developer\s+message", re.IGNORECASE),
    re.compile(r"<script", re.IGNORECASE),
]

EMAIL_PATTERN = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE_PATTERN = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?){2}\d{4}\b")
STRIPE_KEY_PATTERN = re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{16,}\b")
OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
AWS_ACCESS_KEY_PATTERN = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
GITHUB_TOKEN_PATTERN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
GUARDRAIL_SIGNATURE_INDEX_PREFIX = "guardrail-signatures-v"
GUARDRAIL_SIGNATURE_VECTOR_PATH = "embedding"
GUARDRAIL_SIGNATURE_FILTER_FIELDS = ("embedding_version", "enabled")


class GuardrailUnavailableError(RuntimeError):
    pass


def guardrail_signature_lookup_filter(*, embedding_version: str) -> dict[str, Any]:
    return {
        "embedding_version": embedding_version,
        "enabled": True,
    }


def guardrail_signature_index_name(embedding_version: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", embedding_version.lower()).strip("-")
    if not slug:
        slug = "default"
    name = f"{GUARDRAIL_SIGNATURE_INDEX_PREFIX}-{slug}"
    if len(name) <= 120:
        return name
    digest = hashlib.sha1(embedding_version.encode("utf-8")).hexdigest()[:12]
    max_slug_len = max(8, 120 - len(GUARDRAIL_SIGNATURE_INDEX_PREFIX) - len(digest) - 2)
    return f"{GUARDRAIL_SIGNATURE_INDEX_PREFIX}-{slug[:max_slug_len]}-{digest}"


def guardrail_signature_index_spec(*, embedding_version: str, dimensions: int) -> dict[str, Any]:
    return {
        "name": guardrail_signature_index_name(embedding_version),
        "definition": {
            "fields": [
                {
                    "type": "vector",
                    "path": GUARDRAIL_SIGNATURE_VECTOR_PATH,
                    "numDimensions": dimensions,
                    "similarity": "cosine",
                },
                *({"type": "filter", "path": path} for path in GUARDRAIL_SIGNATURE_FILTER_FIELDS),
            ]
        },
    }


async def resync_guardrail_signatures(
    *,
    embedding_service: EmbeddingService | None = None,
    settings: Settings | None = None,
) -> int:
    """(Re-)embed the seed guardrail signature corpus with the active provider.

    Used by both the bootstrap script and the embedding-reprovision orchestrator
    so the control-plane guardrail vectors always match the active embedding
    model/version. Returns the number of signatures written.
    """
    settings = settings or get_settings()
    service = embedding_service or get_embedding_service(settings)
    version = embedding_version_for(service)
    collection = get_control_database()["guardrail_signatures"]
    signatures = guardrail_signatures_seed()
    for signature in signatures:
        text = str(signature["text"])
        embedding = await service.embed_text(text)
        now = datetime.now(UTC)
        await collection.update_one(
            {"_id": signature["_id"]},
            {
                "$set": {
                    **signature,
                    "embedding": embedding,
                    "embedding_version": version,
                    **guardrail_signature_lookup_filter(embedding_version=version),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    return len(signatures)


def _luhn_checksum_is_valid(card_number: str) -> bool:
    digits = [int(ch) for ch in card_number if ch.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for idx, digit in enumerate(digits):
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _redact_credit_cards(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        return "[REDACTED_CARD]" if _luhn_checksum_is_valid(candidate) else candidate

    return re.sub(r"\b(?:\d[ -]?){13,19}\b", _replace, text)


@dataclass
class GuardrailCheck:
    blocked: bool
    reasons: list[str]


class InboundDetector(Protocol):
    async def screen(self, text: str) -> list[str]: ...


class OutboundRedactor(Protocol):
    def redact(self, text: str) -> str: ...


class RegexInboundDetector:
    async def screen(self, text: str) -> list[str]:
        reasons: list[str] = []
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.search(text):
                reasons.append(f"blocked:{pattern.pattern}")
        return reasons


class SemanticInjectionDetector:
    def __init__(
        self,
        *,
        settings: Settings,
        embedding_service: EmbeddingService,
    ) -> None:
        self.settings = settings
        self.embedding_service = embedding_service
        self._circuit_open_until = 0.0
        self._consecutive_failures = 0

    @property
    def embedding_version(self) -> str:
        return embedding_version_for(self.embedding_service)

    @property
    def index_spec(self) -> dict[str, Any]:
        return guardrail_signature_index_spec(
            embedding_version=self.embedding_version,
            dimensions=self.embedding_service.dimensions,
        )

    async def screen(self, text: str) -> list[str]:
        if time.monotonic() < self._circuit_open_until:
            raise GuardrailUnavailableError("Semantic guardrail circuit breaker is open.")

        try:
            vector = await asyncio.wait_for(
                self.embedding_service.embed_text(text),
                timeout=self.settings.guardrail_timeout_seconds,
            )
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": self.index_spec["name"],
                        "path": GUARDRAIL_SIGNATURE_VECTOR_PATH,
                        "queryVector": vector,
                        "filter": guardrail_signature_lookup_filter(
                            embedding_version=self.embedding_version
                        ),
                        "numCandidates": max(20, self.settings.guardrail_signature_top_k * 20),
                        "limit": max(1, self.settings.guardrail_signature_top_k),
                    }
                },
                {
                    "$project": {
                        "_id": 1,
                        "category": 1,
                        "score": {"$meta": "vectorSearchScore"},
                    }
                },
            ]
            cursor = await get_control_database()["guardrail_signatures"].aggregate(pipeline)
            docs = await cursor.to_list(length=max(1, self.settings.guardrail_signature_top_k))
            self._consecutive_failures = 0
        except Exception as exc:  # pragma: no cover - exercised with middleware tests
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.settings.guardrail_circuit_failures:
                self._circuit_open_until = (
                    time.monotonic() + self.settings.guardrail_circuit_reset_seconds
                )
            raise GuardrailUnavailableError(f"Semantic guardrail unavailable: {exc}") from exc

        reasons: list[str] = []
        for doc in docs:
            score = float(doc.get("score", 0.0))
            if score < self.settings.guardrail_injection_threshold:
                continue
            category = str(doc.get("category") or "unknown")
            signature_id = str(doc.get("_id") or "unknown")
            reasons.append(f"blocked:semantic:{category}:{signature_id}:{score:.3f}")
        return reasons


class RegexOutboundRedactor:
    def redact(self, text: str) -> str:
        redacted = text
        redacted = EMAIL_PATTERN.sub(r"***@\2", redacted)
        redacted = SSN_PATTERN.sub("[REDACTED_SSN]", redacted)
        redacted = PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
        redacted = STRIPE_KEY_PATTERN.sub("[REDACTED_STRIPE_KEY]", redacted)
        redacted = OPENAI_KEY_PATTERN.sub("[REDACTED_OPENAI_KEY]", redacted)
        redacted = AWS_ACCESS_KEY_PATTERN.sub("[REDACTED_AWS_ACCESS_KEY]", redacted)
        redacted = GITHUB_TOKEN_PATTERN.sub("[REDACTED_GITHUB_TOKEN]", redacted)
        redacted = _redact_credit_cards(redacted)
        return redacted


class PiiNerRedactor:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled and _PRESIDIO_AVAILABLE)
        self._analyzer = AnalyzerEngine() if self.enabled else None
        self._anonymizer = AnonymizerEngine() if self.enabled else None

    def redact(self, text: str) -> str:
        if not self.enabled or self._analyzer is None or self._anonymizer is None:
            return text
        try:
            findings = self._analyzer.analyze(text=text, language="en")
            if not findings:
                return text
            return self._anonymizer.anonymize(text=text, analyzer_results=findings).text
        except Exception:
            # Fail open so a redactor hiccup never blocks a response, but log it:
            # a silently-failing NER path means PII would pass through unredacted.
            logger.warning("PII NER redaction failed; returning text unredacted.", exc_info=True)
            return text


class GuardrailService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embedding_service: EmbeddingService | None = None,
        inbound_detectors: list[InboundDetector] | None = None,
        outbound_redactors: list[OutboundRedactor] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedding_service = embedding_service or get_embedding_service(self.settings)

        self.inbound_detectors: list[InboundDetector]
        if inbound_detectors is not None:
            self.inbound_detectors = inbound_detectors
        else:
            self.inbound_detectors = [RegexInboundDetector()]
            if self.settings.guardrail_ml_enabled:
                self.inbound_detectors.append(
                    SemanticInjectionDetector(
                        settings=self.settings,
                        embedding_service=self.embedding_service,
                    )
                )

        self.outbound_redactors: list[OutboundRedactor]
        if outbound_redactors is not None:
            self.outbound_redactors = outbound_redactors
        else:
            self.outbound_redactors = [RegexOutboundRedactor()]
            if self.settings.guardrail_pii_ner_enabled:
                self.outbound_redactors.append(PiiNerRedactor(enabled=True))

    async def check_inbound(self, text: str | list[str]) -> GuardrailCheck:
        spans = [text] if isinstance(text, str) else list(text)
        reasons: list[str] = []
        for span in spans:
            if not span:
                continue
            for detector in self.inbound_detectors:
                try:
                    reasons.extend(await detector.screen(span))
                except GuardrailUnavailableError:
                    if self.settings.guardrail_fail_mode == "closed":
                        reasons.append(
                            f"blocked:guardrail_unavailable:{detector.__class__.__name__}"
                        )
        # Stable de-dup to keep responses readable when multiple spans trip same rule.
        deduped = list(dict.fromkeys(reasons))
        return GuardrailCheck(blocked=bool(deduped), reasons=deduped)

    def redact_outbound(self, text: str) -> str:
        redacted = text
        for redactor in self.outbound_redactors:
            redacted = redactor.redact(redacted)
        return redacted
