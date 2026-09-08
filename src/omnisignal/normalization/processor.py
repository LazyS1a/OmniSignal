"""Pure deterministic transformation from one ingested record to one standard document."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from omnisignal.storage.models import IngestedRecord

from .contracts import NormalizationConfig, SourceProfile


NORMALIZER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    severity: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    normalized_id: str
    source_id: str
    source_record_id: str
    input_raw_hash: str
    raw_archive_sha256: str | None
    permission: str
    source_schema_version: str
    normalizer_version: str
    config_hash: str
    normalized_hash: str
    canonical_url: str | None
    title: str | None
    text: str | None
    published_at: datetime | None
    updated_at: datetime | None
    language: str
    entity_ids: tuple[str, ...]
    exact_fingerprint: str | None
    simhash64: str | None
    parent_source_record_id: str | None
    quality_status: str
    issues: tuple[QualityIssue, ...]

    def with_issues(self, *issues: QualityIssue) -> "NormalizedDocument":
        combined = tuple(sorted(set(self.issues + issues), key=lambda issue: (issue.code, issue.field or "")))
        status = _quality_status(combined)
        value = replace(self, issues=combined, quality_status=status)
        return replace(value, normalized_hash=_normalized_hash(value))


def normalize_record(
    record: IngestedRecord,
    profile: SourceProfile,
    config: NormalizationConfig,
) -> NormalizedDocument:
    payload = record.payload
    issues: list[QualityIssue] = []
    title = _clean_text(
        _mapped(payload, profile.fields.title),
        field="title",
        single_line=True,
        max_chars=profile.max_title_chars,
        issues=issues,
    )
    text = _clean_text(
        _mapped(payload, profile.fields.text),
        field="text",
        single_line=False,
        max_chars=profile.max_text_chars,
        issues=issues,
    )
    canonical_url = _canonical_url(_mapped(payload, profile.fields.canonical_url), issues)
    published_at = _timestamp(_mapped(payload, profile.fields.published_at), "published_at", issues)
    updated_at = _timestamp(_mapped(payload, profile.fields.updated_at), "updated_at", issues)
    language = _language(_mapped(payload, profile.fields.language), profile.default_language, issues)
    parent = _identifier(_mapped(payload, profile.fields.parent_source_record_id), "parent_source_record_id", issues)

    values = {
        "canonical_url": canonical_url,
        "title": title,
        "text": text,
        "published_at": published_at,
        "updated_at": updated_at,
        "language": language,
        "parent_source_record_id": parent,
    }
    for field in profile.required_fields:
        if values[field] is None or values[field] == "":
            issues.append(QualityIssue("missing_required_field", "error", field))
    if not title:
        issues.append(QualityIssue("missing_title", "warning", "title"))
    if not text:
        issues.append(QualityIssue("missing_text", "error", "text"))
    elif len(text) < profile.min_text_chars:
        issues.append(QualityIssue("short_text", "warning", "text"))
    if record.raw_archive_sha256 is None:
        issues.append(QualityIssue("missing_raw_archive", "error", "raw_archive_sha256"))

    match_text = _match_form("\n".join(value for value in (title, text) if value))
    entity_ids = tuple(
        sorted(
            entity.entity_id
            for entity in config.entities
            if any(_contains_alias(match_text, _match_form(alias)) for alias in entity.aliases)
        )
    )
    exact_fingerprint = hashlib.sha256(match_text.encode("utf-8")).hexdigest() if match_text else None
    simhash = None
    if len(match_text) >= config.duplicate_policy.min_content_chars:
        simhash = f"{_simhash64(match_text, config.duplicate_policy.ngram_size):016x}"

    config_hash = config.config_hash()
    identity = _digest(
        record.source_id,
        record.source_record_id,
        record.raw_hash,
        record.schema_version,
        record.permission,
        record.raw_archive_sha256 or "",
        NORMALIZER_VERSION,
        config_hash,
    )
    sorted_issues = tuple(sorted(set(issues), key=lambda issue: (issue.code, issue.field or "")))
    document = NormalizedDocument(
        normalized_id=identity,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        input_raw_hash=record.raw_hash,
        raw_archive_sha256=record.raw_archive_sha256,
        permission=record.permission,
        source_schema_version=record.schema_version,
        normalizer_version=NORMALIZER_VERSION,
        config_hash=config_hash,
        normalized_hash="0" * 64,
        canonical_url=canonical_url,
        title=title,
        text=text,
        published_at=published_at,
        updated_at=updated_at,
        language=language,
        entity_ids=entity_ids,
        exact_fingerprint=exact_fingerprint,
        simhash64=simhash,
        parent_source_record_id=parent,
        quality_status=_quality_status(sorted_issues),
        issues=sorted_issues,
    )
    return replace(document, normalized_hash=_normalized_hash(document))


def _mapped(payload: dict[str, object], field: str | None) -> object | None:
    return payload.get(field) if field is not None else None


def _clean_text(
    value: object | None,
    *,
    field: str,
    single_line: bool,
    max_chars: int,
    issues: list[QualityIssue],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        issues.append(QualityIssue("invalid_text_type", "warning", field))
        return None
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    if single_line:
        normalized = re.sub(r"\s+", " ", normalized).strip()
    else:
        lines = [re.sub(r"[\t ]+", " ", line).strip() for line in normalized.split("\n")]
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if len(normalized) > max_chars:
        issues.append(QualityIssue("field_truncated", "warning", field))
    return normalized[:max_chars] or None


def _canonical_url(value: object | None, issues: list[QualityIssue]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        issues.append(QualityIssue("invalid_url", "warning", "canonical_url"))
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        port = parsed.port
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        host = parsed.hostname.lower()
        netloc = host if port is None or port == default_port else f"{host}:{port}"
        return urlunsplit(SplitResult(parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        issues.append(QualityIssue("invalid_url", "warning", "canonical_url"))
        return None


def _timestamp(value: object | None, field: str, issues: list[QualityIssue]) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        issues.append(QualityIssue("invalid_timestamp", "warning", field))
        return None


def _language(value: object | None, default: str, issues: list[QualityIssue]) -> str:
    candidate = str(value).strip() if isinstance(value, str) and value.strip() else default
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})*|und", candidate):
        issues.append(QualityIssue("invalid_language", "warning", "language"))
        return "und"
    parts = candidate.replace("_", "-").split("-")
    normalized = parts[0].lower()
    if len(parts) > 1:
        normalized += "-" + "-".join(part.upper() if len(part) == 2 else part for part in parts[1:])
    if normalized == "und":
        issues.append(QualityIssue("language_undetermined", "warning", "language"))
    return normalized


def _identifier(value: object | None, field: str, issues: list[QualityIssue]) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate or len(candidate) > 256:
        issues.append(QualityIssue("invalid_identifier", "warning", field))
        return None
    return candidate


def _match_form(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _contains_alias(haystack: str, alias: str) -> bool:
    if not alias:
        return False
    if any("\u3400" <= char <= "\u9fff" for char in alias):
        return alias in haystack
    return re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", haystack, flags=re.UNICODE) is not None


def _simhash64(value: str, ngram_size: int) -> int:
    compact = re.sub(r"\s+", " ", value)
    tokens = [compact[index : index + ngram_size] for index in range(max(1, len(compact) - ngram_size + 1))]
    weights = Counter(tokens or [compact])
    vector = [0] * 64
    for token, weight in weights.items():
        bits = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        for index in range(64):
            vector[index] += weight if bits & (1 << index) else -weight
    result = 0
    for index, score in enumerate(vector):
        if score >= 0:
            result |= 1 << index
    return result


def _quality_status(issues: tuple[QualityIssue, ...]) -> str:
    if any(issue.severity == "error" for issue in issues):
        return "quarantined"
    if issues:
        return "warning"
    return "accepted"


def _normalized_hash(document: NormalizedDocument) -> str:
    content = {
        "source_id": document.source_id,
        "source_record_id": document.source_record_id,
        "input_raw_hash": document.input_raw_hash,
        "raw_archive_sha256": document.raw_archive_sha256,
        "permission": document.permission,
        "source_schema_version": document.source_schema_version,
        "normalizer_version": document.normalizer_version,
        "config_hash": document.config_hash,
        "canonical_url": document.canonical_url,
        "title": document.title,
        "text": document.text,
        "published_at": document.published_at.isoformat() if document.published_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
        "language": document.language,
        "entity_ids": document.entity_ids,
        "exact_fingerprint": document.exact_fingerprint,
        "simhash64": document.simhash64,
        "parent_source_record_id": document.parent_source_record_id,
        "quality_status": document.quality_status,
        "quality_codes": [(issue.code, issue.severity, issue.field) for issue in document.issues],
    }
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
