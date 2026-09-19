# Supplier Invoice Reconciliation

Phase 1 foundation for a procure-to-pay exception-resolution project. This slice models suppliers, contracts, purchase orders, delivery receipts, invoices, reconciliation cases, and audit events. It accepts manually entered synthetic records so the data model and API can be learned before adding OCR or LLM extraction.

Phase 2 adds document intake: source files are stored under generated IDs, content is validated from its signature, and a separate worker records safe file metadata. Identical bytes are stored once through a SHA-256 fingerprint, while every upload attempt is retained with its original filename. Phase 3 reads embedded PDF text, supports OCR through Tesseract for scans/images, and stores schema-validated rules extraction with source evidence. An optional Groq JSON path can be used to compare LLM extraction against the rules baseline; its response is validated locally with Pydantic and does not replace the stored baseline.

Phase 4 adds invoice record-linking: it suggests exact supplier and PO matches from saved extraction, checks for duplicate invoice numbers, and supports reviewer confirmation that creates the invoice and links the source document. Phase 5 adds deterministic reconciliation against PO lines, accepted receipts, and active contract price tolerance. These phases are in progress; matching and reconciliation currently cover the invoice path and do not silently approve payment.

## Run locally

Requires Python 3.11 or newer. SQLite is used by default, so PostgreSQL is optional for this phase.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` to create sample suppliers, contracts, POs, receipts, and invoices. The initial API is for local learning and has no authentication; do not expose it publicly.

To use PostgreSQL instead, start `docker compose up -d db` and set `DATABASE_URL` to `postgresql+psycopg://reconcile:reconcile@localhost:5432/reconcile` before starting the API.

## API routes

- `GET /health`
- `POST /suppliers`, `GET /suppliers`
- `POST /contracts`, `GET /contracts`
- `POST /purchase-orders`, `GET /purchase-orders`
- `POST /receipts`, `GET /receipts`
- `POST /invoices`, `GET /invoices`
- `GET /cases`, `GET /cases/{case_id}`
- `POST /cases/{case_id}/reconcile` — compare an invoice case to its purchase order, accepted receipts, and active contract tolerance; returns and records the checks.
- `POST /cases/{case_id}/decision` — record a reviewer approval or rejection after reconciliation. Approval with blocking exceptions requires an override reason.
- `POST /documents` — multipart file upload with `document_type` and optional `parent_type`/`parent_id` form fields.
- `GET /documents`, `GET /documents/{document_id}`, `GET /documents/{document_id}/source`
- `GET /documents/{document_id}/uploads` — shows all filenames/upload attempts for one stored document.
- `GET /documents/{document_id}/pages` — text retained per page and the method used.
- `GET /documents/{document_id}/extraction` — structured output with source evidence.
- `GET /documents/{document_id}/record-match` — suggest exact supplier and PO candidates from an extracted invoice, and check for an existing invoice number; this endpoint does not create or change records.
- `POST /documents/{document_id}/confirm-match` — save reviewer-confirmed invoice values, create its case, and link the source document.
- `POST /documents/{document_id}/compare-extractors` — returns side-by-side rules and Groq extraction plus per-field agreement; add `?include_local=true` to also run the fine-tuned local adapter. It does not overwrite the saved baseline.
- `GET /document-jobs`, `GET /document-jobs/{job_id}`
- `POST /document-jobs/{job_id}/retry` — queue extraction again after an OCR or extractor change.

## Why the database has separate tables

A supplier can have many contracts, POs, and invoices. A PO has multiple lines, and receipts and invoices refer to those lines. Keeping records separate prevents repeating supplier and contract details on every line and makes it possible to compare them later. `ReconciliationCase` is the unit of work shown to a reviewer; it points to the invoice and will later collect documents, exceptions, and review events.

The model stores currency amounts as fixed-precision decimals rather than floating-point numbers. Floating-point rounding can make money comparisons unreliable. Foreign keys connect related records, while unique constraints prevent duplicate supplier codes, invoice numbers per supplier, and PO numbers per supplier.

The app currently creates tables automatically for local learning. Before a shared or production deployment, add versioned database migrations, authentication, authorization, and operational security.

## Phase 4 and 5: link and reconcile an invoice

Create supplier and PO reference records using the API (or use existing records), then upload and extract an invoice. In Swagger, call `GET /documents/{document_id}/record-match` to preview exact supplier and PO candidates. Reviewers can call `POST /documents/{document_id}/confirm-match` with corrected invoice values and line items to create the invoice/case and link the document. Then run `POST /cases/{case_id}/reconcile`. A `matched` result means all implemented checks passed; `needs_review` means at least one check needs a person to inspect it. The response lists checks such as `po_line_match`, `accepted_quantity`, `unit_price`, and `line_arithmetic`. `GET /cases/{case_id}` shows the updated status and saved audit events, including the last report under `events[].detail`.

For the current sample data, the invoice bills 100 units while only 80 have been accepted, and its unit price is 6% above the PO while the contract allows 5%. Phase 5 therefore returns `needs_review` with `accepted_quantity` and `unit_price` exceptions. Each check includes its severity, expected/actual values, and `source_records` such as `invoice_line`, `purchase_order_line`, `delivery_receipt_line`, and `contract`. This is intentional: the reconciliation engine detects the problem and leaves the payment decision to a human.

To connect an uploaded invoice to the records, call `GET /documents/{document_id}/record-match` after extraction finishes. Exact supplier-name and PO-number matches are suggested; an ambiguous, missing, mismatched, or duplicate record yields `needs_review`. The preview does not write anything. After reviewing the suggestion and filling/correcting invoice fields (including line items), call `POST /documents/{document_id}/confirm-match` with an `InvoiceCreate` JSON body. The endpoint checks supplier/PO consistency, currency, totals, and duplicates, creates the invoice and case, links the source document to the invoice, and records the confirmation in the case audit history. Then use `POST /cases/{case_id}/reconcile` to check quantities, prices, receipts, and line arithmetic.

After reconciliation, a reviewer can use `POST /cases/{case_id}/decision`. Rejection always requires a reason. If blocking exceptions exist, ordinary approval is rejected by the API; an override requires a reason and becomes a visible `case_approved_with_override` audit event. A final approval or rejection cannot be replaced by another decision.

## Run the Phase 2 worker

Start a second terminal in the same project directory after starting the API:

```powershell
python -m app.worker
```

Use `POST /documents` in Swagger to upload a PDF, PNG, JPEG, or TIFF file. Choose `invoice`, `purchase_order`, `delivery_receipt`, or `contract` as its document type. The response includes a document ID, upload ID, and job ID. If identical bytes are uploaded again under a different filename, the API reuses the stored document and job but records a new upload attempt; inspect it with `GET /documents/{document_id}/uploads`.

For digital PDFs, the worker extracts embedded text. For scans and images, it uses Tesseract when available. The rules baseline returns a field only when it can point to supporting source text. It stores page text separately from the structured output so a reviewer can inspect evidence.

### Optional LLM extraction comparison

Install the optional SDK and configure your Groq API key in the same PowerShell terminal that starts the API:

```powershell
pip install -e ".[dev,llm]"
Copy `.env.example` to `.env`, then put a newly generated Groq key in `.env`:

```text
GROQ_API_KEY=gsk_your_new_key_here
GROQ_MODEL=openai/gpt-oss-20b
```

The application loads this file automatically. `.env` is ignored by Git and must never be committed.
$env:GROQ_MODEL = "openai/gpt-oss-20b" # optional; this is the default
uvicorn app.main:app --reload
```

After a document job completes, call `POST /documents/{document_id}/compare-extractors` in Swagger. The endpoint reuses stored page text, runs both extractors, and returns their fields and agreement. The ordinary extraction endpoint continues to show the rules baseline; comparisons are not persisted. The LLM response is parsed against a Pydantic schema and accepted only when its quoted evidence exists on the cited page and supports the proposed value. Groq's best-effort JSON-schema mode is used for this nested schema; local validation rejects malformed or unsupported output. The provider call sends extracted invoice text to Groq, so use synthetic documents for learning unless you have reviewed the data-handling requirements for the invoices you intend to process.

Install Tesseract separately before processing scanned PDFs, PNGs, JPEGs, or TIFFs. If it is unavailable, the worker records a clear extraction limitation rather than inventing values. Digital PDFs with embedded text work without it.
 iw ill do this later
## Check the foundation

```powershell
pytest
```

The tests create a synthetic supplier/contract/PO/receipt/invoice flow and check structural rules. They do not yet evaluate OCR, contract interpretation, or exception detection; those belong to later phases.

## Phase 8: run the production-shaped stack

The Compose stack runs PostgreSQL, the FastAPI service, and the durable extraction worker. It waits for PostgreSQL health before starting the application services and restarts them if a process exits:

```powershell
docker compose up --build
```

Check service readiness at `http://127.0.0.1:8000/health/ready`. Stop the stack with `Ctrl+C`, or run `docker compose down` when finished. Uploaded files live in the named `document_data` volume and the database lives in `pgdata`.

## Optional Phase 9: fine-tune an invoice model

Phase 9 trains an open model, rather than Groq, to emit the same invoice JSON schema from extracted page text. First build the chat-style training files from the labeled benchmark:

```powershell
python scripts/build_finetune_dataset.py
```

Create a fictional starter corpus, then build the files:

```powershell
python scripts/generate_synthetic_training_data.py
python scripts/build_finetune_dataset.py
```

This produces `artifacts/fine_tuning/train.jsonl` and `validation.jsonl`. The generator creates 240 fictional invoices across 12 vendor groups. Vendor groups are kept fully separate between training and validation. Use this to validate the pipeline; add 200+ reviewed, diverse examples before making quality claims about a trained model. Then, on a CUDA Linux GPU environment, install the optional dependencies and train a QLoRA adapter:

```powershell
pip install -e ".[finetune]"
python scripts/train_qlora.py
```

The training script defaults to `Qwen/Qwen2.5-1.5B-Instruct`, which is suitable for a 6 GB GPU with 4-bit QLoRA. It writes the adapter under `artifacts/fine_tuning` and refuses to run without a CUDA GPU. Evaluate the adapter on the held-out split against both rules and Groq before using it as a fallback or removing Groq.

Evaluate exact invoice-field accuracy with:

```powershell
python scripts/evaluate_finetuned_model.py
```

This loads the base model plus the saved adapter, generates JSON from validation prompts, and reports parse failures and field-level expected/actual mismatches. Token accuracy from training logs is not used as the final quality metric.

To compare the trained adapter on a stored document, call `POST /documents/{document_id}/compare-extractors?include_local=true`. The response adds `local_output`, `local_model`, `local_field_agreement`, and `local_notes`. The local extractor is opt-in so ordinary API requests do not load a multi-gigabyte model into memory. To use it for new worker jobs, set `EXTRACTION_BACKEND=local` in both the API and worker environments and restart them. The worker first tries the local adapter; if local inference fails, it calls Groq and records `groq-fallback-v1` plus a note in the job metadata. Groq must be installed and configured for this fallback. If both extractors fail, the job is marked failed and can be retried. Keep the default `rules` backend until you have evaluated a larger reviewed dataset; the local model still runs behind source-evidence validation and does not silently replace missing values.

## Phase 7: run the extraction benchmark

The first benchmark uses three small, labeled invoice examples. Each record stores source page text and the expected field values. Run it from the project directory:

```powershell
python scripts/run_evaluation.py
```

The report shows overall field accuracy, accuracy by field, and the exact expected/actual values for failures. This benchmark currently evaluates the deterministic rules extractor. Later, the same labeled records can be sent through OCR and the Groq extractor so their results are comparable.

To evaluate the files currently stored by the upload pipeline, run:

```powershell
python scripts/run_real_document_evaluation.py
```

The real-document labels include a malformed total where the correct behavior is to return no value. This measures safe abstention: the extractor must not invent a financial amount when the source is damaged.

To compare the rules extractor with Groq on the same labeled text cases, install the optional SDK and run:

```powershell
pip install -e ".[llm]"
python scripts/compare_extractors.py
```

This command requires `GROQ_API_KEY` in `.env`. It reports accuracy for each extractor using identical expected answers; the LLM result is still accepted only when its evidence quote is present in the source text.
