"""Small deterministic calculator for frozen MetricSpec definitions."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from omnisignal.contracts.metrics import MetricInput, MetricOutput, MetricResult, MetricSpec, MetricStatus


VALUE_QUANTUM = Decimal("0.0001")


def calculate_metric(spec: MetricSpec, data: MetricInput) -> MetricResult:
    status = MetricStatus.COMPLETE
    value: Decimal | None

    if spec.output == MetricOutput.COMPOSITE_INDEX:
        expected = {item.metric_id for item in spec.components}
        received = set(data.component_values)
        if received != expected:
            missing = sorted(expected - received)
            unexpected = sorted(received - expected)
            raise ValueError(f"component values mismatch; missing={missing}, unexpected={unexpected}")
        value = sum(
            (data.component_values[item.metric_id] * item.weight for item in spec.components),
            Decimal("0"),
        )
    elif data.numerator is None:
        raise ValueError("non-composite metrics require a numerator value")
    elif spec.output == MetricOutput.PERCENT:
        if data.denominator is None:
            status = MetricStatus.DENOMINATOR_MISSING
            value = None
        elif data.denominator == 0:
            status = MetricStatus.DENOMINATOR_ZERO
            value = None
        else:
            if data.numerator > data.denominator:
                raise ValueError("percent numerator cannot exceed denominator")
            value = data.numerator / data.denominator * Decimal("100")
    else:
        if data.denominator is not None:
            raise ValueError(f"{spec.output.value} input cannot provide a denominator")
        value = data.numerator

    if value is not None:
        value = value.quantize(VALUE_QUANTUM, rounding=ROUND_HALF_UP)

    return MetricResult(
        metric_id=spec.id,
        definition_hash=spec.definition_hash(),
        status=status,
        output=spec.output,
        value=value,
        numerator=data.numerator,
        denominator=data.denominator,
        period_start=data.period_start,
        period_end=data.period_end,
        coverage=spec.coverage,
        query_set_version=spec.query_set_version,
        entity_rule_version=spec.entity_rule_version,
    )
