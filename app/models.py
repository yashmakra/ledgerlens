from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    legal_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contracts: Mapped[list["Contract"]] = relationship(back_populates="supplier")
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="supplier")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="supplier")


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (
        UniqueConstraint("supplier_id", "contract_number", name="uq_supplier_contract_number"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_contract_date_range"),
        CheckConstraint("price_tolerance_pct >= 0", name="ck_contract_nonnegative_tolerance"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    contract_number: Mapped[str] = mapped_column(String(100))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    price_tolerance_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(0))
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terms_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    supplier: Mapped[Supplier] = relationship(back_populates="contracts")
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="contract")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("supplier_id", "po_number", name="uq_supplier_po_number"),
        CheckConstraint("currency = upper(currency)", name="ck_po_currency_uppercase"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=True)
    po_number: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    ordered_on: Mapped[date]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    supplier: Mapped[Supplier] = relationship(back_populates="purchase_orders")
    contract: Mapped[Contract | None] = relationship(back_populates="purchase_orders")
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(back_populates="purchase_order", cascade="all, delete-orphan")
    receipts: Mapped[list["DeliveryReceipt"]] = relationship(back_populates="purchase_order")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="purchase_order")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "line_number", name="uq_po_line_number"),
        CheckConstraint("ordered_quantity > 0", name="ck_po_positive_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_po_nonnegative_price"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    line_number: Mapped[int] = mapped_column(Integer)
    item_code: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(String(500))
    ordered_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4))

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    receipt_lines: Mapped[list["DeliveryReceiptLine"]] = relationship(back_populates="purchase_order_line")


class DeliveryReceipt(Base):
    __tablename__ = "delivery_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="RESTRICT"), index=True)
    receipt_number: Mapped[str] = mapped_column(String(100), unique=True)
    received_on: Mapped[date]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="receipts")
    lines: Mapped[list["DeliveryReceiptLine"]] = relationship(back_populates="receipt", cascade="all, delete-orphan")


class DeliveryReceiptLine(Base):
    __tablename__ = "delivery_receipt_lines"
    __table_args__ = (
        UniqueConstraint("receipt_id", "line_number", name="uq_receipt_line_number"),
        CheckConstraint("quantity_received >= 0", name="ck_receipt_nonnegative_received"),
        CheckConstraint("quantity_accepted >= 0", name="ck_receipt_nonnegative_accepted"),
        CheckConstraint("quantity_accepted <= quantity_received", name="ck_receipt_accepted_lte_received"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("delivery_receipts.id", ondelete="CASCADE"), index=True)
    purchase_order_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=True)
    line_number: Mapped[int] = mapped_column(Integer)
    item_code: Mapped[str] = mapped_column(String(100), index=True)
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    quantity_accepted: Mapped[Decimal] = mapped_column(Numeric(14, 4))

    receipt: Mapped[DeliveryReceipt] = relationship(back_populates="lines")
    purchase_order_line: Mapped[PurchaseOrderLine | None] = relationship(back_populates="receipt_lines")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("supplier_id", "invoice_number", name="uq_supplier_invoice_number"),
        CheckConstraint("currency = upper(currency)", name="ck_invoice_currency_uppercase"),
        CheckConstraint("subtotal >= 0 AND tax_amount >= 0 AND total_amount >= 0", name="ck_invoice_nonnegative_amounts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=True, index=True)
    invoice_number: Mapped[str] = mapped_column(String(100))
    invoice_date: Mapped[date]
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    supplier: Mapped[Supplier] = relationship(back_populates="invoices")
    purchase_order: Mapped[PurchaseOrder | None] = relationship(back_populates="invoices")
    lines: Mapped[list["InvoiceLine"]] = relationship(back_populates="invoice", cascade="all, delete-orphan")
    case: Mapped["ReconciliationCase | None"] = relationship(back_populates="invoice", uselist=False, cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_number", name="uq_invoice_line_number"),
        CheckConstraint("quantity > 0", name="ck_invoice_positive_quantity"),
        CheckConstraint("unit_price >= 0 AND line_amount >= 0", name="ck_invoice_nonnegative_line_amounts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    line_number: Mapped[int] = mapped_column(Integer)
    item_code: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    line_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class ReconciliationCase(Base):
    __tablename__ = "reconciliation_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    invoice: Mapped[Invoice] = relationship(back_populates="case")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_cases.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(100), default="system")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    case: Mapped[ReconciliationCase] = relationship(back_populates="events")


class Document(Base):
    """Immutable uploaded source file. Extracted data stays in related records."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_type: Mapped[str] = mapped_column(String(32), index=True)
    parent_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100))
    storage_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list["DocumentProcessingJob"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    uploads: Mapped[list["DocumentUpload"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    pages: Mapped[list["DocumentPage"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    extraction: Mapped["DocumentExtraction | None"] = relationship(
        back_populates="document", uselist=False, cascade="all, delete-orphan"
    )


class DocumentUpload(Base):
    """One upload attempt. Several names can refer to one byte-identical document."""

    __tablename__ = "document_uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="uploads")


class DocumentProcessingJob(Base):
    """Durable work record. Jobs are processed by a separate worker process."""

    __tablename__ = "document_processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="jobs")


class DocumentPage(Base):
    """Text retained per page so every extracted field can cite its source."""

    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_document_page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    extraction_method: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="pages")


class DocumentExtraction(Base):
    """One latest schema-validated extraction result per source document."""

    __tablename__ = "document_extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True, index=True)
    extractor_version: Mapped[str] = mapped_column(String(100))
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="extraction")
