"""Small, deterministic evaluation utilities for the extraction benchmark."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.extraction import ExtractedPage, rules_extract


@dataclass(frozen=True)
class GoldenInvoice:
    case_id: str
    pages: list[ExtractedPage]
    expected_fields: dict[str, str]


@dataclass(frozen=True)
class FieldResult:
    case_id: str
    field: str
    expected: str | None
    actual: str | None
    passed: bool


def _normalize(value: str | None) -> str:
    if value is None:
        return ""
    compact = " ".join(str(value).casefold().split()).strip()
    try:
        return str(Decimal(compact.replace(",", "")))
    except (InvalidOperation, ValueError):
        return re.sub(r"[^a-z0-9]", "", compact)


def evaluate_rules(records: list[GoldenInvoice]) -> list[FieldResult]:
    """Run the rules extractor and return one auditable result per field."""
    results: list[FieldResult] = []
    for record in records:
        output = rules_extract("invoice", record.pages)
        for field, expected in record.expected_fields.items():
            actual = output.fields.get(field)
            actual_value = actual.value if actual else None
            results.append(FieldResult(
                case_id=record.case_id,
                field=field,
                expected=expected,
                actual=actual_value,
                passed=_normalize(expected) == _normalize(actual_value),
            ))
    return results


def summarize_results(results: list[FieldResult]) -> dict[str, object]:
    passed = sum(result.passed for result in results)
    total = len(results)
    by_field: dict[str, dict[str, int]] = {}
    for result in results:
        stats = by_field.setdefault(result.field, {"passed": 0, "total": 0})
        stats["total"] += 1
        stats["passed"] += int(result.passed)
    return {
        "passed": passed,
        "total": total,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "by_field": by_field,
        "failures": [result.__dict__ for result in results if not result.passed],
    }
