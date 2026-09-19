"""Generate labeled fictional invoices for fine-tuning pipeline development."""

import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "training" / "synthetic_invoices.json"
RANDOM = random.Random(42)
VENDORS = [
    ("Northwind Components", "NW"),
    ("Acme Industrial Supply", "AC"),
    ("Fabrikam Tools", "FT"),
    ("Globex Manufacturing", "GX"),
    ("Contoso Parts", "CP"),
    ("Adventure Works", "AW"),
    ("Tailspin Logistics", "TS"),
    ("Wide World Imports", "WW"),
    ("Proseware Services", "PS"),
    ("Woodgrove Bank", "WB"),
    ("Blue Yonder Airlines", "BY"),
    ("Consolidated Retail", "CR"),
]
CURRENCIES = ("USD", "EUR", "GBP", "INR")
ITEMS = (("WIDGET-1", "Industrial Widget"), ("BOLT-22", "Stainless Steel Bolts"), ("PUMP-7", "Hydraulic Pump"))


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def make_record(index: int, vendor: str, code: str) -> dict:
    currency = CURRENCIES[index % len(CURRENCIES)]
    item_code, description = ITEMS[index % len(ITEMS)]
    quantity = Decimal((index % 40) + 1)
    unit_price = Decimal(25 + (index % 9) * 17)
    amount = quantity * unit_price
    invoice_number = f"{code}-{1000 + index}"
    po_number = f"PO-{7000 + index}"
    invoice_date = date(2026, 1, 1) + timedelta(days=index)
    label_style = index % 4
    total_label = ("Total Due", "Grand Total", "Amount Due", "TOTAL")[label_style]
    invoice_label = ("Invoice Number", "Invoice #", "Invoice No.", "INVOICE NO")[label_style]
    po_label = ("Purchase Order", "P.O. No", "PO", "Purchase Order Number")[label_style]
    amount_text = f"{currency} {money(amount)}" if label_style == 1 else f"${money(amount)}"
    heading = vendor if index % 7 else f"{vendor} INVOICE"
    text = "\n".join([
        heading,
        invoice_label + ": " + invoice_number,
        "Invoice Date: " + invoice_date.isoformat(),
        po_label + ": " + po_number,
        "Currency: " + currency,
        "Item: " + item_code + " - " + description,
        "Quantity: " + str(quantity),
        "Unit Price: " + money(unit_price),
        total_label + ": " + amount_text,
    ])
    if index % 11 == 0:
        text = text.replace("Total Due", "TAL DUE")
    return {
        "case_id": f"synthetic-{code.lower()}-{index:03d}",
        "split_group": code,
        "pages": [{"page_number": 1, "method": "ocr" if index % 3 == 0 else "pdf_text", "text": text}],
        "expected_fields": {
            "supplier_name": vendor,
            "currency": currency,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date.isoformat(),
            "purchase_order_number": po_number,
            "total_amount": f"{amount:.2f}",
        },
        "expected_line_items": [{
            "item_code": item_code,
            "description": description,
            "quantity": str(quantity),
            "unit_price": f"{unit_price:.2f}",
            "amount": f"{amount:.2f}",
        }],
    }


def main() -> None:
    records = [make_record(index, *VENDORS[index % len(VENDORS)]) for index in range(240)]
    RANDOM.shuffle(records)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} fictional labeled invoices to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
