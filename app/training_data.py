"""Format labeled invoice cases as chat training examples."""

import json

SYSTEM_PROMPT = (
    "Extract invoice fields from the supplied page text. Return only JSON with "
    "supplier_name, currency, invoice_number, invoice_date, purchase_order_number, "
    "total_amount, and line_items. Use null when a field is absent; never infer values."
)
FIELDS = (
    "supplier_name",
    "currency",
    "invoice_number",
    "invoice_date",
    "purchase_order_number",
    "total_amount",
)


def training_example(record: dict) -> dict:
    """Create one evidence-preserving instruction-tuning record."""
    source_text = "\n\n".join(
        f"[Page {page['page_number']}]\n{page['text']}" for page in record["pages"]
    )
    expected = record["expected_fields"]
    output = {field: expected.get(field) or None for field in FIELDS}
    output["line_items"] = record.get("expected_line_items", [])
    return {
        "case_id": record["case_id"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": source_text},
            {"role": "assistant", "content": json.dumps(output, separators=(",", ":"))},
        ],
    }
