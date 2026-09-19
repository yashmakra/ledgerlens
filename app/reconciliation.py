"""Deterministic two-way/three-way invoice matching against purchasing records."""

from decimal import Decimal

from app.models import Invoice, PurchaseOrderLine

MONEY_TOLERANCE = Decimal("0.02")

SOURCE_RECORDS = {
    "supplier_match": ["invoice", "purchase_order"],
    "supplier_mismatch": ["invoice", "purchase_order"],
    "currency_match": ["invoice", "purchase_order"],
    "currency_mismatch": ["invoice", "purchase_order"],
    "receipt_present": ["purchase_order", "delivery_receipt"],
    "receipt_missing": ["purchase_order", "delivery_receipt"],
    "contract_missing": ["purchase_order", "contract"],
    "contract_invalid": ["invoice", "purchase_order", "contract"],
    "subtotal_arithmetic": ["invoice", "invoice_line"],
    "tax_total_arithmetic": ["invoice"],
    "po_line_match": ["invoice_line", "purchase_order_line"],
    "po_line_missing": ["invoice_line", "purchase_order"],
    "po_line_ambiguous": ["invoice_line", "purchase_order"],
    "ordered_quantity": ["invoice_line", "purchase_order_line"],
    "accepted_quantity": ["invoice_line", "delivery_receipt_line"],
    "unit_price": ["invoice_line", "purchase_order_line", "contract"],
    "line_arithmetic": ["invoice_line"],
    "purchase_order_missing": ["invoice", "purchase_order"],
}


def _normalise_code(value: str) -> str:
    return "".join(value.casefold().split())


def _check(
    code: str,
    status: str,
    message: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    tolerance: str | None = None,
    line_number: int | None = None,
) -> dict:
    return {
        "code": code,
        "status": status,
        "severity": "blocking" if status == "exception" else "info",
        "message": message,
        "source_records": SOURCE_RECORDS.get(code, []),
        "expected": expected,
        "actual": actual,
        "tolerance": tolerance,
        "invoice_line_number": line_number,
    }


def reconcile_invoice(invoice: Invoice) -> dict:
    """Compare invoice values to its PO and accepted receipt quantities.

    This function only reports discrepancies. It never edits invoice or PO data.
    A missing or ambiguous relationship becomes an exception for human review.
    """
    checks: list[dict] = []
    po = invoice.purchase_order

    if po is None:
        checks.append(_check(
            "purchase_order_missing", "exception", "Invoice is not linked to a purchase order."
        ))
    else:
        if po.supplier_id == invoice.supplier_id:
            checks.append(_check("supplier_match", "pass", "Invoice and purchase order use the same supplier."))
        else:
            checks.append(_check(
                "supplier_mismatch", "exception", "Invoice and purchase order suppliers differ.",
                expected=str(po.supplier_id), actual=str(invoice.supplier_id),
            ))

        if po.currency == invoice.currency:
            checks.append(_check("currency_match", "pass", "Invoice and purchase order currencies match."))
        else:
            checks.append(_check(
                "currency_mismatch", "exception", "Invoice currency differs from the purchase order.",
                expected=po.currency, actual=invoice.currency,
            ))

        if po.receipts:
            checks.append(_check("receipt_present", "pass", "At least one delivery receipt is linked to the purchase order."))
        else:
            checks.append(_check(
                "receipt_missing", "exception",
                "No delivery receipt is linked to the purchase order; received quantity cannot be verified.",
            ))

        contract = po.contract
        if contract is None:
            checks.append(_check(
                "contract_missing", "exception",
                "No contract is linked to the purchase order; contract terms cannot be verified.",
            ))
        elif not (
            contract.valid_from <= invoice.invoice_date
            and (contract.valid_to is None or invoice.invoice_date <= contract.valid_to)
        ):
            checks.append(_check(
                "contract_invalid", "exception",
                "The linked contract is not active on the invoice date.",
                expected=f"{contract.valid_from} to {contract.valid_to or 'open-ended'}",
                actual=str(invoice.invoice_date),
            ))

        line_sum = sum((line.line_amount for line in invoice.lines), start=Decimal(0))
        if abs(line_sum - invoice.subtotal) <= MONEY_TOLERANCE:
            checks.append(_check(
                "subtotal_arithmetic", "pass", "Invoice subtotal equals the sum of invoice line amounts.",
                expected=str(line_sum), actual=str(invoice.subtotal),
            ))
        else:
            checks.append(_check(
                "subtotal_arithmetic", "exception", "Invoice subtotal does not equal the sum of invoice line amounts.",
                expected=str(line_sum), actual=str(invoice.subtotal),
            ))

        calculated_total = invoice.subtotal + invoice.tax_amount
        if abs(calculated_total - invoice.total_amount) <= MONEY_TOLERANCE:
            checks.append(_check(
                "tax_total_arithmetic", "pass", "Subtotal plus tax equals the invoice total.",
                expected=str(calculated_total), actual=str(invoice.total_amount),
            ))
        else:
            checks.append(_check(
                "tax_total_arithmetic", "exception", "Subtotal plus tax does not equal the invoice total.",
                expected=str(calculated_total), actual=str(invoice.total_amount),
            ))

        po_lines_by_code: dict[str, list[PurchaseOrderLine]] = {}
        for po_line in po.lines:
            po_lines_by_code.setdefault(_normalise_code(po_line.item_code), []).append(po_line)

        accepted_by_line_id: dict[int, Decimal] = {}
        accepted_by_code: dict[str, Decimal] = {}
        for receipt in po.receipts:
            for receipt_line in receipt.lines:
                accepted_by_code[_normalise_code(receipt_line.item_code)] = (
                    accepted_by_code.get(_normalise_code(receipt_line.item_code), Decimal(0))
                    + receipt_line.quantity_accepted
                )
                if receipt_line.purchase_order_line_id is not None:
                    accepted_by_line_id[receipt_line.purchase_order_line_id] = (
                        accepted_by_line_id.get(receipt_line.purchase_order_line_id, Decimal(0))
                        + receipt_line.quantity_accepted
                    )

        tolerance_pct = contract.price_tolerance_pct if contract else Decimal(0)
        contract_active = bool(
            contract
            and contract.valid_from <= invoice.invoice_date
            and (contract.valid_to is None or invoice.invoice_date <= contract.valid_to)
        )

        for invoice_line in invoice.lines:
            line_number = invoice_line.line_number
            code = _normalise_code(invoice_line.item_code)
            candidates = po_lines_by_code.get(code, [])
            if not candidates:
                checks.append(_check(
                    "po_line_missing", "exception", "Invoice item code was not found on the purchase order.",
                    expected="a purchase-order item code", actual=invoice_line.item_code,
                    line_number=line_number,
                ))
                continue
            if len(candidates) > 1:
                checks.append(_check(
                    "po_line_ambiguous", "exception", "Item code matches multiple purchase-order lines.",
                    actual=invoice_line.item_code, line_number=line_number,
                ))
                continue

            po_line = candidates[0]
            checks.append(_check(
                "po_line_match", "pass", "Invoice item matched a purchase-order line.",
                expected=po_line.item_code, actual=invoice_line.item_code, line_number=line_number,
            ))

            if invoice_line.quantity <= po_line.ordered_quantity:
                checks.append(_check(
                    "ordered_quantity", "pass", "Invoiced quantity is within the ordered quantity.",
                    expected=str(po_line.ordered_quantity), actual=str(invoice_line.quantity),
                    line_number=line_number,
                ))
            else:
                checks.append(_check(
                    "ordered_quantity", "exception", "Invoiced quantity exceeds the quantity ordered.",
                    expected=str(po_line.ordered_quantity), actual=str(invoice_line.quantity),
                    line_number=line_number,
                ))

            received = accepted_by_line_id.get(
                po_line.id, accepted_by_code.get(code, Decimal(0))
            )
            if invoice_line.quantity <= received:
                checks.append(_check(
                    "accepted_quantity", "pass", "Invoiced quantity is covered by accepted receipts.",
                    expected=str(received), actual=str(invoice_line.quantity), line_number=line_number,
                ))
            else:
                checks.append(_check(
                    "accepted_quantity", "exception",
                    "Invoiced quantity exceeds the quantity accepted in delivery receipts.",
                    expected=str(received), actual=str(invoice_line.quantity), line_number=line_number,
                ))

            price_delta = abs(invoice_line.unit_price - po_line.unit_price)
            allowed_delta = po_line.unit_price * tolerance_pct / Decimal(100) if contract_active else Decimal(0)
            if price_delta <= allowed_delta:
                checks.append(_check(
                    "unit_price", "pass", "Invoice unit price is within the applicable contract tolerance.",
                    expected=str(po_line.unit_price), actual=str(invoice_line.unit_price),
                    tolerance=f"{tolerance_pct}% ({allowed_delta} per unit)",
                    line_number=line_number,
                ))
            else:
                explanation = (
                    "Invoice unit price exceeds the contract tolerance."
                    if contract_active else "Invoice unit price differs from the PO; no active contract tolerance applies."
                )
                checks.append(_check(
                    "unit_price", "exception", explanation,
                    expected=str(po_line.unit_price), actual=str(invoice_line.unit_price),
                    tolerance=f"{tolerance_pct}% ({allowed_delta} per unit)",
                    line_number=line_number,
                ))

            calculated_amount = invoice_line.quantity * invoice_line.unit_price
            if abs(calculated_amount - invoice_line.line_amount) <= MONEY_TOLERANCE:
                checks.append(_check(
                    "line_arithmetic", "pass", "Quantity multiplied by unit price matches the invoice line amount.",
                    expected=str(calculated_amount), actual=str(invoice_line.line_amount),
                    line_number=line_number,
                ))
            else:
                checks.append(_check(
                    "line_arithmetic", "exception", "Invoice line amount does not equal quantity multiplied by unit price.",
                    expected=str(calculated_amount), actual=str(invoice_line.line_amount),
                    line_number=line_number,
                ))

    exception_count = sum(check["status"] == "exception" for check in checks)
    return {
        "status": "needs_review" if exception_count else "matched",
        "checks": checks,
        "exception_count": exception_count,
    }
