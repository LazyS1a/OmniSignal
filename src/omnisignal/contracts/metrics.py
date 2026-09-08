"""Metric definitions that preserve scope and denominator provenance."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from .connector import StrictModel


class MetricKind(StrEnum):
    OWNED_SEARCH_PERFORMANCE = "owned_search_performance"
    SERP_VISIBILITY = "serp_visibility"
    MONITORED_SITE_MENTIONS = "monitored_site_mentions"
    ONSITE_SEARCH = "onsite_search"
    RELATIVE_TREND = "relative_trend"
    COMPOSITE = "composite"


class MetricOutput(StrEnum):
    COUNT = "count"
    PERCENT = "percent"
    RELATIVE_INDEX = "relative_index"
    COMPOSITE_INDEX = "composite_index"


class Aggregation(StrEnum):
    COUNT = "count"
    SUM = "sum"


class TimeGrain(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class QuantitySpec(StrictModel):
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    aggregation: Aggregation
    label: str = Field(min_length=1, max_length=160)


class CoverageSpec(StrictModel):
    source_ids: tuple[str, ...] = Field(min_length=1)
    search_engines: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    top_k: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("source_ids", "search_engines", "regions", "devices", "languages")
    @classmethod
    def reject_duplicate_scope_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("coverage scope contains duplicates")
        return value


class WeightedMetric(StrictModel):
    metric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    weight: Decimal = Field(gt=0, le=1)


class MetricSpec(StrictModel):
    spec_version: str = Field(pattern=r"^1\.\d+$")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    display_name: str = Field(min_length=1, max_length=160)
    kind: MetricKind
    output: MetricOutput
    time_grain: TimeGrain
    coverage: CoverageSpec
    query_set_version: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,80}$")
    entity_rule_version: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,80}$")
    numerator: QuantitySpec | None = None
    denominator: QuantitySpec | None = None
    components: tuple[WeightedMetric, ...] = ()
    description: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_metric_definition(self) -> "MetricSpec":
        if self.output == MetricOutput.PERCENT:
            if self.numerator is None or self.denominator is None:
                raise ValueError("percent metrics require numerator and denominator definitions")
            if len(self.coverage.search_engines) > 1:
                raise ValueError("percent metrics cannot combine multiple search engines")
        elif self.output in {MetricOutput.COUNT, MetricOutput.RELATIVE_INDEX}:
            if self.numerator is None:
                raise ValueError(f"{self.output.value} metrics require a numerator definition")
            if self.denominator is not None:
                raise ValueError(f"{self.output.value} metrics cannot define a denominator")

        if self.kind == MetricKind.RELATIVE_TREND and self.output != MetricOutput.RELATIVE_INDEX:
            raise ValueError("relative trend must be labeled relative_index, never percent")

        if self.kind == MetricKind.COMPOSITE:
            if self.output != MetricOutput.COMPOSITE_INDEX:
                raise ValueError("composite metrics must be labeled composite_index")
            if len(self.components) < 2:
                raise ValueError("composite metrics require at least two weighted components")
            if len({item.metric_id for item in self.components}) != len(self.components):
                raise ValueError("composite components contain duplicate metric ids")
            if sum((item.weight for item in self.components), Decimal("0")) != Decimal("1"):
                raise ValueError("composite component weights must sum to 1")
            if self.numerator is not None or self.denominator is not None:
                raise ValueError("composite metrics use components instead of numerator or denominator")
        elif self.components:
            raise ValueError("only composite metrics can define components")

        return self

    def definition_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class MetricStatus(StrEnum):
    COMPLETE = "complete"
    DENOMINATOR_MISSING = "denominator_missing"
    DENOMINATOR_ZERO = "denominator_zero"


class MetricInput(StrictModel):
    period_start: datetime
    period_end: datetime
    numerator: Decimal | None = Field(default=None, ge=0)
    denominator: Decimal | None = Field(default=None, ge=0)
    component_values: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_period(self) -> "MetricInput":
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        if any(value < 0 for value in self.component_values.values()):
            raise ValueError("component values cannot be negative")
        return self


class MetricResult(StrictModel):
    metric_id: str
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: MetricStatus
    output: MetricOutput
    value: Decimal | None
    numerator: Decimal | None
    denominator: Decimal | None
    period_start: datetime
    period_end: datetime
    coverage: CoverageSpec
    query_set_version: str
    entity_rule_version: str
