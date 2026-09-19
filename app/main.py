import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, selectinload

from app.db import get_db, init_db
from app.extraction import ExtractedPage, rules_extract
from app.llm_extraction import LLMConfigurationError, LLMRequestError, extract_invoice_with_llm
from app.local_extraction import (
    LocalModelConfigurationError,
    LocalModelRequestError,
    extract_invoice_with_local_model,
)
from app.models import (
    AuditEvent,
    Contract,
    DeliveryReceipt,
    DeliveryReceiptLine,
    Document,
    DocumentExtraction,
    DocumentPage,
    DocumentProcessingJob,
    DocumentUpload,
    Invoice,
    InvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
    ReconciliationCase,
    Supplier,
)
from app.reconciliation import reconcile_invoice
from app.schemas import (
    CaseDecisionRead,
    CaseDecisionRequest,
    CaseInvoiceCorrectionRead,
    CaseInvoiceCorrectionRequest,
    CaseRead,
    ContractCreate,
    ContractRead,
    DocumentExtractionRead,
    DocumentJobRead,
    DocumentPageRead,
    DocumentRead,
    DocumentRecordMatchRead,
    DocumentUploadAttemptRead,
    DocumentUploadRead,
    ExtractedFieldRead,
    ExtractionOutputRead,
    ExtractorComparisonRead,
    InvoiceCreate,
    InvoiceLinkConfirmationRead,
    InvoiceLinkConfirmationRequest,
    InvoiceRead,
    PurchaseOrderCreate,
    PurchaseOrderMatchCandidateRead,
    PurchaseOrderRead,
    ReceiptCreate,
    ReceiptRead,
    ReconciliationResultRead,
    SupplierCreate,
    SupplierMatchCandidateRead,
    SupplierRead,
)
from app.storage import content_sha256, max_upload_bytes, save_content, validate_content


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Local development convenience. Production migrations come in a later phase.
    init_db()
    yield


app = FastAPI(title="Supplier Invoice Reconciliation", version="0.1.0", lifespan=lifespan)
MAX_JOB_ATTEMPTS = 3
STATIC_DIR = Path(__file__).resolve().parent / "static"
REVIEW_PAGE = STATIC_DIR / "review.html"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def home_page():
    return RedirectResponse("/review")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness(db: Annotated[Session, Depends(get_db)]) -> dict[str, object]:
    """Report whether dependencies needed for document processing are available."""
    checks: dict[str, str] = {}
    try:
        db.execute(sql_text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001 - readiness must return a useful diagnosis.
        checks["database"] = "error"

    tesseract_path = shutil.which("tesseract")
    if not tesseract_path:
        configured = os.getenv("TESSERACT_CMD")
        candidate = Path(configured) if configured else Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"
        tesseract_path = str(candidate) if candidate.is_file() else None
    checks["ocr"] = "ok" if tesseract_path else "unavailable"
    checks["worker_queue"] = "ok" if db.scalar(select(DocumentProcessingJob.id).where(DocumentProcessingJob.status == "queued").limit(1)) else "idle"
    ready = checks["database"] == "ok"
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@app.get("/review", include_in_schema=False)
def review_page():
    """Serve the local human-review page for the portfolio demo."""
    return FileResponse(REVIEW_PAGE, media_type="text/html")


def _parent_exists(parent_type: str | None, parent_id: int | None, db: Session) -> bool:
    if parent_type is None and parent_id is None:
        return True
    if parent_type is None or parent_id is None:
        return False
    model_by_parent_type = {
        "invoice": Invoice,
        "purchase_order": PurchaseOrder,
        "delivery_receipt": DeliveryReceipt,
        "contract": Contract,
    }
    return bool(db.get(model_by_parent_type[parent_type], parent_id))


@app.post("/documents", response_model=DocumentUploadRead, status_code=202)
async def upload_document(
    file: Annotated[UploadFile, File()],
    document_type: Annotated[str, Form()],
    db: Annotated[Session, Depends(get_db)],
    parent_type: Annotated[str | None, Form()] = None,
    parent_id: Annotated[int | None, Form()] = None,
):
    """Store an invoice/PO/receipt/contract and queue Phase 2 intake processing."""
    allowed_types = {"invoice", "purchase_order", "delivery_receipt", "contract"}
    if document_type not in allowed_types:
        raise HTTPException(422, "document_type must be invoice, purchase_order, delivery_receipt, or contract")
    if parent_type is not None and parent_type not in allowed_types:
        raise HTTPException(422, "parent_type is invalid")
    if not _parent_exists(parent_type, parent_id, db):
        raise HTTPException(422, "parent_type and parent_id must refer to an existing matching record")
    content = await file.read(max_upload_bytes() + 1)
    if not content:
        raise HTTPException(422, "Uploaded file is empty")
    if len(content) > max_upload_bytes():
        raise HTTPException(413, "File exceeds MAX_UPLOAD_MB")
    original_filename = (file.filename or "upload")[:255]
    try:
        validate_content(content, original_filename)
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc
    digest = content_sha256(content)
    existing = db.scalar(select(Document).where(Document.sha256 == digest))
    if existing:
        job = db.scalar(select(DocumentProcessingJob).where(DocumentProcessingJob.document_id == existing.id))
        upload = DocumentUpload(
            id=str(uuid4()),
            document_id=existing.id,
            original_filename=original_filename,
        )
        db.add(upload)
        db.commit()
        db.refresh(upload)
        return DocumentUploadRead(document=existing, job=job, upload=upload, duplicate_upload=True)
    try:
        document_id, path, media_type = save_content(content, original_filename)
    except ValueError as exc:
        raise HTTPException(415, str(exc)) from exc
    document = Document(
        id=document_id,
        document_type=document_type,
        parent_type=parent_type,
        parent_id=parent_id,
        original_filename=original_filename,
        media_type=media_type,
        storage_path=path,
        sha256=digest,
        size_bytes=len(content),
    )
    job = DocumentProcessingJob(id=str(uuid4()), document=document, status="queued")
    upload = DocumentUpload(id=str(uuid4()), document=document, original_filename=original_filename)
    db.add_all([document, job, upload])
    db.commit()
    db.refresh(document)
    db.refresh(job)
    db.refresh(upload)
    return DocumentUploadRead(document=document, job=job, upload=upload, duplicate_upload=False)


@app.get("/documents", response_model=list[DocumentRead])
def list_documents(db: Annotated[Session, Depends(get_db)]):
    return list(db.scalars(select(Document).order_by(Document.created_at.desc())).all())


@app.get("/documents/{document_id}", response_model=DocumentRead)
def get_document(document_id: str, db: Annotated[Session, Depends(get_db)]):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    return document


@app.get("/documents/{document_id}/uploads", response_model=list[DocumentUploadAttemptRead])
def list_document_uploads(document_id: str, db: Annotated[Session, Depends(get_db)]):
    if not db.get(Document, document_id):
        raise HTTPException(404, "document not found")
    return list(
        db.scalars(
            select(DocumentUpload)
            .where(DocumentUpload.document_id == document_id)
            .order_by(DocumentUpload.uploaded_at)
        ).all()
    )


@app.get("/documents/{document_id}/source")
def get_document_source(document_id: str, db: Annotated[Session, Depends(get_db)]):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    return FileResponse(document.storage_path, media_type=document.media_type, filename=document.original_filename)


@app.get("/documents/{document_id}/pages", response_model=list[DocumentPageRead])
def list_document_pages(document_id: str, db: Annotated[Session, Depends(get_db)]):
    if not db.get(Document, document_id):
        raise HTTPException(404, "document not found")
    statement = select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
    return list(db.scalars(statement).all())


@app.get("/documents/{document_id}/extraction", response_model=DocumentExtractionRead)
def get_document_extraction(document_id: str, db: Annotated[Session, Depends(get_db)]):
    extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_id))
    if not extraction:
        raise HTTPException(404, "Document has not been extracted yet")
    return extraction


def _match_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _decimal_value(value: str | None, field_name: str) -> Decimal:
    """Parse a money or quantity field retained by the baseline extractor."""
    if not value:
        raise HTTPException(422, f"{field_name} is missing from the invoice extraction")
    try:
        return Decimal(value.replace(",", "").replace("USD", "").strip())
    except InvalidOperation as exc:
        raise HTTPException(422, f"{field_name} is not a valid number") from exc


def _invoice_line_from_source(pages: list[DocumentPage]) -> InvoiceLine:
    """Read one simple table row from the retained source text.

    This is deliberately conservative: automatic case creation stops when a document
    does not contain the expected item-code, description, quantity, price, amount row.
    A reviewer can then use the normal confirmation flow instead.
    """
    lines = [line.strip() for page in pages for line in page.text.splitlines() if line.strip()]
    try:
        start = lines.index("Item code")
    except ValueError as exc:
        raise HTTPException(422, "invoice line-items table was not found in source text") from exc
    values = lines[start + 5 :]
    if len(values) < 5:
        raise HTTPException(422, "invoice line-item row is incomplete; use reviewer confirmation")
    item_code, description, quantity, unit_price, line_amount = values[:5]
    if not item_code or not description:
        raise HTTPException(422, "invoice line-item identity is missing; use reviewer confirmation")
    return InvoiceLine(
        line_number=1,
        item_code=item_code,
        description=description,
        quantity=_decimal_value(quantity, "invoice line quantity"),
        unit_price=_decimal_value(unit_price, "invoice line unit price"),
        line_amount=_decimal_value(line_amount, "invoice line amount"),
    )


def _source_table_values(pages: list[DocumentPage]) -> list[str]:
    """Return the first simple item-code table row retained from a source document."""
    lines = [line.strip() for page in pages for line in page.text.splitlines() if line.strip()]
    try:
        start = lines.index("Item code")
    except ValueError as exc:
        raise HTTPException(422, "item table was not found in source text") from exc
    values = lines[start + 5 :]
    if len(values) < 5:
        raise HTTPException(422, "item table is incomplete; review the source manually")
    return values[:5]


def _document_pages(document_id: str, db: Session) -> list[DocumentPage]:
    return list(db.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
    ).all())


def _resolved_supplier(value: str | None, db: Session) -> Supplier:
    matches = [
        supplier for supplier in db.scalars(select(Supplier).order_by(Supplier.id)).all()
        if value and _match_key(supplier.legal_name) == _match_key(value)
    ]
    if len(matches) != 1:
        raise HTTPException(422, "supplier could not be resolved to exactly one saved supplier")
    return matches[0]


def _resolved_or_created_supplier(value: str | None, db: Session) -> tuple[Supplier, bool]:
    """Resolve a supplier or create one during an explicit reviewer save."""
    if not value or not value.strip():
        raise HTTPException(422, "supplier name is missing from the extraction")
    normalized = _match_key(value)
    matches = [
        supplier for supplier in db.scalars(select(Supplier).order_by(Supplier.id)).all()
        if _match_key(supplier.legal_name) == normalized
    ]
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        raise HTTPException(422, "supplier matched multiple saved suppliers; resolve the ambiguity first")

    words = re.findall(r"[A-Za-z0-9]+", value.upper())
    code = "".join(word[0] for word in words)[:8] or "SUP"
    existing_codes = set(db.scalars(select(Supplier.supplier_code)).all())
    candidate = code
    suffix = 2
    while candidate in existing_codes:
        candidate = f"{code[: max(1, 50 - len(str(suffix)))]}{suffix}"
        suffix += 1
    supplier = Supplier(supplier_code=candidate, legal_name=value.strip().title())
    db.add(supplier)
    db.flush()
    return supplier, True


def _link_contract_to_purchase_orders(contract: Contract, db: Session) -> int:
    """Attach a saved contract to eligible supplier POs that are not linked yet."""
    purchase_orders = db.scalars(
        select(PurchaseOrder).where(
            PurchaseOrder.supplier_id == contract.supplier_id,
            PurchaseOrder.contract_id.is_(None),
            PurchaseOrder.currency == contract.currency,
        )
    ).all()
    linked = 0
    for purchase_order in purchase_orders:
        if purchase_order.ordered_on < contract.valid_from:
            continue
        if contract.valid_to and purchase_order.ordered_on > contract.valid_to:
            continue
        purchase_order.contract_id = contract.id
        linked += 1
    return linked


def _matching_contract_for_purchase_order(po: PurchaseOrder, db: Session) -> Contract | None:
    """Find a single eligible contract and repair an older unlinked PO."""
    if po.contract_id:
        return db.get(Contract, po.contract_id)
    candidates = [
        contract for contract in db.scalars(
            select(Contract).where(
                Contract.supplier_id == po.supplier_id,
                Contract.currency == po.currency,
            )
        ).all()
        if po.ordered_on >= contract.valid_from
        and (contract.valid_to is None or po.ordered_on <= contract.valid_to)
    ]
    if len(candidates) != 1:
        return None
    po.contract_id = candidates[0].id
    return candidates[0]


@app.get("/documents/{document_id}/record-match", response_model=DocumentRecordMatchRead)
def suggest_document_record_match(document_id: str, db: Annotated[Session, Depends(get_db)]):
    """Suggest exact supplier and PO links from saved invoice extraction; never writes records."""
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    if document.document_type != "invoice":
        raise HTTPException(422, "record matching currently supports invoices only")
    extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_id))
    if not extraction:
        raise HTTPException(409, "invoice has not been extracted yet")

    output = ExtractionOutputRead.model_validate(extraction.output_json)
    fields = output.fields
    supplier_value = fields.get("supplier_name", ExtractedFieldRead()).value
    po_value = fields.get("purchase_order_number", ExtractedFieldRead()).value
    invoice_number = fields.get("invoice_number", ExtractedFieldRead()).value

    supplier_candidates = []
    if supplier_value:
        suppliers = db.scalars(select(Supplier).order_by(Supplier.id)).all()
        supplier_candidates = [
            supplier for supplier in suppliers
            if _match_key(supplier.legal_name) == _match_key(supplier_value)
        ]
    if not supplier_value:
        supplier_status = "missing"
    elif not supplier_candidates:
        supplier_status = "not_found"
    elif len(supplier_candidates) > 1:
        supplier_status = "ambiguous"
    else:
        supplier_status = "matched"
    matched_supplier = supplier_candidates[0] if supplier_status == "matched" else None

    po_candidates = []
    if po_value:
        purchase_orders = db.scalars(select(PurchaseOrder).order_by(PurchaseOrder.id)).all()
        all_number_matches = [
            po for po in purchase_orders if _match_key(po.po_number) == _match_key(po_value)
        ]
        if matched_supplier:
            po_candidates = [po for po in all_number_matches if po.supplier_id == matched_supplier.id]
            if not po_candidates and all_number_matches:
                purchase_order_status = "supplier_mismatch"
                po_candidates = all_number_matches
            elif not po_candidates:
                purchase_order_status = "not_found"
            elif len(po_candidates) > 1:
                purchase_order_status = "ambiguous"
            else:
                purchase_order_status = "matched"
        else:
            po_candidates = all_number_matches
            if not po_candidates:
                purchase_order_status = "not_found"
            elif len(po_candidates) > 1:
                purchase_order_status = "ambiguous"
            else:
                purchase_order_status = "matched"
    else:
        purchase_order_status = "missing"

    matched_po = po_candidates[0] if purchase_order_status == "matched" else None
    duplicate_invoice = None
    if invoice_number and matched_supplier:
        supplier_invoices = db.scalars(
            select(Invoice).where(Invoice.supplier_id == matched_supplier.id)
        ).all()
        duplicate_invoice = next(
            (
                invoice for invoice in supplier_invoices
                if _match_key(invoice.invoice_number) == _match_key(invoice_number)
            ),
            None,
        )

    notes: list[str] = []
    if supplier_status != "matched":
        notes.append("Supplier needs review because the extracted name did not resolve to exactly one saved supplier.")
    if purchase_order_status != "matched":
        notes.append("Purchase order needs review because the extracted number did not resolve to one PO for this supplier.")
    if not invoice_number:
        notes.append("Invoice number is missing from the saved extraction.")
    if duplicate_invoice:
        notes.append("An invoice with this number already exists for the matched supplier.")

    ready_to_link = bool(
        matched_supplier and matched_po and invoice_number and not duplicate_invoice
        and matched_po.supplier_id == matched_supplier.id
    )
    return DocumentRecordMatchRead(
        document_id=document_id,
        extractor_version=extraction.extractor_version,
        status="matched" if ready_to_link else "needs_review",
        ready_to_link=ready_to_link,
        invoice_number=invoice_number,
        duplicate_invoice_id=duplicate_invoice.id if duplicate_invoice else None,
        supplier_value=supplier_value,
        supplier_status=supplier_status,
        supplier_id=matched_supplier.id if matched_supplier else None,
        supplier_candidates=[
            SupplierMatchCandidateRead(
                id=supplier.id,
                supplier_code=supplier.supplier_code,
                legal_name=supplier.legal_name,
            )
            for supplier in supplier_candidates
        ],
        purchase_order_value=po_value,
        purchase_order_status=purchase_order_status,
        purchase_order_id=matched_po.id if matched_po else None,
        purchase_order_candidates=[
            PurchaseOrderMatchCandidateRead(
                id=po.id,
                po_number=po.po_number,
                supplier_id=po.supplier_id,
                currency=po.currency,
            )
            for po in po_candidates
        ],
        notes=notes,
    )


@app.get("/documents/{document_id}/reconciliation-requirements")
def reconciliation_requirements(document_id: str, db: Annotated[Session, Depends(get_db)]) -> dict:
    """State which saved records are available for a full invoice reconciliation.

    The endpoint intentionally reports missing records instead of silently treating
    them as a match. The client uses this to request only the next source document.
    """
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    if document.document_type != "invoice":
        raise HTTPException(422, "requirements currently support invoices only")
    extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_id))
    if not extraction:
        raise HTTPException(409, "invoice extraction is not ready yet")

    output = ExtractionOutputRead.model_validate(extraction.output_json)
    supplier_name = output.fields.get("supplier_name", ExtractedFieldRead()).value
    po_number = output.fields.get("purchase_order_number", ExtractedFieldRead()).value
    supplier_matches = [
        supplier for supplier in db.scalars(select(Supplier).order_by(Supplier.id)).all()
        if supplier_name and _match_key(supplier.legal_name) == _match_key(supplier_name)
    ]
    supplier = supplier_matches[0] if len(supplier_matches) == 1 else None
    po_matches = []
    if supplier and po_number:
        po_matches = [
            po for po in db.scalars(select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier.id)).all()
            if _match_key(po.po_number) == _match_key(po_number)
        ]
    po = po_matches[0] if len(po_matches) == 1 else None
    receipts = list(db.scalars(select(DeliveryReceipt).where(DeliveryReceipt.purchase_order_id == po.id)).all()) if po else []
    contract = _matching_contract_for_purchase_order(po, db) if po else None
    if po and contract and db.is_modified(po, include_collections=False):
        db.commit()

    def requirement(key: str, label: str, available: bool, detail: str) -> dict:
        return {"key": key, "label": label, "status": "available" if available else "missing", "detail": detail}

    supplier_ok = supplier is not None
    po_ok = po is not None
    requirements = [
        requirement("invoice", "Supplier invoice", True, "Extracted from the uploaded source."),
        requirement("purchase_order", "Purchase order", po_ok, "A saved PO matches the extracted supplier and PO number." if po_ok else "No saved PO matches this invoice."),
        requirement("delivery_receipt", "Delivery receipt", bool(receipts), "Accepted quantities are available for this PO." if receipts else "No receipt is linked to the matched PO."),
        requirement("contract", "Supplier contract", contract is not None, "Pricing tolerance is available for this PO." if contract else "No contract is linked to the matched PO."),
    ]
    missing = [item["key"] for item in requirements if item["status"] == "missing" and item["key"] != "invoice"]
    # A receipt and contract depend on a resolved PO. Ask for that record first,
    # then request the remaining evidence only after the PO has been saved.
    next_required = ["purchase_order"] if not po_ok else [
        item for item in missing if item in {"delivery_receipt", "contract"}
    ]
    return {
        "document_id": document.id,
        "supplier_resolved": supplier_ok,
        "purchase_order_id": po.id if po else None,
        "requirements": requirements,
        "missing": missing,
        "next_required": next_required,
        "ready_for_full_reconciliation": not missing,
    }


@app.post("/documents/{document_id}/save-purchasing-record", status_code=201)
def save_purchasing_record_from_document(
    document_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Create a PO, receipt, or contract record from an extracted source document.

    This is a reviewer-triggered write. It rejects incomplete or duplicate documents
    so an uncertain extraction cannot silently affect a payment decision.
    """
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    if document.document_type not in {"purchase_order", "delivery_receipt", "contract"}:
        raise HTTPException(422, "only a PO, receipt, or contract can be saved as a purchasing record")
    if document.parent_type:
        raise HTTPException(409, "this source document is already linked to a saved record")
    extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document.id))
    if not extraction:
        raise HTTPException(409, "document extraction is not ready yet")
    output = ExtractionOutputRead.model_validate(extraction.output_json)
    fields = output.fields
    pages = _document_pages(document.id, db)
    supplier, _supplier_created = _resolved_or_created_supplier(
        fields.get("supplier_name", ExtractedFieldRead()).value,
        db,
    )
    currency = fields.get("currency", ExtractedFieldRead()).value
    if not currency:
        raise HTTPException(422, "currency is missing from the extraction")

    if document.document_type == "contract":
        number = fields.get("contract_number", ExtractedFieldRead()).value
        effective_date = fields.get("effective_date", ExtractedFieldRead()).value
        tolerance = fields.get("price_tolerance_percent", ExtractedFieldRead()).value
        if not all([number, effective_date, tolerance]):
            raise HTTPException(422, "contract number, effective date, and tolerance are required")
        existing = db.scalar(select(Contract).where(Contract.supplier_id == supplier.id, Contract.contract_number == number))
        if existing:
            linked_po_count = _link_contract_to_purchase_orders(existing, db)
            document.parent_type, document.parent_id = "contract", existing.id
            db.commit()
            return {"record_type": "contract", "record_id": existing.id, "linked_purchase_orders": linked_po_count, "message": "Existing contract linked to this source document."}
        valid_to_match = re.search(r"\bvalid\s+to\b\s*\n?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", "\n".join(page.text for page in pages), re.IGNORECASE)
        record = Contract(
            supplier_id=supplier.id,
            contract_number=number,
            valid_from=date.fromisoformat(effective_date),
            valid_to=date.fromisoformat(valid_to_match.group(1)) if valid_to_match else None,
            currency=currency.upper(),
            price_tolerance_pct=_decimal_value(tolerance.replace("%", ""), "price tolerance"),
        )
        db.add(record)
        db.flush()
        linked_po_count = _link_contract_to_purchase_orders(record, db)
        document.parent_type, document.parent_id = "contract", record.id
        db.commit()
        return {"record_type": "contract", "record_id": record.id, "linked_purchase_orders": linked_po_count, "message": "Contract saved. Reopen the invoice to refresh readiness."}

    if document.document_type == "purchase_order":
        number = fields.get("purchase_order_number", ExtractedFieldRead()).value
        ordered_on = fields.get("ordered_on", ExtractedFieldRead()).value
        if not number or not ordered_on:
            raise HTTPException(422, "PO number and order date are required")
        existing = db.scalar(select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier.id, PurchaseOrder.po_number == number))
        if existing:
            document.parent_type, document.parent_id = "purchase_order", existing.id
            db.commit()
            return {"record_type": "purchase_order", "record_id": existing.id, "message": "Existing purchase order linked to this source document."}
        item_code, description, quantity, unit_price, _line_total = _source_table_values(pages)
        contract_match = re.search(r"\bcontract\s+number\b\s*\n?\s*([A-Z0-9/-]+)", "\n".join(page.text for page in pages), re.IGNORECASE)
        contract = db.scalar(select(Contract).where(
            Contract.supplier_id == supplier.id,
            Contract.contract_number == contract_match.group(1) if contract_match else "",
        )) if contract_match else None
        record = PurchaseOrder(
            supplier_id=supplier.id,
            contract_id=contract.id if contract else None,
            po_number=number,
            currency=currency.upper(),
            ordered_on=date.fromisoformat(ordered_on),
            lines=[PurchaseOrderLine(
                line_number=1,
                item_code=item_code,
                description=description,
                ordered_quantity=_decimal_value(quantity, "ordered quantity"),
                unit_price=_decimal_value(unit_price, "PO unit price"),
            )],
        )
        db.add(record)
        db.flush()
        document.parent_type, document.parent_id = "purchase_order", record.id
        db.commit()
        return {"record_type": "purchase_order", "record_id": record.id, "message": "Purchase order saved. Reopen the invoice to check the next requirement."}

    po_number = fields.get("purchase_order_number", ExtractedFieldRead()).value
    receipt_number = fields.get("receipt_number", ExtractedFieldRead()).value
    if not po_number or not receipt_number:
        raise HTTPException(422, "receipt and PO numbers are required")
    po = db.scalar(select(PurchaseOrder).where(PurchaseOrder.supplier_id == supplier.id, PurchaseOrder.po_number == po_number))
    if not po:
        raise HTTPException(422, "save the matching purchase order before saving this receipt")
    existing = db.scalar(select(DeliveryReceipt).where(DeliveryReceipt.receipt_number == receipt_number))
    if existing:
        document.parent_type, document.parent_id = "delivery_receipt", existing.id
        db.commit()
        return {"record_type": "delivery_receipt", "record_id": existing.id, "message": "Existing delivery receipt linked to this source document."}
    item_code, _description, received, accepted, _rejected = _source_table_values(pages)
    received_on_match = re.search(r"\breceipt\s+date\b\s*\n?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", "\n".join(page.text for page in pages), re.IGNORECASE)
    if not received_on_match:
        raise HTTPException(422, "receipt date is required")
    po_line = db.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id, PurchaseOrderLine.item_code == item_code))
    record = DeliveryReceipt(
        purchase_order_id=po.id,
        receipt_number=receipt_number,
        received_on=date.fromisoformat(received_on_match.group(1)),
        lines=[DeliveryReceiptLine(
            line_number=1,
            purchase_order_line_id=po_line.id if po_line else None,
            item_code=item_code,
            quantity_received=_decimal_value(received, "received quantity"),
            quantity_accepted=_decimal_value(accepted, "accepted quantity"),
        )],
    )
    db.add(record)
    db.flush()
    document.parent_type, document.parent_id = "delivery_receipt", record.id
    db.commit()
    return {"record_type": "delivery_receipt", "record_id": record.id, "message": "Delivery receipt saved. Reopen the invoice to refresh readiness."}


@app.post(
    "/documents/{document_id}/create-review-case",
    response_model=ReconciliationResultRead,
    status_code=201,
)
def create_review_case_from_document(
    document_id: str,
    db: Annotated[Session, Depends(get_db)],
):
    """Create and reconcile a case from a completed invoice extraction.

    This fast path is for an unambiguous invoice whose supplier and PO already exist
    in the purchasing records. It is intentionally fail-closed: missing fields,
    duplicate invoices, or ambiguous matches are returned for human review.
    """
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    if document.document_type != "invoice":
        raise HTTPException(422, "only an invoice document can create a review case")
    if document.parent_type == "invoice":
        raise HTTPException(409, "this invoice document is already linked to a case")

    extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_id))
    if not extraction:
        raise HTTPException(409, "invoice extraction is not ready yet")
    output = ExtractionOutputRead.model_validate(extraction.output_json)
    fields = output.fields
    supplier_name = fields.get("supplier_name", ExtractedFieldRead()).value
    po_number = fields.get("purchase_order_number", ExtractedFieldRead()).value
    invoice_number = fields.get("invoice_number", ExtractedFieldRead()).value
    invoice_date = fields.get("invoice_date", ExtractedFieldRead()).value
    currency = fields.get("currency", ExtractedFieldRead()).value
    total_amount = fields.get("total_amount", ExtractedFieldRead()).value

    if not all([supplier_name, po_number, invoice_number, invoice_date, currency, total_amount]):
        raise HTTPException(422, "invoice extraction is incomplete; review the extracted fields first")
    suppliers = db.scalars(select(Supplier).order_by(Supplier.id)).all()
    supplier_matches = [item for item in suppliers if _match_key(item.legal_name) == _match_key(supplier_name)]
    if len(supplier_matches) != 1:
        raise HTTPException(422, "supplier could not be resolved to exactly one purchasing record")
    supplier = supplier_matches[0]
    po_matches = db.scalars(
        select(PurchaseOrder)
        .where(PurchaseOrder.supplier_id == supplier.id)
        .order_by(PurchaseOrder.id)
    ).all()
    po_matches = [item for item in po_matches if _match_key(item.po_number) == _match_key(po_number)]
    if len(po_matches) != 1:
        raise HTTPException(422, "purchase order could not be resolved to exactly one purchasing record")
    po = po_matches[0]
    if po.currency != currency.upper():
        raise HTTPException(422, "invoice currency differs from the matched purchase order")
    duplicate = db.scalar(
        select(Invoice).where(
            Invoice.supplier_id == supplier.id,
            Invoice.invoice_number == invoice_number,
        )
    )
    if duplicate:
        raise HTTPException(409, "invoice_number already exists for this supplier")

    pages = list(
        db.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        ).all()
    )
    invoice_line = _invoice_line_from_source(pages)
    try:
        parsed_date = date.fromisoformat(invoice_date)
    except ValueError as exc:
        raise HTTPException(422, "invoice date must use YYYY-MM-DD") from exc
    total = _decimal_value(total_amount, "total amount")
    subtotal = invoice_line.line_amount
    tax_amount = total - subtotal
    if tax_amount < 0:
        raise HTTPException(422, "invoice total is less than the extracted line-item subtotal")

    invoice = Invoice(
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=invoice_number,
        invoice_date=parsed_date,
        currency=currency.upper(),
        subtotal=subtotal,
        tax_amount=tax_amount,
        total_amount=total,
        lines=[invoice_line],
    )
    invoice.case = ReconciliationCase(status="new")
    db.add(invoice)
    db.flush()
    document.parent_type = "invoice"
    document.parent_id = invoice.id
    db.add(AuditEvent(
        case_id=invoice.case.id,
        event_type="invoice_recorded",
        actor="system",
        detail={"source": "document_auto_link", "document_id": document.id},
    ))

    case = db.scalar(
        select(ReconciliationCase)
        .options(
            selectinload(ReconciliationCase.invoice)
            .selectinload(Invoice.purchase_order)
            .selectinload(PurchaseOrder.lines),
            selectinload(ReconciliationCase.invoice)
            .selectinload(Invoice.purchase_order)
            .selectinload(PurchaseOrder.receipts)
            .selectinload(DeliveryReceipt.lines),
            selectinload(ReconciliationCase.invoice)
            .selectinload(Invoice.purchase_order)
            .selectinload(PurchaseOrder.contract),
            selectinload(ReconciliationCase.invoice).selectinload(Invoice.lines),
        )
        .where(ReconciliationCase.id == invoice.case.id)
    )
    assert case is not None
    report = reconcile_invoice(case.invoice)
    case.status = report["status"]
    event = AuditEvent(
        case_id=case.id,
        event_type="reconciliation_completed",
        actor="system",
        detail=report,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return ReconciliationResultRead(
        case_id=case.id,
        invoice_id=case.invoice_id,
        status=report["status"],
        checks=report["checks"],
        exception_count=report["exception_count"],
        created_at=event.created_at,
    )


@app.post(
    "/documents/{document_id}/confirm-match",
    response_model=InvoiceLinkConfirmationRead,
    status_code=201,
)
def confirm_document_record_match(
    document_id: str,
    payload: InvoiceLinkConfirmationRequest,
    db: Annotated[Session, Depends(get_db)],
):
    """Create an invoice/case from reviewer-confirmed values and link its source document."""
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    if document.document_type != "invoice":
        raise HTTPException(422, "only invoice documents can be linked to invoice records")
    if document.parent_type == "invoice":
        raise HTTPException(409, "document is already linked to an invoice")
    extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == document_id))
    if not extraction:
        raise HTTPException(409, "invoice has not been extracted yet")

    supplier = db.get(Supplier, payload.supplier_id)
    if not supplier:
        raise HTTPException(404, "supplier_id does not exist")
    po = db.scalar(
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .where(PurchaseOrder.id == payload.purchase_order_id)
    )
    if not po:
        raise HTTPException(404, "purchase_order_id does not exist")
    if po.supplier_id != payload.supplier_id:
        raise HTTPException(422, "confirmed supplier must match the selected purchase order")
    if po.currency != payload.currency:
        raise HTTPException(422, "invoice currency must match the selected purchase order")

    supplier_invoices = db.scalars(
        select(Invoice).where(Invoice.supplier_id == payload.supplier_id)
    ).all()
    if any(_match_key(invoice.invoice_number) == _match_key(payload.invoice_number) for invoice in supplier_invoices):
        raise HTTPException(409, "invoice_number already exists for this supplier")

    line_sum = sum((line.line_amount for line in payload.lines), start=0)
    if abs(line_sum - payload.subtotal) > 0.02:
        raise HTTPException(422, "subtotal must equal the sum of invoice line amounts within 0.02")
    if abs(payload.subtotal + payload.tax_amount - payload.total_amount) > 0.02:
        raise HTTPException(422, "total_amount must equal subtotal plus tax_amount within 0.02")

    output = ExtractionOutputRead.model_validate(extraction.output_json)
    extracted_invoice_number = output.fields.get("invoice_number", ExtractedFieldRead()).value
    invoice = Invoice(
        **payload.model_dump(exclude={"lines"}),
        lines=[InvoiceLine(**line.model_dump()) for line in payload.lines],
        case=ReconciliationCase(status="new"),
    )
    db.add(invoice)
    db.flush()
    document.parent_type = "invoice"
    document.parent_id = invoice.id
    db.add(AuditEvent(
        case_id=invoice.case.id,
        event_type="invoice_link_confirmed",
        actor="reviewer",
        detail={
            "document_id": document.id,
            "extractor_version": extraction.extractor_version,
            "extracted_invoice_number": extracted_invoice_number,
            "confirmed_invoice_number": invoice.invoice_number,
            "supplier_id": supplier.id,
            "purchase_order_id": po.id,
        },
    ))
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if "uq_supplier_invoice_number" in str(exc):
            raise HTTPException(409, "invoice_number already exists for this supplier") from exc
        raise
    saved_invoice = db.scalar(
        select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == invoice.id)
    )
    return InvoiceLinkConfirmationRead(
        document_id=document.id,
        invoice_id=invoice.id,
        case_id=invoice.case.id,
        invoice=saved_invoice,
    )


@app.post("/documents/{document_id}/compare-extractors", response_model=ExtractorComparisonRead)
def compare_document_extractors(
    document_id: str,
    db: Annotated[Session, Depends(get_db)],
    include_local: bool = False,
):
    """Compare rules and Groq, optionally including the local QLoRA adapter."""
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(404, "document not found")
    if document.document_type != "invoice":
        raise HTTPException(422, "extractor comparison currently supports invoices only")
    stored_pages = list(db.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_number)
    ).all())
    if not stored_pages or not any(page.text.strip() for page in stored_pages):
        raise HTTPException(409, "document text is not ready; wait for its processing job to complete")

    pages = [
        ExtractedPage(page_number=page.page_number, text=page.text, method=page.extraction_method)
        for page in stored_pages
    ]
    rules_output = rules_extract("invoice", pages)
    try:
        llm_result = extract_invoice_with_llm(pages)
    except LLMConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except LLMRequestError as exc:
        raise HTTPException(502, str(exc)) from exc

    def comparable(value: str | None, field_name: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if field_name == "total_amount":
            normalized = normalized.replace(",", "").replace("$", "").replace(" ", "")
        return normalized

    field_names = set(rules_output.fields) | set(llm_result.output.fields)
    agreement = {
        name: comparable(rules_output.fields.get(name, ExtractedFieldRead()).value, name)
        == comparable(llm_result.output.fields.get(name, ExtractedFieldRead()).value, name)
        for name in sorted(field_names)
    }
    local_result = None
    local_agreement = None
    if include_local:
        try:
            local_result = extract_invoice_with_local_model(pages)
        except LocalModelConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except LocalModelRequestError as exc:
            raise HTTPException(502, str(exc)) from exc
        local_agreement = {
            name: comparable(
                rules_output.fields.get(name, ExtractedFieldRead()).value,
                name,
            ) == comparable(
                local_result.output.fields.get(name, ExtractedFieldRead()).value,
                name,
            )
            for name in sorted(set(rules_output.fields) | set(local_result.output.fields))
        }
    return ExtractorComparisonRead(
        document_id=document_id,
        provider=llm_result.provider,
        model=llm_result.model,
        rules_output=rules_output,
        llm_output=llm_result.output,
        field_agreement=agreement,
        llm_notes=llm_result.notes,
        local_model=local_result.model if local_result else None,
        local_output=local_result.output if local_result else None,
        local_field_agreement=local_agreement,
        local_notes=local_result.notes if local_result else [],
    )


@app.get("/document-jobs", response_model=list[DocumentJobRead])
def list_document_jobs(db: Annotated[Session, Depends(get_db)]):
    return list(db.scalars(select(DocumentProcessingJob).order_by(DocumentProcessingJob.created_at.desc())).all())


@app.get("/document-jobs/{job_id}", response_model=DocumentJobRead)
def get_document_job(job_id: str, db: Annotated[Session, Depends(get_db)]):
    job = db.get(DocumentProcessingJob, job_id)
    if not job:
        raise HTTPException(404, "document processing job not found")
    return job


@app.post("/document-jobs/{job_id}/retry", response_model=DocumentJobRead, status_code=202)
def retry_document_job(job_id: str, db: Annotated[Session, Depends(get_db)]):
    """Re-run extraction after installing OCR or changing an extractor version."""
    job = db.get(DocumentProcessingJob, job_id)
    if not job:
        raise HTTPException(404, "document processing job not found")
    if job.status == "processing":
        raise HTTPException(409, "document processing job is already running")
    if job.attempt_count >= MAX_JOB_ATTEMPTS:
        raise HTTPException(409, f"document processing job reached the {MAX_JOB_ATTEMPTS}-attempt limit")
    job.status = "queued"
    job.error = None
    job.started_at = None
    job.completed_at = None
    db.commit()
    db.refresh(job)
    return job


@app.post("/suppliers", response_model=SupplierRead, status_code=201)
def create_supplier(payload: SupplierCreate, db: Annotated[Session, Depends(get_db)]):
    if db.scalar(select(Supplier).where(Supplier.supplier_code == payload.supplier_code)):
        raise HTTPException(409, "supplier_code already exists")
    row = Supplier(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.get("/suppliers", response_model=list[SupplierRead])
def list_suppliers(db: Annotated[Session, Depends(get_db)]):
    return list(db.scalars(select(Supplier).order_by(Supplier.supplier_code)).all())


@app.post("/contracts", response_model=ContractRead, status_code=201)
def create_contract(payload: ContractCreate, db: Annotated[Session, Depends(get_db)]):
    if not db.get(Supplier, payload.supplier_id):
        raise HTTPException(404, "supplier_id does not exist")
    row = Contract(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if "uq_supplier_contract_number" in str(exc):
            raise HTTPException(409, "contract_number already exists for this supplier") from exc
        raise
    db.refresh(row)
    return row


@app.get("/contracts", response_model=list[ContractRead])
def list_contracts(db: Annotated[Session, Depends(get_db)]):
    """Inspect stored contracts during the manual-data phase."""
    return list(db.scalars(select(Contract).order_by(Contract.contract_number)).all())


@app.post("/purchase-orders", response_model=PurchaseOrderRead, status_code=201)
def create_purchase_order(payload: PurchaseOrderCreate, db: Annotated[Session, Depends(get_db)]):
    supplier = db.get(Supplier, payload.supplier_id)
    if not supplier:
        raise HTTPException(404, "supplier_id does not exist")
    contract = db.get(Contract, payload.contract_id) if payload.contract_id else None
    if payload.contract_id and not contract:
        raise HTTPException(404, "contract_id does not exist")
    if contract and contract.supplier_id != payload.supplier_id:
        raise HTTPException(422, "contract supplier must match purchase-order supplier")
    if contract and contract.currency != payload.currency:
        raise HTTPException(422, "contract and purchase-order currency must match")
    lines = [PurchaseOrderLine(**line.model_dump()) for line in payload.lines]
    row = PurchaseOrder(**payload.model_dump(exclude={"lines"}), lines=lines)
    db.add(row)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if "uq_supplier_po_number" in str(exc):
            raise HTTPException(409, "po_number already exists for this supplier") from exc
        raise
    return db.scalar(
        select(PurchaseOrder).options(selectinload(PurchaseOrder.lines)).where(PurchaseOrder.id == row.id)
    )


@app.get("/purchase-orders", response_model=list[PurchaseOrderRead])
def list_purchase_orders(db: Annotated[Session, Depends(get_db)]):
    """Inspect POs and their line items."""
    statement = select(PurchaseOrder).options(selectinload(PurchaseOrder.lines)).order_by(PurchaseOrder.po_number)
    return list(db.scalars(statement).all())


@app.post("/receipts", response_model=ReceiptRead, status_code=201)
def create_receipt(payload: ReceiptCreate, db: Annotated[Session, Depends(get_db)]):
    po = db.scalar(select(PurchaseOrder).options(selectinload(PurchaseOrder.lines)).where(PurchaseOrder.id == payload.purchase_order_id))
    if not po:
        raise HTTPException(404, "purchase_order_id does not exist")
    po_lines = {line.id: line for line in po.lines}
    for line in payload.lines:
        if line.purchase_order_line_id:
            po_line = po_lines.get(line.purchase_order_line_id)
            if not po_line:
                raise HTTPException(422, "receipt line must reference a line from its purchase order")
            if line.item_code != po_line.item_code:
                raise HTTPException(422, "receipt item_code does not match the referenced purchase-order line")
    row = DeliveryReceipt(
        purchase_order_id=payload.purchase_order_id,
        receipt_number=payload.receipt_number,
        received_on=payload.received_on,
        lines=[DeliveryReceiptLine(**line.model_dump()) for line in payload.lines],
    )
    db.add(row)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if "receipt_number" in str(exc):
            raise HTTPException(409, "receipt_number already exists") from exc
        raise
    return db.scalar(
        select(DeliveryReceipt).options(selectinload(DeliveryReceipt.lines)).where(DeliveryReceipt.id == row.id)
    )


@app.get("/receipts", response_model=list[ReceiptRead])
def list_receipts(db: Annotated[Session, Depends(get_db)]):
    """Inspect receipt quantities before reconciliation is implemented."""
    statement = select(DeliveryReceipt).options(selectinload(DeliveryReceipt.lines)).order_by(DeliveryReceipt.receipt_number)
    return list(db.scalars(statement).all())


@app.post("/invoices", response_model=InvoiceRead, status_code=201)
def create_invoice(payload: InvoiceCreate, db: Annotated[Session, Depends(get_db)]):
    supplier = db.get(Supplier, payload.supplier_id)
    if not supplier:
        raise HTTPException(404, "supplier_id does not exist")
    if payload.purchase_order_id:
        po = db.get(PurchaseOrder, payload.purchase_order_id)
        if not po:
            raise HTTPException(404, "purchase_order_id does not exist")
        if po.supplier_id != payload.supplier_id:
            raise HTTPException(422, "invoice supplier must match purchase-order supplier")
    line_sum = sum((line.line_amount for line in payload.lines), start=0)
    if abs(line_sum - payload.subtotal) > 0.02:
        raise HTTPException(422, "subtotal must equal the sum of invoice line amounts within 0.02")
    if abs(payload.subtotal + payload.tax_amount - payload.total_amount) > 0.02:
        raise HTTPException(422, "total_amount must equal subtotal plus tax_amount within 0.02")
    row = Invoice(
        **payload.model_dump(exclude={"lines"}),
        lines=[InvoiceLine(**line.model_dump()) for line in payload.lines],
    )
    row.case = ReconciliationCase(status="new")
    db.add(row)
    db.flush()
    db.add(AuditEvent(case=row.case, event_type="invoice_recorded", actor="api", detail={"source": "manual_phase1"}))
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if "uq_supplier_invoice_number" in str(exc):
            raise HTTPException(409, "invoice_number already exists for this supplier") from exc
        raise
    return db.scalar(select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == row.id))


@app.get("/invoices", response_model=list[InvoiceRead])
def list_invoices(db: Annotated[Session, Depends(get_db)]):
    """Inspect stored invoice values and line items."""
    statement = select(Invoice).options(selectinload(Invoice.lines)).order_by(Invoice.invoice_number)
    return list(db.scalars(statement).all())


@app.get("/cases", response_model=list[CaseRead])
def list_cases(db: Annotated[Session, Depends(get_db)]):
    rows = db.scalars(
        select(ReconciliationCase)
        .options(
            selectinload(ReconciliationCase.events),
            selectinload(ReconciliationCase.invoice).selectinload(Invoice.lines),
        )
        .order_by(ReconciliationCase.id)
    ).all()
    return rows


@app.get("/cases/{case_id}", response_model=CaseRead)
def get_case(case_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.scalar(
        select(ReconciliationCase)
        .options(
            selectinload(ReconciliationCase.events),
            selectinload(ReconciliationCase.invoice).selectinload(Invoice.lines),
        )
        .where(ReconciliationCase.id == case_id)
    )
    if not row:
        raise HTTPException(404, "case not found")
    return row


def _invoice_audit_snapshot(invoice: Invoice) -> dict:
    return {
        "subtotal": str(invoice.subtotal),
        "tax_amount": str(invoice.tax_amount),
        "total_amount": str(invoice.total_amount),
        "lines": [
            {
                "line_number": line.line_number,
                "item_code": line.item_code,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "line_amount": str(line.line_amount),
            }
            for line in invoice.lines
        ],
    }


@app.post("/cases/{case_id}/correct-invoice", response_model=CaseInvoiceCorrectionRead, status_code=201)
def correct_case_invoice(
    case_id: int,
    payload: CaseInvoiceCorrectionRequest,
    db: Annotated[Session, Depends(get_db)],
):
    """Save reviewer-corrected numeric invoice values and require fresh reconciliation."""
    row = db.scalar(
        select(ReconciliationCase)
        .options(selectinload(ReconciliationCase.invoice).selectinload(Invoice.lines))
        .where(ReconciliationCase.id == case_id)
    )
    if not row:
        raise HTTPException(404, "case not found")
    if row.status in {"approved", "rejected"}:
        raise HTTPException(409, "a final case cannot be corrected")
    line_sum = sum((line.line_amount for line in payload.lines), start=0)
    if abs(line_sum - payload.subtotal) > 0.02:
        raise HTTPException(422, "subtotal must equal the sum of corrected line amounts within 0.02")
    if abs(payload.subtotal + payload.tax_amount - payload.total_amount) > 0.02:
        raise HTTPException(422, "total_amount must equal corrected subtotal plus tax_amount within 0.02")

    invoice = row.invoice
    before = _invoice_audit_snapshot(invoice)
    invoice.subtotal = payload.subtotal
    invoice.tax_amount = payload.tax_amount
    invoice.total_amount = payload.total_amount
    invoice.lines.clear()
    db.flush()
    invoice.lines.extend(InvoiceLine(**line.model_dump()) for line in payload.lines)
    after = _invoice_audit_snapshot(invoice)
    row.status = "new"
    event = AuditEvent(
        case_id=row.id,
        event_type="invoice_corrected",
        actor=payload.actor.strip(),
        detail={"reason": payload.reason.strip(), "before": before, "after": after},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    saved_invoice = db.scalar(
        select(Invoice).options(selectinload(Invoice.lines)).where(Invoice.id == invoice.id)
    )
    return CaseInvoiceCorrectionRead(
        case_id=row.id,
        invoice=saved_invoice,
        status="new",
        event_id=event.id,
        created_at=event.created_at,
    )


@app.post("/cases/{case_id}/decision", response_model=CaseDecisionRead, status_code=201)
def decide_case(
    case_id: int,
    payload: CaseDecisionRequest,
    db: Annotated[Session, Depends(get_db)],
):
    """Record a reviewer approval or rejection after reconciliation."""
    row = db.scalar(
        select(ReconciliationCase)
        .options(selectinload(ReconciliationCase.events))
        .where(ReconciliationCase.id == case_id)
    )
    if not row:
        raise HTTPException(404, "case not found")
    if row.status in {"approved", "rejected"}:
        raise HTTPException(409, "case already has a final decision")

    last_report = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.case_id == case_id,
            AuditEvent.event_type == "reconciliation_completed",
        )
        .order_by(AuditEvent.id.desc())
    )
    if not last_report:
        raise HTTPException(409, "reconcile the case before recording a decision")
    last_correction = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.case_id == case_id, AuditEvent.event_type == "invoice_corrected")
        .order_by(AuditEvent.id.desc())
    )
    if last_correction and last_correction.id > last_report.id:
        raise HTTPException(409, "reconcile the corrected invoice before recording a decision")
    exception_count = int(last_report.detail.get("exception_count", 0))
    if payload.decision == "approved" and exception_count and not payload.override_blocking_exceptions:
        raise HTTPException(
            422,
            "approval is blocked because reconciliation has blocking exceptions; reject or provide an override reason",
        )

    row.status = payload.decision
    event_type = (
        "case_approved_with_override"
        if payload.decision == "approved" and payload.override_blocking_exceptions
        else f"case_{payload.decision}"
    )
    event = AuditEvent(
        case_id=row.id,
        event_type=event_type,
        actor=payload.actor.strip(),
        detail={
            "decision": payload.decision,
            "reason": payload.reason.strip() if payload.reason else None,
            "override_blocking_exceptions": payload.override_blocking_exceptions,
            "blocking_exception_count": exception_count,
            "reconciliation_event_id": last_report.id,
        },
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return CaseDecisionRead(
        case_id=row.id,
        status=row.status,
        decision=payload.decision,
        override_blocking_exceptions=payload.override_blocking_exceptions,
        event_id=event.id,
        created_at=event.created_at,
    )


@app.post("/cases/{case_id}/reconcile", response_model=ReconciliationResultRead)
def reconcile_case(case_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.scalar(
        select(ReconciliationCase)
        .options(
            selectinload(ReconciliationCase.invoice)
            .selectinload(Invoice.purchase_order)
            .selectinload(PurchaseOrder.lines),
            selectinload(ReconciliationCase.invoice)
            .selectinload(Invoice.purchase_order)
            .selectinload(PurchaseOrder.receipts)
            .selectinload(DeliveryReceipt.lines),
            selectinload(ReconciliationCase.invoice)
            .selectinload(Invoice.purchase_order)
            .selectinload(PurchaseOrder.contract),
            selectinload(ReconciliationCase.invoice).selectinload(Invoice.lines),
        )
        .where(ReconciliationCase.id == case_id)
    )
    if not row:
        raise HTTPException(404, "case not found")

    report = reconcile_invoice(row.invoice)
    event = AuditEvent(
        case_id=row.id,
        event_type="reconciliation_completed",
        actor="system",
        detail=report,
    )
    row.status = report["status"]
    db.add(event)
    db.commit()
    db.refresh(event)
    return ReconciliationResultRead(
        case_id=row.id,
        invoice_id=row.invoice_id,
        status=report["status"],
        checks=report["checks"],
        exception_count=report["exception_count"],
        created_at=event.created_at,
    )
