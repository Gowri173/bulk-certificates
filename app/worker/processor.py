"""Background processor executing certificate generation with crash recovery and per-row failure isolation."""

from datetime import datetime, timezone
import logging
import secrets
from typing import Optional
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.enums import CertificateStatus, ErrorCode, JobStatus
from app.generator.base import CertificateData
from app.generator.pdf import get_generator
from app.models import Certificate, Job
from app.storage.local import get_storage

logger = logging.getLogger(__name__)


def generate_serial_number(db: Session, year: Optional[int] = None) -> str:
    """Generate a unique serial number in format CERT-<year>-<8 hex uppercase>.

    Retries up to 5 times if a collision is detected.
    """
    if year is None:
        year = datetime.now(timezone.utc).year

    for _ in range(5):
        random_hex = secrets.token_hex(4).upper()
        serial = f"CERT-{year}-{random_hex}"
        exists = (
            db.query(Certificate.id)
            .filter(Certificate.serial_number == serial)
            .first()
        )
        if not exists:
            return serial

    # Fallback to 6 bytes hex if high collision
    return f"CERT-{year}-{secrets.token_hex(6).upper()}"


def _recompute_and_save_counts(db: Session, job: Job) -> tuple[int, int]:
    """Recompute success and failed counts directly from certificate rows and update the job."""
    counts = (
        db.query(Certificate.status, func.count(Certificate.id))
        .filter(Certificate.job_id == job.id)
        .group_by(Certificate.status)
        .all()
    )
    status_dict = dict(counts)
    success = status_dict.get(CertificateStatus.SUCCESS, 0)
    failed = status_dict.get(CertificateStatus.FAILED, 0)

    job.success_count = success
    job.failed_count = failed
    db.commit()
    return success, failed


def process_job(job_id: str) -> None:
    """Process a certificate generation job.

    Uses its own dedicated database session, processes pending certificates in batches,
    claims each row atomically, wraps generation in a per-certificate try/except, and
    commits after every row to ensure crash recovery and complete failure isolation.
    """
    settings = get_settings()
    storage = get_storage()
    db: Session = SessionLocal()

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.warning("Worker received non-existent job ID %s", job_id)
            return

        # Do not reprocess finalized jobs
        if job.status in [
            JobStatus.COMPLETED,
            JobStatus.COMPLETED_WITH_ERRORS,
            JobStatus.FAILED,
        ] and job.completed_at is not None:
            logger.info("Job %s is already finalized (%s); skipping", job.id, job.status.value)
            return

        # -------------------------------------------------------------
        # 1. Transition Job to Processing
        # -------------------------------------------------------------
        job.status = JobStatus.PROCESSING
        if not job.started_at:
            job.started_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Started processing job %s", job.id)

        # -------------------------------------------------------------
        # 2. Crash Recovery: Reset dangling 'processing' certificates
        # -------------------------------------------------------------
        db.execute(
            update(Certificate)
            .where(
                Certificate.job_id == job.id,
                Certificate.status == CertificateStatus.PROCESSING,
            )
            .values(status=CertificateStatus.PENDING)
        )
        db.commit()

        # -------------------------------------------------------------
        # 3. Process Pending Certificates in Batches
        # -------------------------------------------------------------
        processed_since_sync = 0

        while True:
            # Fetch up to BATCH_SIZE pending rows
            pending_rows = (
                db.query(Certificate)
                .filter(
                    Certificate.job_id == job.id,
                    Certificate.status == CertificateStatus.PENDING,
                )
                .order_by(Certificate.row_index)
                .limit(settings.BATCH_SIZE)
                .all()
            )

            if not pending_rows:
                break

            for row in pending_rows:
                # Atomically claim the row
                claim_result = db.execute(
                    update(Certificate)
                    .where(
                        Certificate.id == row.id,
                        Certificate.status == CertificateStatus.PENDING,
                    )
                    .values(status=CertificateStatus.PROCESSING)
                )
                db.commit()

                if claim_result.rowcount == 0:
                    # Another worker claimed this row
                    continue

                # Refresh certificate instance in session
                db.refresh(row)

                # Isolated generation try/except
                try:
                    generator = get_generator()
                    serial = generate_serial_number(db, year=job.issued_on.year)

                    cert_data = CertificateData(
                        recipient_name=row.recipient_name or "Recipient",
                        achievement=row.achievement,
                        event_name=job.event_name,
                        title=job.title,
                        issuer_name=job.issuer_name,
                        issued_on=job.issued_on,
                        serial_number=serial,
                    )

                    pdf_bytes = generator.generate(cert_data)
                    rel_path = f"{job.id}/{row.id}.pdf"
                    saved_path = storage.save(rel_path, pdf_bytes)

                    row.status = CertificateStatus.SUCCESS
                    row.file_path = saved_path
                    row.serial_number = serial
                    row.generated_at = datetime.now(timezone.utc)
                    row.error_code = None
                    row.error_message = None

                except Exception as exc:
                    row.status = CertificateStatus.FAILED
                    row.error_code = ErrorCode.GENERATION_ERROR
                    row.error_message = str(exc)[:500]
                    logger.exception(
                        "Generation failed for certificate %s in job %s: %s",
                        row.id,
                        job.id,
                        exc,
                    )

                # Commit after EACH certificate
                db.commit()
                processed_since_sync += 1

                # Periodically sync counters to the job row
                if processed_since_sync >= 25:
                    _recompute_and_save_counts(db, job)
                    processed_since_sync = 0

        # -------------------------------------------------------------
        # 4. Finalize Job Status
        # -------------------------------------------------------------
        db.refresh(job)
        success_count, failed_count = _recompute_and_save_counts(db, job)

        if success_count == 0:
            job.status = JobStatus.FAILED
        elif failed_count == 0:
            job.status = JobStatus.COMPLETED
        else:
            job.status = JobStatus.COMPLETED_WITH_ERRORS

        job.completed_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Finished job %s: status=%s, total=%d, success=%d, failed=%d",
            job.id,
            job.status.value,
            job.total_count,
            success_count,
            failed_count,
        )

    except Exception as exc:
        logger.exception("Unexpected fatal error processing job %s: %s", job_id, exc)
    finally:
        db.close()
