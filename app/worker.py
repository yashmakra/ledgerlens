"""Durable worker for Phase 3 document text extraction and structured baseline output."""

import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.extraction import run_extraction
from app.models import DocumentExtraction, DocumentPage, DocumentProcessingJob

logger = logging.getLogger(__name__)


def process_next_job() -> bool:
    with SessionLocal() as db:
        job = db.scalar(
            select(DocumentProcessingJob)
            .where(DocumentProcessingJob.status == "queued")
            .order_by(DocumentProcessingJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return False
        job.status = "processing"
        job.attempt_count += 1
        job.started_at = datetime.now(UTC)
        db.commit()
        job_id = job.id
        document_id = job.document_id
        attempt_count = job.attempt_count
        logger.info(
            "document_job_started job_id=%s document_id=%s attempt=%s",
            job_id,
            document_id,
            attempt_count,
        )

    started_clock = time.perf_counter()
    try:
        with SessionLocal() as db:
            job = db.get(DocumentProcessingJob, job_id)
            if not job:
                return True
            result = run_extraction(
                job.document.storage_path,
                job.document.media_type,
                job.document.document_type,
            )
            db.execute(delete(DocumentPage).where(DocumentPage.document_id == job.document_id))
            db.add_all([
                DocumentPage(
                    document_id=job.document_id,
                    page_number=page.page_number,
                    extraction_method=page.method,
                    text=page.text,
                )
                for page in result.pages
            ])
            extraction = db.scalar(select(DocumentExtraction).where(DocumentExtraction.document_id == job.document_id))
            if extraction:
                extraction.extractor_version = result.extractor_version
                extraction.output_json = result.output.model_dump(mode="json")
                extraction.notes = result.notes
            else:
                db.add(DocumentExtraction(
                    id=str(uuid4()),
                    document_id=job.document_id,
                    extractor_version=result.extractor_version,
                    output_json=result.output.model_dump(mode="json"),
                    notes=result.notes,
                ))
            job.metadata_json = {
                "phase": "extraction",
                "extractor_version": result.extractor_version,
                "page_count": len(result.pages),
                "text_pages": sum(bool(page.text) for page in result.pages),
                "notes": result.notes,
                "duration_ms": round((time.perf_counter() - started_clock) * 1000, 2),
            }
            job.status = "complete"
            job.completed_at = datetime.now(UTC)
            db.commit()
            logger.info(
                "document_job_completed job_id=%s document_id=%s pages=%s duration_ms=%s",
                job_id,
                job.document_id,
                len(result.pages),
                job.metadata_json["duration_ms"],
            )
    except Exception as exc:
        with SessionLocal() as db:
            job = db.get(DocumentProcessingJob, job_id)
            if job:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                job.metadata_json = {
                    **(job.metadata_json or {}),
                    "duration_ms": round((time.perf_counter() - started_clock) * 1000, 2),
                }
                db.commit()
        logger.exception(
            "document_job_failed job_id=%s document_id=%s attempt=%s",
            job_id,
            document_id,
            attempt_count,
        )
    return True


def run_worker() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_db()
    logger.info("document_worker_started")
    while True:
        if not process_next_job():
            time.sleep(2)


if __name__ == "__main__":
    run_worker()
