from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnisignal.contracts import MetricInput, MetricOutput, MetricSpec, MetricStatus
from omnisignal.metrics import calculate_metric


FIXTURE_DIR = Path(__file__).parents[1] / "examples" / "metrics"


def load_spec(name: str) -> MetricSpec:
    data = yaml.safe_load((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return MetricSpec.model_validate(data)


def period_input(**values: object) -> MetricInput:
    return MetricInput(
        period_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 8, 2, tzinfo=timezone.utc),
        **values,
    )


@pytest.mark.parametrize(
    "name",
    [
        "owned_search_ctr.yaml",
        "serp_visibility.yaml",
        "monitored_site_mention_share.yaml",
        "onsite_query_share.yaml",
        "relative_search_interest.yaml",
        "composite_visibility_index.yaml",
    ],
)
def test_reference_metric_specs_are_valid(name: str) -> None:
    assert load_spec(name).spec_version == "1.0"


def test_percent_definition_requires_denominator() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "owned_search_ctr.yaml").read_text(encoding="utf-8"))
    data.pop("denominator")
    with pytest.raises(ValidationError, match="require numerator and denominator"):
        MetricSpec.model_validate(data)


def test_percent_cannot_merge_search_engines() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "serp_visibility.yaml").read_text(encoding="utf-8"))
    data["coverage"]["search_engines"] = ["google", "bing"]
    with pytest.raises(ValidationError, match="cannot combine multiple search engines"):
        MetricSpec.model_validate(data)


def test_relative_trend_cannot_be_labeled_percent() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "relative_search_interest.yaml").read_text(encoding="utf-8"))
    data["output"] = "percent"
    data["denominator"] = {"field": "fake_total", "aggregation": "sum", "label": "Fake total"}
    with pytest.raises(ValidationError, match="relative_index"):
        MetricSpec.model_validate(data)


def test_composite_weights_must_sum_to_one() -> None:
    data = yaml.safe_load((FIXTURE_DIR / "composite_visibility_index.yaml").read_text(encoding="utf-8"))
    data["components"][1]["weight"] = 0.5
    with pytest.raises(ValidationError, match="weights must sum to 1"):
        MetricSpec.model_validate(data)


def test_percent_is_deterministic_and_preserves_provenance() -> None:
    spec = load_spec("owned_search_ctr.yaml")
    data = period_input(numerator=Decimal("25"), denominator=Decimal("200"))

    first = calculate_metric(spec, data)
    replay = calculate_metric(spec, data)

    assert first == replay
    assert first.value == Decimal("12.5000")
    assert first.numerator == Decimal("25")
    assert first.denominator == Decimal("200")
    assert first.coverage == spec.coverage
    assert first.definition_hash == spec.definition_hash()


@pytest.mark.parametrize(
    ("denominator", "status"),
    [(None, MetricStatus.DENOMINATOR_MISSING), (Decimal("0"), MetricStatus.DENOMINATOR_ZERO)],
)
def test_missing_or_zero_denominator_never_outputs_percent(
    denominator: Decimal | None,
    status: MetricStatus,
) -> None:
    result = calculate_metric(
        load_spec("owned_search_ctr.yaml"),
        period_input(numerator=Decimal("4"), denominator=denominator),
    )
    assert result.status == status
    assert result.value is None


def test_percent_numerator_cannot_exceed_denominator() -> None:
    with pytest.raises(ValueError, match="cannot exceed denominator"):
        calculate_metric(
            load_spec("serp_visibility.yaml"),
            period_input(numerator=Decimal("101"), denominator=Decimal("100")),
        )


def test_composite_requires_exact_components_and_exposes_index_label() -> None:
    spec = load_spec("composite_visibility_index.yaml")
    result = calculate_metric(
        spec,
        period_input(component_values={"owned_search_ctr": Decimal("10"), "serp_visibility": Decimal("50")}),
    )
    assert result.output == MetricOutput.COMPOSITE_INDEX
    assert result.value == Decimal("34.0000")

    with pytest.raises(ValueError, match="component values mismatch"):
        calculate_metric(spec, period_input(component_values={"owned_search_ctr": Decimal("10")}))


def test_checked_in_metric_schema_matches_runtime_contract() -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "metric-spec.schema.json"
    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))
    assert checked_in == MetricSpec.model_json_schema()
