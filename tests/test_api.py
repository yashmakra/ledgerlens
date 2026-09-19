from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import extraction as extraction_module
from app import main as main_module
from app import worker
from app.db import Base, get_db
from app.evaluation import GoldenInvoice, evaluate_rules, summarize_results
from app.extraction import ExtractedPage, ExtractionRun, rules_extract
from app.llm_extraction import (
    LLMExtractionResult,
    LLMFieldValue,
    LLMInvoiceOutput,
    LLMLineItem,
    extract_invoice_with_llm,
)
from app.local_extraction import LocalExtractionResult
from app.main import app
from app.training_data import training_example


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("DOCUMENT_STORAGE_DIR", str(tmp_path / "documents"))
    monkeypatch.setattr(worker, "SessionLocal", TestingSession)

    def override_db() -> Generator[Session, None, None]:
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


def test_pdf_upload_is_deduplicated_and_processed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"%PDF-1.4\nphase 2 synthetic sample\n%%EOF"
    files = {"file": ("invoice.pdf", content, "application/pdf")}
    data = {"document_type": "invoice"}
    uploaded = client.post("/documents", files=files, data=data)
    assert uploaded.status_code == 202
    first = uploaded.json()
    assert first["duplicate_upload"] is False
    assert first["job"]["status"] == "queued"
    assert len(first["document"]["sha256"]) == 64

    duplicate = client.post(
        "/documents",
        files={"file": ("northwind-may-invoice.pdf", content, "application/pdf")},
        data=data,
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate_upload"] is True
    assert duplicate.json()["document"]["id"] == first["document"]["id"]
    assert duplicate.json()["upload"]["id"] != first["upload"]["id"]

    upload_history = client.get(f"/documents/{first['document']['id']}/uploads")
    assert upload_history.status_code == 200
    assert [upload["original_filename"] for upload in upload_history.json()] == [
        "invoice.pdf",
        "northwind-may-invoice.pdf",
    ]

    pages = [ExtractedPage(
        page_number=1,
        method="pdf_text",
        text="Northwind Components\nSynthetic supplier invoice - created only for local testing\nInvoice Number: INV-2048\nTotal: $10,600.00",
    )]
    monkeypatch.setattr(
        worker,
        "run_extraction",
        lambda *_args: ExtractionRun(pages=pages, output=rules_extract("invoice", pages), notes=[]),
    )
    assert worker.process_next_job() is True
    job = client.get(f"/document-jobs/{first['job']['id']}")
    assert job.status_code == 200
    assert job.json()["status"] == "complete"
    assert job.json()["metadata_json"]["phase"] == "extraction"
    assert job.json()["metadata_json"]["duration_ms"] >= 0

    extraction = client.get(f"/documents/{first['document']['id']}/extraction")
    assert extraction.status_code == 200
    assert extraction.json()["output_json"]["fields"]["invoice_number"]["value"] == "INV-2048"
    assert extraction.json()["output_json"]["fields"]["invoice_number"]["evidence"][0]["page"] == 1


def test_rules_baseline_keeps_source_evidence() -> None:
    pages = [ExtractedPage(
        page_number=1,
        method="pdf_text",
        text="Northwind Components\nSynthetic supplier invoice - created only for local testing\nInvoice Number: INV-2048\nPO Number: PO-7781\nTotal: $10,600.00",
    )]
    result = rules_extract("invoice", pages)
    assert result.fields["invoice_number"].value == "INV-2048"
    assert result.fields["purchase_order_number"].value == "PO-7781"
    assert result.fields["total_amount"].value == "10,600.00"
    assert result.fields["total_amount"].evidence[0].text == "10,600.00"

    malformed = rules_extract("invoice", [ExtractedPage(1, "Total: ,600.00", "pdf_text")])
    assert malformed.fields["total_amount"].value is None


def test_rules_extraction_handles_common_ocr_label_errors_safely() -> None:
    pages = [ExtractedPage(
        page_number=1,
        method="ocr",
        text="NORTHWIND COMPONENTS INVOICE\nInvoice Number: INV-2048\nTAL DUE 10,600.00\nSubtotal $10,600.00\nTax $0.00",
    )]
    result = rules_extract("invoice", pages)
    assert result.fields["supplier_name"].value == "NORTHWIND COMPONENTS"
    assert result.fields["total_amount"].value == "10,600.00"
    assert result.fields["total_amount"].evidence[0].text == "10,600.00"


def test_evaluation_reports_field_accuracy_and_failures() -> None:
    records = [GoldenInvoice(
        case_id="benchmark-1",
        pages=[ExtractedPage(1, "Acme\nInvoice Number: AC-1\nTotal: $10.00", "pdf_text")],
        expected_fields={"invoice_number": "AC-1", "total_amount": "10.00", "invoice_date": "2026-01-01"},
    )]
    summary = summarize_results(evaluate_rules(records))
    assert summary["passed"] == 2
    assert summary["total"] == 3
    assert summary["accuracy"] == 0.6667
    assert summary["failures"][0]["field"] == "invoice_date"


def test_training_example_preserves_source_and_missing_fields() -> None:
    example = training_example({
        "case_id": "training-1",
        "pages": [{"page_number": 1, "text": "Acme\nInvoice Number: A-1"}],
        "expected_fields": {"supplier_name": "Acme", "invoice_number": "A-1"},
    })
    assert example["case_id"] == "training-1"
    assert "[Page 1]" in example["messages"][1]["content"]
    assert '"supplier_name":"Acme"' in example["messages"][2]["content"]
    assert '"total_amount":null' in example["messages"][2]["content"]


def test_llm_extractor_requires_quotes_from_the_source_page() -> None:
    pages = [ExtractedPage(
        page_number=1,
        method="pdf_text",
        text="Northwind Components\nInvoice Number: INV-2048\nTOTAL DUE: $10,600.00",
    )]
    parsed = LLMInvoiceOutput(
        supplier_name=LLMFieldValue(value="Northwind Components", evidence_page=1, evidence_text="Northwind Components"),
        currency=LLMFieldValue(value=None, evidence_page=None, evidence_text=None),
        invoice_number=LLMFieldValue(value="INV-2048", evidence_page=1, evidence_text="Invoice Number: INV-2048"),
        invoice_date=LLMFieldValue(value=None, evidence_page=None, evidence_text=None),
        purchase_order_number=LLMFieldValue(value=None, evidence_page=None, evidence_text=None),
        total_amount=LLMFieldValue(value="10,600.00", evidence_page=1, evidence_text="TOTAL DUE: $10,600.00"),
        line_items=[LLMLineItem(
            description="Made-up widget",
            item_code=None,
            quantity=None,
            unit_price=None,
            amount=None,
            evidence_page=1,
            evidence_text="TOTAL DUE: $10,600.00",
        )],
    )

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["response_format"] == {"type": "json_object"}
            assert "JSON Schema" in kwargs["messages"][0]["content"]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=parsed.model_dump_json()))])

    result = extract_invoice_with_llm(
        pages,
        client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())),
        model="test-model",
    )

    assert result.provider == "groq"
    assert result.model == "test-model"
    assert result.output.fields["invoice_number"].value == "INV-2048"
    assert result.output.fields["invoice_number"].confidence is None
    assert result.output.fields["invoice_number"].evidence[0].page == 1
    assert result.output.fields["total_amount"].value == "10,600.00"
    assert result.output.line_items == []
    assert any("line item 1 omitted" in note for note in result.notes)


def test_compare_extractors_route_returns_both_without_overwriting_baseline(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"%PDF-1.4\nsynthetic comparison document\n%%EOF"
    uploaded = client.post(
        "/documents",
        files={"file": ("compare-invoice.pdf", content, "application/pdf")},
        data={"document_type": "invoice"},
    )
    document_id = uploaded.json()["document"]["id"]
    pages = [ExtractedPage(1, "Northwind Components\nInvoice Number: INV-2048\nTotal: $10,600.00", "pdf_text")]
    baseline = rules_extract("invoice", pages)
    monkeypatch.setattr(worker, "run_extraction", lambda *_args: ExtractionRun(pages, baseline, []))
    assert worker.process_next_job() is True
    monkeypatch.setattr(
        main_module,
        "extract_invoice_with_llm",
        lambda _pages: LLMExtractionResult(output=baseline, provider="groq", model="mock-model", notes=[]),
    )

    response = client.post(f"/documents/{document_id}/compare-extractors")

    assert response.status_code == 200
    assert response.json()["provider"] == "groq"
    assert response.json()["model"] == "mock-model"
    assert response.json()["field_agreement"]["invoice_number"] is True
    saved = client.get(f"/documents/{document_id}/extraction").json()
    assert saved["output_json"]["fields"]["invoice_number"]["value"] == "INV-2048"


def test_compare_extractors_can_include_local_adapter(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"%PDF-1.4\nlocal comparison document\n%%EOF"
    uploaded = client.post(
        "/documents",
        files={"file": ("local-invoice.pdf", content, "application/pdf")},
        data={"document_type": "invoice"},
    )
    document_id = uploaded.json()["document"]["id"]
    pages = [ExtractedPage(1, "Northwind Components\nInvoice Number: INV-2048\nTotal: $10,600.00", "pdf_text")]
    baseline = rules_extract("invoice", pages)
    monkeypatch.setattr(worker, "run_extraction", lambda *_args: ExtractionRun(pages, baseline, []))
    assert worker.process_next_job() is True
    monkeypatch.setattr(
        main_module,
        "extract_invoice_with_llm",
        lambda _pages: LLMExtractionResult(output=baseline, provider="groq", model="mock-model", notes=[]),
    )
    monkeypatch.setattr(
        main_module,
        "extract_invoice_with_local_model",
        lambda _pages: LocalExtractionResult(output=baseline, provider="local", model="mock-adapter", notes=[]),
    )

    response = client.post(f"/documents/{document_id}/compare-extractors?include_local=true")

    assert response.status_code == 200
    assert response.json()["local_model"] == "mock-adapter"
    assert response.json()["local_field_agreement"]["invoice_number"] is True


def test_local_backend_uses_groq_when_local_inference_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [ExtractedPage(1, "Northwind Components\nInvoice Number: INV-2048", "pdf_text")]
    baseline = rules_extract("invoice", pages)
    monkeypatch.setenv("EXTRACTION_BACKEND", "local")
    monkeypatch.setattr(extraction_module, "read_pages", lambda *_args: (pages, []))

    def fail_local(_pages):
        from app.local_extraction import LocalModelRequestError

        raise LocalModelRequestError("test failure")

    monkeypatch.setattr("app.local_extraction.extract_invoice_with_local_model", fail_local)
    monkeypatch.setattr(
        "app.llm_extraction.extract_invoice_with_llm",
        lambda _pages: LLMExtractionResult(output=baseline, provider="groq", model="fallback", notes=[]),
    )

    result = extraction_module.run_extraction("unused.pdf", "application/pdf", "invoice")

    assert result.output == baseline
    assert result.extractor_version == "groq-fallback-v1"
    assert "trying Groq fallback" in result.notes[0]


def test_record_match_suggests_exact_supplier_and_po_without_writing_invoice(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplier = client.post("/suppliers", json={
        "supplier_code": "NW",
        "legal_name": "Northwind Components",
    }).json()
    po = client.post("/purchase-orders", json={
        "supplier_id": supplier["id"],
        "po_number": "PO-7781",
        "currency": "USD",
        "ordered_on": "2026-05-01",
        "lines": [{
            "line_number": 1,
            "item_code": "WIDGET-1",
            "description": "Widget",
            "ordered_quantity": "100",
            "unit_price": "100.00",
        }],
    }).json()
    uploaded = client.post(
        "/documents",
        files={"file": ("link-sample.pdf", b"%PDF-1.4\nrecord link sample\n%%EOF", "application/pdf")},
        data={"document_type": "invoice"},
    )
    document_id = uploaded.json()["document"]["id"]
    pages = [ExtractedPage(
        1,
        "Northwind Components\nInvoice Number: INV-2051\nPurchase Order: PO-7781\nTotal Due: $100.00",
        "pdf_text",
    )]
    monkeypatch.setattr(
        worker,
        "run_extraction",
        lambda *_args: ExtractionRun(pages, rules_extract("invoice", pages), []),
    )
    assert worker.process_next_job() is True

    suggestion = client.get(f"/documents/{document_id}/record-match")
    assert suggestion.status_code == 200
    match = suggestion.json()
    assert match["status"] == "matched"
    assert match["ready_to_link"] is True
    assert match["supplier_id"] == supplier["id"]
    assert match["purchase_order_id"] == po["id"]
    assert client.get("/invoices").json() == []

    confirmed = client.post(f"/documents/{document_id}/confirm-match", json={
        "supplier_id": supplier["id"],
        "purchase_order_id": po["id"],
        "invoice_number": "INV-2051",
        "invoice_date": "2026-05-12",
        "currency": "USD",
        "subtotal": "100.00",
        "tax_amount": "0.00",
        "total_amount": "100.00",
        "lines": [{
            "line_number": 1,
            "item_code": "WIDGET-1",
            "description": "Widget",
            "quantity": "1",
            "unit_price": "100.00",
            "line_amount": "100.00",
        }],
    })
    assert confirmed.status_code == 201
    confirmation = confirmed.json()
    assert confirmation["invoice"]["invoice_number"] == "INV-2051"
    assert confirmation["case_id"] > 0
    saved_document = client.get(f"/documents/{document_id}").json()
    assert saved_document["parent_type"] == "invoice"
    assert saved_document["parent_id"] == confirmation["invoice_id"]
    saved_case = client.get(f"/cases/{confirmation['case_id']}").json()
    assert saved_case["events"][-1]["event_type"] == "invoice_link_confirmed"

    duplicate_match = client.get(f"/documents/{document_id}/record-match").json()
    assert duplicate_match["status"] == "needs_review"
    assert duplicate_match["ready_to_link"] is False
    assert duplicate_match["duplicate_invoice_id"] == confirmation["invoice_id"]


def test_upload_rejects_an_invalid_signature(client: TestClient) -> None:
    response = client.post(
        "/documents",
        files={"file": ("not-really-a-pdf.pdf", b"plain text", "application/pdf")},
        data={"document_type": "invoice"},
    )
    assert response.status_code == 415


def test_failed_document_job_records_error_and_can_be_retried(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = client.post(
        "/documents",
        files={"file": ("worker-failure.pdf", b"%PDF-1.4\nfailure sample\n%%EOF", "application/pdf")},
        data={"document_type": "invoice"},
    )
    job_id = uploaded.json()["job"]["id"]

    def fail_extraction(*_args):
        raise RuntimeError("simulated OCR outage")

    monkeypatch.setattr(worker, "run_extraction", fail_extraction)
    assert worker.process_next_job() is True

    failed = client.get(f"/document-jobs/{job_id}")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["attempt_count"] == 1
    assert "simulated OCR outage" in failed.json()["error"]

    retried = client.post(f"/document-jobs/{job_id}/retry")
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert retried.json()["error"] is None


def test_review_page_is_served(client: TestClient) -> None:
    response = client.get("/review")
    assert response.status_code == 200
    assert "Invoice Review Queue" in response.text
    assert "/cases/" in response.text
    assert client.get("/static/dashboard.css").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200


def test_readiness_reports_database_ocr_and_worker_queue(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["ocr"] in {"ok", "unavailable"}
    assert body["checks"]["worker_queue"] == "idle"


def test_records_link_and_case_can_be_retrieved(client: TestClient) -> None:
    supplier = client.post("/suppliers", json={"supplier_code": "NW", "legal_name": "Northwind Components"})
    assert supplier.status_code == 201
    supplier_id = supplier.json()["id"]

    contract = client.post("/contracts", json={
        "supplier_id": supplier_id,
        "contract_number": "C-2026-04",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "currency": "usd",
        "price_tolerance_pct": "5.00",
        "payment_terms_days": 30,
    })
    assert contract.status_code == 201
    assert contract.json()["currency"] == "USD"

    po = client.post("/purchase-orders", json={
        "supplier_id": supplier_id,
        "contract_id": contract.json()["id"],
        "po_number": "PO-7781",
        "currency": "USD",
        "ordered_on": "2026-05-01",
        "lines": [{"line_number": 1, "item_code": "WIDGET-1", "description": "Widget", "ordered_quantity": "100", "unit_price": "100.00"}],
    })
    assert po.status_code == 201
    po_line_id = po.json()["lines"][0]["id"]

    receipt = client.post("/receipts", json={
        "purchase_order_id": po.json()["id"],
        "receipt_number": "GR-901",
        "received_on": "2026-05-10",
        "lines": [{"line_number": 1, "purchase_order_line_id": po_line_id, "item_code": "WIDGET-1", "quantity_received": "80", "quantity_accepted": "80"}],
    })
    assert receipt.status_code == 201
    assert client.get("/contracts").json()[0]["contract_number"] == "C-2026-04"
    assert client.get("/purchase-orders").json()[0]["lines"][0]["item_code"] == "WIDGET-1"
    assert client.get("/receipts").json()[0]["lines"][0]["quantity_accepted"] == "80.0000"

    invoice = client.post("/invoices", json={
        "supplier_id": supplier_id,
        "purchase_order_id": po.json()["id"],
        "invoice_number": "INV-2048",
        "invoice_date": "2026-05-12",
        "currency": "USD",
        "subtotal": "10600.00",
        "tax_amount": "0.00",
        "total_amount": "10600.00",
        "lines": [{"line_number": 1, "item_code": "WIDGET-1", "description": "Widget", "quantity": "100", "unit_price": "106.00", "line_amount": "10600.00"}],
    })
    assert invoice.status_code == 201
    assert Decimal(invoice.json()["lines"][0]["quantity"]) == Decimal(100)
    assert client.get("/invoices").json()[0]["total_amount"] == "10600.00"

    case_response = client.get("/cases")
    assert case_response.status_code == 200
    assert case_response.json()[0]["invoice"]["invoice_number"] == "INV-2048"
    case_id = case_response.json()[0]["id"]

    reconciliation = client.post(f"/cases/{case_id}/reconcile")
    assert reconciliation.status_code == 200
    report = reconciliation.json()
    assert report["status"] == "needs_review"
    exception_codes = {check["code"] for check in report["checks"] if check["status"] == "exception"}
    assert exception_codes == {"accepted_quantity", "unit_price"}

    updated_case = client.get(f"/cases/{case_id}").json()
    assert updated_case["status"] == "needs_review"
    assert updated_case["events"][-1]["event_type"] == "reconciliation_completed"
    assert updated_case["events"][-1]["detail"]["exception_count"] == 2

    blocked_approval = client.post(f"/cases/{case_id}/decision", json={
        "decision": "approved",
        "actor": "reviewer@example.test",
    })
    assert blocked_approval.status_code == 422

    rejected_without_reason = client.post(f"/cases/{case_id}/decision", json={
        "decision": "rejected",
        "actor": "reviewer@example.test",
    })
    assert rejected_without_reason.status_code == 422

    override = client.post(f"/cases/{case_id}/decision", json={
        "decision": "approved",
        "actor": "reviewer@example.test",
        "override_blocking_exceptions": True,
        "reason": "Synthetic test override after procurement approval.",
    })
    assert override.status_code == 201
    assert override.json()["status"] == "approved"
    assert override.json()["override_blocking_exceptions"] is True
    final_case = client.get(f"/cases/{case_id}").json()
    assert final_case["events"][-1]["event_type"] == "case_approved_with_override"


def test_api_rejects_invalid_arithmetic_and_supplier_mismatch(client: TestClient) -> None:
    supplier_a = client.post("/suppliers", json={"supplier_code": "A", "legal_name": "Supplier A"}).json()
    supplier_b = client.post("/suppliers", json={"supplier_code": "B", "legal_name": "Supplier B"}).json()
    po = client.post("/purchase-orders", json={
        "supplier_id": supplier_a["id"], "po_number": "PO-A1", "currency": "USD", "ordered_on": "2026-01-01",
        "lines": [{"line_number": 1, "item_code": "X", "description": "Item X", "ordered_quantity": "1", "unit_price": "10"}],
    }).json()

    wrong_supplier = client.post("/invoices", json={
        "supplier_id": supplier_b["id"], "purchase_order_id": po["id"], "invoice_number": "I-1",
        "invoice_date": "2026-01-02", "currency": "USD", "subtotal": "10", "tax_amount": "0",
        "total_amount": "10", "lines": [{"line_number": 1, "item_code": "X", "description": "Item X", "quantity": "1", "unit_price": "10", "line_amount": "10"}],
    })
    assert wrong_supplier.status_code == 422

    invalid_math = client.post("/invoices", json={
        "supplier_id": supplier_a["id"], "purchase_order_id": po["id"], "invoice_number": "I-2",
        "invoice_date": "2026-01-02", "currency": "USD", "subtotal": "10", "tax_amount": "2",
        "total_amount": "10", "lines": [{"line_number": 1, "item_code": "X", "description": "Item X", "quantity": "1", "unit_price": "10", "line_amount": "10"}],
    })
    assert invalid_math.status_code == 422


def test_reconciliation_flags_missing_receipt_and_contract(client: TestClient) -> None:
    supplier = client.post("/suppliers", json={"supplier_code": "NOREF", "legal_name": "No Reference Supplier"}).json()
    po = client.post("/purchase-orders", json={
        "supplier_id": supplier["id"],
        "po_number": "PO-NOREF",
        "currency": "USD",
        "ordered_on": "2026-01-01",
        "lines": [{
            "line_number": 1,
            "item_code": "ITEM-1",
            "description": "Item",
            "ordered_quantity": "2",
            "unit_price": "10.00",
        }],
    }).json()
    invoice = client.post("/invoices", json={
        "supplier_id": supplier["id"],
        "purchase_order_id": po["id"],
        "invoice_number": "INV-NOREF",
        "invoice_date": "2026-01-02",
        "currency": "USD",
        "subtotal": "10.00",
        "tax_amount": "1.00",
        "total_amount": "11.00",
        "lines": [{
            "line_number": 1,
            "item_code": "ITEM-1",
            "description": "Item",
            "quantity": "1",
            "unit_price": "10.00",
            "line_amount": "10.00",
        }],
    }).json()

    case = next(item for item in client.get("/cases").json() if item["invoice_id"] == invoice["id"])
    report = client.post(f"/cases/{case['id']}/reconcile")
    assert report.status_code == 200
    exception_codes = {check["code"] for check in report.json()["checks"] if check["status"] == "exception"}
    assert {"receipt_missing", "contract_missing"}.issubset(exception_codes)

    correction = client.post(f"/cases/{case['id']}/correct-invoice", json={
        "actor": "reviewer@example.test",
        "reason": "Corrected the invoice line after reviewing the source document.",
        "subtotal": "10.00",
        "tax_amount": "1.00",
        "total_amount": "11.00",
        "lines": [{
            "line_number": 1,
            "item_code": "ITEM-1",
            "description": "Corrected item description",
            "quantity": "1",
            "unit_price": "10.00",
            "line_amount": "10.00",
        }],
    })
    assert correction.status_code == 201
    assert correction.json()["status"] == "new"
    assert correction.json()["invoice"]["lines"][0]["description"] == "Corrected item description"

    stale_decision = client.post(f"/cases/{case['id']}/decision", json={
        "decision": "rejected",
        "actor": "reviewer@example.test",
        "reason": "Attempting a decision from an old report.",
    })
    assert stale_decision.status_code == 409
    corrected_case = client.get(f"/cases/{case['id']}").json()
    assert corrected_case["events"][-1]["event_type"] == "invoice_corrected"
    assert corrected_case["events"][-1]["detail"]["before"]["lines"][0]["description"] == "Item"
