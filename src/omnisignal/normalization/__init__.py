"""Deterministic normalization, quality, lineage and duplicate grouping."""

from .contracts import NormalizationConfig, SourceProfile, load_normalization_config
from .lineage import LineageBackfillSummary, backfill_archive_lineage
from .processor import NORMALIZER_VERSION, NormalizedDocument, QualityIssue, normalize_record
from .repository import NormalizationSummary, run_normalization

__all__ = [
    "NORMALIZER_VERSION",
    "LineageBackfillSummary",
    "NormalizationConfig",
    "NormalizationSummary",
    "NormalizedDocument",
    "QualityIssue",
    "SourceProfile",
    "backfill_archive_lineage",
    "load_normalization_config",
    "normalize_record",
    "run_normalization",
]
