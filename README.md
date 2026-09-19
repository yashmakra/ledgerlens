# LedgerLens — Supplier Invoice Reconciliation

## Live demo

Try the deployed application:

- [LedgerLens review dashboard](https://ledgerlens-qakt.onrender.com/review)
- [Interactive API documentation](https://ledgerlens-qakt.onrender.com/docs)
- [Service health check](https://ledgerlens-qakt.onrender.com/health/ready)

The demo runs on Render’s free tier, so it may sleep after inactivity. Uploaded documents are intended for demonstration and testing only.
## Project overview

LedgerLens is a production-shaped supplier invoice reconciliation platform for accounts-payable teams.

It accepts supplier invoices, extracts structured financial fields from digital and scanned documents, links the invoice to saved purchasing records, and checks whether the billed amount is supported by the purchase order, delivery receipt, and supplier contract.

The system does not silently approve questionable invoices. It produces an auditable reconciliation report and sends exceptions to a human reviewer.

## Problem solved

Accounts-payable teams often compare four separate sources manually:

- Supplier invoice: what the supplier charged
- Purchase order: what the company ordered
- Delivery receipt: what was actually received
- Supplier contract: the agreed price and tolerance

Manual comparison is slow and error-prone. LedgerLens automates the comparison while preserving the source evidence and reviewer decision.

## Main workflow

```text
Upload invoice
      ↓
Extract text with PyMuPDF or Tesseract OCR
      ↓
Extract structured fields with rules, Groq, or local QLoRA model
      ↓
Attach source evidence to every extracted value
      ↓
Match supplier, purchase order, receipt, and contract
      ↓
Run reconciliation checks
      ↓
Create a review case for exceptions
      ↓
Approve, reject, or approve with a documented override


