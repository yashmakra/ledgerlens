from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SupplierCreate(BaseModel):
    supplier_code: str = Field(min_length=1, max_length=50)
    legal_name: str = Field(min_length=1, max_length=255)


class SupplierRead(APIModel):
    id: int
    supplier_code: str
    legal_name: str
    created_at: datetime


class ContractCreate(BaseModel):
    supplier_id: int = Field(gt=0)
    contract_number: str = Field(min_length=1, max_length=100)
    valid_from: date
    valid_to: date | None = None
    currency: str = Field(min_length=3, max_length=3)
    price_tolerance_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0, le=3650)
    terms_summary: str | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def check_dates(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be on or after valid_from")
        return self


class ContractRead(APIModel):
    id: int
    supplier_id: int
    contract_number: str
    valid_from: date
    valid_to: date | None
    currency: str
    price_tolerance_pct: Decimal
    payment_terms_days: int | None
    terms_summary: str | None


class PurchaseOrderLineCreate(BaseModel):
    line_number: int = Field(gt=0)
    item_code: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    ordered_quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class PurchaseOrderCreate(BaseModel):
    supplier_id: int = Field(gt=0)
    contract_id: int | None = Field(default=None, gt=0)
    po_number: str = Field(min_length=1, max_length=100)
    currency: str = Field(min_length=3, max_length=3)
    ordered_on: date
    lines: list[PurchaseOrderLineCreate] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class PurchaseOrderLineRead(APIModel):
    id: int
    line_number: int
    item_code: str
    description: str
    ordered_quantity: Decimal
    unit_price: Decimal


class PurchaseOrderRead(APIModel):
    id: int
    supplier_id: int
    contract_id: int | None
    po_number: str
    currency: str
    ordered_on: date
    lines: list[PurchaseOrderLineRead]


class ReceiptLineCreate(BaseModel):
    line_number: int = Field(gt=0)
    purchase_order_line_id: int | None = Field(default=None, gt=0)
    item_code: str = Field(min_length=1, max_length=100)
    quantity_received: Decimal = Field(ge=0)
    quantity_accepted: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def accepted_not_more_than_received(self):
        if self.quantity_accepted > self.quantity_received:
            raise ValueError("quantity_accepted cannot exceed quantity_received")
        return self


class ReceiptCreate(BaseModel):
    purchase_order_id: int = Field(gt=0)
    receipt_number: str = Field(min_length=1, max_length=100)
    received_on: date
    lines: list[ReceiptLineCreate] = Field(min_length=1)


class ReceiptLineRead(APIModel):
    id: int
    line_number: int
    purchase_order_line_id: int | None
    item_code: str
    quantity_received: Decimal
    quantity_accepted: Decimal


class ReceiptRead(APIModel):
    id: int
    purchase_order_id: int
    receipt_number: str
    received_on: date
    lines: list[ReceiptLineRead]


class InvoiceLineCreate(BaseModel):
    line_number: int = Field(gt=0)
    item_code: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    line_amount: Decimal = Field(ge=0)


class InvoiceCreate(BaseModel):
    supplier_id: int = Field(gt=0)
    purchase_order_id: int | None = Field(default=None, gt=0)
    invoice_number: str = Field(min_length=1, max_length=100)
    invoice_date: date
    currency: str = Field(min_length=3, max_length=3)
    subtotal: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    lines: list[InvoiceLineCreate] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class InvoiceLineRead(APIModel):
    id: int
    line_number: int
    item_code: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_amount: Decimal


class InvoiceRead(APIModel):
    id: int
    supplier_id: int
    purchase_order_id: int | None
    invoice_number: str
    invoice_date: date
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    lines: list[InvoiceLineRead]


class InvoiceLinkConfirmationRequest(InvoiceCreate):
    purchase_order_id: int = Field(gt=0)


class InvoiceLinkConfirmationRead(BaseModel):
    document_id: str
    invoice_id: int
    case_id: int
    invoice: InvoiceRead


class AuditEventRead(APIModel):
    id: int
    event_type: str
    actor: str
    detail: dict
    created_at: datetime


class CaseRead(APIModel):
    id: int
    invoice_id: int
    status: str
    invoice: InvoiceRead
    events: list[AuditEventRead] = Field(default_factory=list)


class CaseInvoiceCorrectionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    subtotal: Decimal = Field(ge=0)
    tax_amount: Decimal = Field(ge=0)
    total_amount: Decimal = Field(ge=0)
    lines: list[InvoiceLineCreate] = Field(min_length=1)


class CaseInvoiceCorrectionRead(BaseModel):
    case_id: int
    invoice: InvoiceRead
    status: Literal["new"]
    event_id: int
    created_at: datetime


class CaseDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    actor: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)
    override_blocking_exceptions: bool = False

    @model_validator(mode="after")
    def require_reason_when_needed(self):
        if (self.decision == "rejected" or self.override_blocking_exceptions) and not self.reason:
            raise ValueError("reason is required when rejecting or overriding blocking exceptions")
        if self.decision == "rejected" and self.override_blocking_exceptions:
            raise ValueError("override_blocking_exceptions only applies to approval")
        return self


class CaseDecisionRead(BaseModel):
    case_id: int
    status: Literal["approved", "rejected"]
    decision: Literal["approved", "rejected"]
    override_blocking_exceptions: bool
    event_id: int
    created_at: datetime


class ReconciliationCheckRead(BaseModel):
    code: str
    status: Literal["pass", "exception"]
    severity: Literal["info", "blocking"]
    message: str
    source_records: list[str] = Field(default_factory=list)
    expected: str | None = None
    actual: str | None = None
    tolerance: str | None = None
    invoice_line_number: int | None = None


class ReconciliationResultRead(BaseModel):
    case_id: int
    invoice_id: int
    status: Literal["matched", "needs_review"]
    checks: list[ReconciliationCheckRead]
    exception_count: int
    created_at: datetime


class SupplierMatchCandidateRead(BaseModel):
    id: int
    supplier_code: str
    legal_name: str


class PurchaseOrderMatchCandidateRead(BaseModel):
    id: int
    po_number: str
    supplier_id: int
    currency: str


class DocumentRecordMatchRead(BaseModel):
    document_id: str
    extractor_version: str
    status: Literal["matched", "needs_review"]
    ready_to_link: bool
    invoice_number: str | None
    duplicate_invoice_id: int | None
    supplier_value: str | None
    supplier_status: Literal["matched", "missing", "not_found", "ambiguous"]
    supplier_id: int | None
    supplier_candidates: list[SupplierMatchCandidateRead]
    purchase_order_value: str | None
    purchase_order_status: Literal[
        "matched", "missing", "not_found", "ambiguous", "supplier_mismatch"
    ]
    purchase_order_id: int | None
    purchase_order_candidates: list[PurchaseOrderMatchCandidateRead]
    notes: list[str]


DocumentType = Literal["invoice", "purchase_order", "delivery_receipt", "contract"]
ParentType = Literal["invoice", "purchase_order", "delivery_receipt", "contract"]


class DocumentRead(APIModel):
    id: str
    document_type: DocumentType
    parent_type: ParentType | None
    parent_id: int | None
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class DocumentJobRead(APIModel):
    id: str
    document_id: str
    status: Literal["queued", "processing", "complete", "failed"]
    attempt_count: int
    metadata_json: dict
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class DocumentUploadAttemptRead(APIModel):
    id: str
    document_id: str
    original_filename: str
    uploaded_at: datetime


class DocumentUploadRead(BaseModel):
    document: DocumentRead
    job: DocumentJobRead
    upload: DocumentUploadAttemptRead
    duplicate_upload: bool


class EvidenceRead(BaseModel):
    page: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=1000)


class ExtractedFieldRead(BaseModel):
    value: str | None = None
    evidence: list[EvidenceRead] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ExtractedLineItemRead(BaseModel):
    description: str
    item_code: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None
    evidence: list[EvidenceRead] = Field(default_factory=list)


class ExtractionOutputRead(BaseModel):
    document_type: DocumentType
    fields: dict[str, ExtractedFieldRead]
    line_items: list[ExtractedLineItemRead] = Field(default_factory=list)


class DocumentPageRead(APIModel):
    page_number: int
    extraction_method: str
    text: str


class DocumentExtractionRead(APIModel):
    id: str
    document_id: str
    extractor_version: str
    output_json: ExtractionOutputRead
    notes: list[str]
    created_at: datetime
    updated_at: datetime


class ExtractorComparisonRead(BaseModel):
    document_id: str
    provider: str
    model: str
    rules_output: ExtractionOutputRead
    llm_output: ExtractionOutputRead
    field_agreement: dict[str, bool]
    llm_notes: list[str]
    local_model: str | None = None
    local_output: ExtractionOutputRead | None = None
    local_field_agreement: dict[str, bool] | None = None
    local_notes: list[str] = Field(default_factory=list)
