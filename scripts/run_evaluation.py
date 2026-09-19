"""Run the local extraction benchmark: python scripts/run_evaluation.py"""

import json
from pathlib import Path

from app.evaluation import GoldenInvoice, evaluate_rules, summarize_results
from app.extraction import ExtractedPage

ROOT = Path(__file__).resolve().parents[1]


def load_records(path: Path) -> list[GoldenInvoice]:
    raw_records = json.loads(path.read_text(encoding="utf-8"))
    return [GoldenInvoice(
        case_id=record["case_id"],
        pages=[ExtractedPage(**page) for page in record["pages"]],
        expected_fields=record["expected_fields"],
    ) for record in raw_records]


if __name__ == "__main__":
    records = load_records(ROOT / "data" / "evaluation" / "golden_invoices.json")
    summary = summarize_results(evaluate_rules(records))
    print(json.dumps(summary, indent=2))
