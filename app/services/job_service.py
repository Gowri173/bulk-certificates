"""Job lifecycle service handling job creation, database queries, summaries, and retry logic."""

from datetime import datetime, timezone
import logging
from typing import List, Optional, Set, Tuple
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.enums import CertificateStatus, ErrorCode, JobStatus
from app.models import Certificate, Job
from app.schemas import CreateJobRequest, JobStatusResponse
from app.services.validation import validate_recipient

logger = logging.getLogger(__name__)


def create_job(
    db: Session,
    request: CreateJobRequest,
    idempotency_key: Optional[str] = None,
) -> Tuple[Job, int, int, bool]:
    """Create a new bulk certificate generation job and certificate rows in a single transaction.

    If an idempotency_key is provided and already exists, returns the existing job.

    Returns:
        Tuple of (Job, accepted_count, rejected_count, is_existing).
    """
    if idempotency_key:
        existing_job = (
            db.query(Job)
            .filter(Job.idempotency_key == idempotency_key)
            .first()
        )
        if existing_job:
            logger.info("Idempotent request matched existing job %s", existing_job.id)
            # Compute accepted and rejected counts for existing job
            rejected = (
                db.query(func.count(Certificate.id))
                .filter(
                    Certificate.job_id == existing_job.id,
                    Certificate.error_code.in_([ErrorCode.VALIDATION_ERROR, ErrorCode.DUPLICATE_RECIPIENT]),
                )
                .scalar()
                or 0
            )
            accepted = existing_job.total_count - rejected
            return existing_job, accepted, rejected, True

    total_recipients = len(request.recipients)
    job = Job(
        event_name=request.event_name,
        title=request.title,
        issuer_name=request.issuer_name,
        issued_on=request.issued_on,
        total_count=total_recipients,
        idempotency_key=idempotency_key,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.flush()  # Populate job.id for certificate foreign keys

    seen_emails: Set[str] = set()
    accepted_count = 0
    rejected_count = 0

    for idx, raw_item in enumerate(request.recipients):
        val_res = validate_recipient(raw_item)

        if val_res.is_valid and val_res.clean_data is not None:
            email = val_res.clean_data["email"]
            if email in seen_emails:
                # Duplicate email within same request
                cert = Certificate(
                    job_id=job.id,
                    row_index=idx,
                    recipient_name=val_res.clean_data["name"],
                    recipient_email=email,
                    achievement=val_res.clean_data["achievement"],
                    status=CertificateStatus.FAILED,
                    error_code=ErrorCode.DUPLICATE_RECIPIENT,
                    error_message=f"Duplicate recipient email in request: {email}",
                )
                rejected_count += 1
            else:
                seen_emails.add(email)
                cert = Certificate(
                    job_id=job.id,
                    row_index=idx,
                    recipient_name=val_res.clean_data["name"],
                    recipient_email=email,
                    achievement=val_res.clean_data["achievement"],
                    status=CertificateStatus.PENDING,
                )
                accepted_count += 1
        else:
            # Level 2 validation failure
            cert = Certificate(
                job_id=job.id,
                row_index=idx,
                recipient_name=val_res.raw_name,
                recipient_email=val_res.raw_email,
                achievement=val_res.raw_achievement,
                status=CertificateStatus.FAILED,
                error_code=val_res.error_code,
                error_message=val_res.error_message,
            )
            rejected_count += 1

        db.add(cert)

    job.failed_count = rejected_count

    # If zero valid recipients, immediately fail and complete the job
    if accepted_count == 0:
        job.status = JobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(job)

    logger.info(
        "Created job %s with %d total, %d accepted, %d rejected",
        job.id,
        total_recipients,
        accepted_count,
        rejected_count,
    )
    return job, accepted_count, rejected_count, False


def get_job_or_404(db: Session, job_id: str) -> Job:
    """Fetch job by ID or raise HTTP 404 Not Found."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Certificate job not found: {job_id}",
        )
    return job


def get_job_summary(db: Session, job: Job) -> JobStatusResponse:
    """Construct JobStatusResponse with dynamic progress calculations and counts."""
    # Count pending / processing items
    pending_count = (
        db.query(func.count(Certificate.id))
        .filter(
            Certificate.job_id == job.id,
            Certificate.status.in_([CertificateStatus.PENDING, CertificateStatus.PROCESSING]),
        )
        .scalar()
        or 0
    )

    # Ensure counts are synchronized
    success_count = job.success_count
    failed_count = job.failed_count

    if job.total_count > 0:
        progress = round(((success_count + failed_count) / job.total_count) * 100.0, 1)
        progress = min(progress, 100.0)
    else:
        progress = 0.0

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        event_name=job.event_name,
        total_count=job.total_count,
        success_count=success_count,
        failed_count=failed_count,
        pending_count=pending_count,
        progress_percent=progress,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def list_certificates(
    db: Session,
    job_id: str,
    status_filter: Optional[CertificateStatus] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[int, List[Certificate]]:
    """List certificates for a job with filtering and pagination."""
    # Verify job existence first
    get_job_or_404(db, job_id)

    query = db.query(Certificate).filter(Certificate.job_id == job_id)
    if status_filter:
        query = query.filter(Certificate.status == status_filter)

    total = query.count()
    items = (
        query.order_by(Certificate.row_index)
        .offset(offset)
        .limit(min(limit, 500))
        .all()
    )
    return total, items


def retry_failed(db: Session, job_id: str) -> Job:
    """Re-queue generation failures for retry."""
    job = get_job_or_404(db, job_id)

    # Only retry certificates that encountered GENERATION_ERROR
    failed_certs = (
        db.query(Certificate)
        .filter(
            Certificate.job_id == job.id,
            Certificate.error_code == ErrorCode.GENERATION_ERROR,
        )
        .all()
    )

    if not failed_certs:
        return job

    for cert in failed_certs:
        cert.status = CertificateStatus.PENDING
        cert.error_code = None
        cert.error_message = None
        cert.file_path = None
        cert.serial_number = None
        cert.generated_at = None

    job.status = JobStatus.PENDING
    job.completed_at = None

    # Recompute failed_count
    remaining_failed = (
        db.query(func.count(Certificate.id))
        .filter(
            Certificate.job_id == job.id,
            Certificate.status == CertificateStatus.FAILED,
        )
        .scalar()
        or 0
    )
    job.failed_count = remaining_failed

    db.commit()
    db.refresh(job)

    logger.info("Reset %d generation failed certificates for job %s", len(failed_certs), job.id)
    return job
