"""Evaluate files stored by the real document pipeline."""

import json
from pathlib import Path

from app.evaluation import GoldenInvoice, evaluate_rules, summarize_results
from app.extraction import read_pages

ROOT = Path(__file__).resolve().parents[1]


def load_records(path: Path) -> list[GoldenInvoice]:
    records = []
    for record in json.loads(path.read_text(encoding="utf-8")):
        file_path = ROOT / record["file"]
        pages, notes = read_pages(str(file_path), record["media_type"])
        if notes:
            print(f"{record['case_id']} notes: {'; '.join(notes)}")
        records.append(GoldenInvoice(
            case_id=record["case_id"],
            pages=pages,
            expected_fields=record["expected_fields"],
        ))
    return records


if __name__ == "__main__":
    records = load_records(ROOT / "data" / "evaluation" / "real_documents.json")
    print(json.dumps(summarize_results(evaluate_rules(records)), indent=2))
