"""Phase 3 extraction pipeline.

The baseline reads text and uses conservative rules. It only emits a value when that
exact value appears in a stored page, so later LLM extraction can be compared fairly.
"""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
import pytesseract
from PIL import Image, ImageOps

from app.schemas import EvidenceRead, ExtractedFieldRead, ExtractionOutputRead

EXTRACTOR_VERSION = "rules-baseline-v1"
GROQ_FALLBACK_VERSION = "groq-fallback-v1"


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    method: str


@dataclass
class ExtractionRun:
    pages: list[ExtractedPage]
    output: ExtractionOutputRead
    notes: list[str]
    extractor_version: str = EXTRACTOR_VERSION


def _ocr_image(image: Image.Image) -> str:
    executable = shutil.which("tesseract")
    if not executable:
        configured_path = os.getenv("TESSERACT_CMD")
        candidates = [Path(configured_path)] if configured_path else []
        program_files = Path(os.getenv("ProgramFiles", r"C:\Program Files"))
        candidates.append(program_files / "Tesseract-OCR" / "tesseract.exe")
        executable_path = next((path for path in candidates if path.is_file()), None)
        executable = str(executable_path) if executable_path else None
    if not executable:
        raise RuntimeError(
            "OCR unavailable: install Tesseract, add it to PATH, or set TESSERACT_CMD"
        )
    pytesseract.pytesseract.tesseract_cmd = executable
    # Light normalization improves scans while preserving the original page image.
    prepared = ImageOps.autocontrast(image.convert("L"))
    return pytesseract.image_to_string(prepared).strip()


def read_pages(path: str, media_type: str) -> tuple[list[ExtractedPage], list[str]]:
    source = Path(path)
    notes: list[str] = []
    if media_type == "application/pdf":
        pages: list[ExtractedPage] = []
        with fitz.open(source) as pdf:
            for page_number, page in enumerate(pdf, start=1):
                text = page.get_text("text").strip()
                method = "pdf_text"
                if not text:
                    try:
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                        image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        text = _ocr_image(image)
                        method = "ocr"
                    except RuntimeError as exc:
                        notes.append(f"Page {page_number}: {exc}")
                        method = "no_extractable_text"
                pages.append(ExtractedPage(page_number, text, method))
        return pages, notes
    if media_type.startswith("image/"):
        with Image.open(source) as image:
            return [ExtractedPage(1, _ocr_image(image), "ocr")], notes
    raise ValueError(f"Unsupported media type: {media_type}")


def _find_evidence(value: str, pages: list[ExtractedPage]) -> list[EvidenceRead]:
    for page in pages:
        match = re.search(re.escape(value), page.text, flags=re.IGNORECASE)
        if match:
            return [EvidenceRead(page=page.page_number, text=match.group(0))]
    return []


def _field(value: str | None, pages: list[ExtractedPage]) -> ExtractedFieldRead:
    if not value:
        return ExtractedFieldRead()
    evidence = _find_evidence(value, pages)
    return ExtractedFieldRead(value=value, evidence=evidence, confidence=0.6 if evidence else None)


def _first_nonempty_line(pages: list[ExtractedPage]) -> str | None:
    ignored = {"invoice", "purchase order", "delivery receipt", "goods receipt", "contract"}
    for page in pages:
        for line in page.text.splitlines():
            candidate = line.strip()
            if candidate and candidate.casefold() not in ignored:
                return candidate[:255]
    return None


def _supplier_name(pages: list[ExtractedPage]) -> str | None:
    candidate = _first_nonempty_line(pages)
    if not candidate:
        return None
    # OCR often joins the company name with the large document heading.
    candidate = re.sub(r"\s+(?:invoice|purchase\s+order|delivery\s+receipt|contract)\s*$", "", candidate, flags=re.IGNORECASE)
    return candidate.strip() or None


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _invoice_total(text: str) -> str | None:
    amount = r"(?:USD|EUR|GBP|INR)?\s*\$?([0-9][\d,]*\.\d{2})"
    total = _first_match(rf"\b(?:grand\s+total|amount\s+due|total\s+due|total)\b\s*[:$ ]+{amount}", text)
    if total:
        return total
    # Tesseract may drop the first character in TOTAL (for example, TAL DUE).
    total = _first_match(rf"\b(?:tal|totl|total)\s+due\b\s*[:$ ]*{amount}", text)
    if total:
        return total
    # A zero-tax invoice may safely use its subtotal as the total candidate.
    subtotal = _first_match(r"\bsubtotal\b\s*[:$ ]+\$?([0-9][\d,]*\.\d{2})", text)
    tax = _first_match(r"\btax\b\s*[:$ ]+\$?([0-9][\d,]*\.\d{2})", text)
    if subtotal and tax and tax.replace(",", "") in {"0.00", "0"}:
        return subtotal
    return None


def rules_extract(document_type: str, pages: list[ExtractedPage]) -> ExtractionOutputRead:
    text = "\n".join(page.text for page in pages)
    fields: dict[str, ExtractedFieldRead] = {}
    supplier = _supplier_name(pages)
    fields["supplier_name"] = _field(supplier, pages)
    fields["currency"] = _field(
        "USD" if re.search(r"\bUSD\b|\$", text, re.IGNORECASE) else _first_match(r"\b(EUR|GBP|INR)\b", text),
        pages,
    )
    if document_type == "invoice":
        fields.update({
            "invoice_number": _field(_first_match(r"\binvoice\s*(?:number|no\.?|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})", text), pages),
            "invoice_date": _field(_first_match(r"\b(?:invoice\s+date|date)\s*[:#-]?\s*([0-9]{1,4}[/-][0-9]{1,2}[/-][0-9]{1,4})", text), pages),
            "purchase_order_number": _field(_first_match(r"\b(?:purchase\s*order|P\.?O\.?)\s*(?:number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})", text), pages),
            "total_amount": _field(_invoice_total(text), pages),
        })
    elif document_type == "purchase_order":
        fields.update({
            "purchase_order_number": _field(_first_match(r"\b(?:purchase\s*order|P\.?O\.?)\s*(?:number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})", text), pages),
            "ordered_on": _field(_first_match(r"\b(?:order\s+date|date)\s*[:#-]?\s*([0-9]{1,4}[/-][0-9]{1,2}[/-][0-9]{1,4})", text), pages),
        })
    elif document_type == "delivery_receipt":
        fields.update({
            "receipt_number": _field(_first_match(r"\b(?:delivery|goods)?\s*receipt\s*(?:number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})", text), pages),
            "purchase_order_number": _field(_first_match(r"\b(?:purchase\s*order|P\.?O\.?)\s*(?:number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})", text), pages),
        })
    else:
        fields.update({
            "contract_number": _field(_first_match(r"\bcontract\s*(?:number|no\.?|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,})", text), pages),
            "effective_date": _field(_first_match(r"\b(?:effective\s+date|valid\s+from)\s*[:#-]?\s*([0-9]{1,4}[/-][0-9]{1,2}[/-][0-9]{1,4})", text), pages),
            "price_tolerance_percent": _field(_first_match(r"\b(?:price\s+variance|price\s+tolerance)\s*[:#-]?\s*(\d+(?:\.\d+)?%)", text), pages),
        })
    return ExtractionOutputRead(document_type=document_type, fields=fields, line_items=[])


def run_extraction(path: str, media_type: str, document_type: str) -> ExtractionRun:
    pages, notes = read_pages(path, media_type)
    backend = os.getenv("EXTRACTION_BACKEND", "rules").strip().casefold()
    extractor_version = EXTRACTOR_VERSION
    if backend == "local" and document_type == "invoice":
        from app.local_extraction import (
            LOCAL_EXTRACTOR_VERSION,
            LocalModelConfigurationError,
            LocalModelRequestError,
            extract_invoice_with_local_model,
        )

        try:
            local_result = extract_invoice_with_local_model(pages)
            output = local_result.output
            notes.extend(local_result.notes)
            extractor_version = LOCAL_EXTRACTOR_VERSION
        except (LocalModelConfigurationError, LocalModelRequestError) as local_error:
            notes.append(
                f"Local extractor failed ({type(local_error).__name__}); trying Groq fallback."
            )
            from app.llm_extraction import extract_invoice_with_llm

            groq_result = extract_invoice_with_llm(pages)
            output = groq_result.output
            notes.extend(groq_result.notes)
            extractor_version = GROQ_FALLBACK_VERSION
    elif backend not in {"rules", "local"}:
        raise ValueError("EXTRACTION_BACKEND must be 'rules' or 'local'.")
    else:
        output = rules_extract(document_type, pages)
    if not any(page.text for page in pages):
        notes.append("No text was extracted; send this document to human review or enable OCR.")
    return ExtractionRun(
        pages=pages,
        output=output,
        notes=notes,
        extractor_version=extractor_version,
    )
