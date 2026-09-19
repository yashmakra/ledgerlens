"""Compare rules and Groq extraction on the labeled benchmark cases."""

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.evaluation import GoldenInvoice
from app.extraction import ExtractedPage, rules_extract
from app.llm_extraction import extract_invoice_with_llm

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("supplier_name", "invoice_number", "invoice_date", "purchase_order_number", "total_amount")


def normalize(value: str | None) -> str:
    if value is None:
        return ""
    value = " ".join(str(value).casefold().split()).strip()
    try:
        return str(Decimal(value.replace(",", "")))
    except (InvalidOperation, ValueError):
        return "".join(character for character in value if character.isalnum())


def load_records() -> list[GoldenInvoice]:
    raw = json.loads((ROOT / "data/evaluation/golden_invoices.json").read_text(encoding="utf-8"))
    return [GoldenInvoice(
        case_id=item["case_id"],
        pages=[ExtractedPage(**page) for page in item["pages"]],
        expected_fields=item["expected_fields"],
    ) for item in raw]


if __name__ == "__main__":
    records = load_records()
    totals = {"rules": 0, "groq": 0, "total": 0}
    for record in records:
        rules = rules_extract("invoice", record.pages)
        llm = extract_invoice_with_llm(record.pages).output
        for field in FIELDS:
            expected = normalize(record.expected_fields.get(field))
            rules_value = normalize(rules.fields[field].value)
            llm_value = normalize(llm.fields[field].value)
            totals["total"] += 1
            totals["rules"] += int(rules_value == expected)
            totals["groq"] += int(llm_value == expected)
            print(f"{record.case_id} | {field} | rules={'PASS' if rules_value == expected else 'FAIL'} | groq={'PASS' if llm_value == expected else 'FAIL'}")
    print(json.dumps({
        "rules_accuracy": round(totals["rules"] / totals["total"], 4),
        "groq_accuracy": round(totals["groq"] / totals["total"], 4),
        "passed": totals,
    }, indent=2))
