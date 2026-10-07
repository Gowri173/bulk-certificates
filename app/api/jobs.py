"""API endpoints for managing certificate generation jobs."""

from pathlib import Path
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.enums import CertificateStatus
from app.schemas import (
    CertificateItemResponse,
    CertificateListResponse,
    CreateJobRequest,
    CreateJobResponse,
    ErrorDetail,
    JobLinks,
    JobStatusResponse,
)
from app.services import job_service, zip_service
from app.storage.local import get_storage
from app.worker.queue import enqueue

router = APIRouter(prefix="/certificate-jobs", tags=["Jobs"])


def _cleanup_file(path: Path) -> None:
    """Safely remove a temporary file."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateJobResponse,
    responses={
        422: {"model": ErrorDetail, "description": "Request level validation error"},
    },
    summary="Submit a bulk certificate generation job",
    description="Accepts bulk recipient details, performs Level 1 and Level 2 validation, and enqueues generation.",
)
def submit_certificate_job(
    payload: CreateJobRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
) -> CreateJobResponse:
    """Submit a bulk certificate job with optional idempotency support."""
    # Fallback to check underscore variant or direct header lookup if alias missed it
    resolved_key = (
        idempotency_key
        or request.headers.get("idempotency-key")
        or request.headers.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or request.headers.get("Idempotency_Key")
    )

    job, accepted_count, rejected_count, is_existing = job_service.create_job(
        db=db,
        request=payload,
        idempotency_key=resolved_key,
    )

    # Only enqueue if this is a newly created job and has accepted recipients
    if not is_existing and accepted_count > 0:
        enqueue(job.id)

    return CreateJobResponse(
        job_id=job.id,
        status=job.status,
        total_count=job.total_count,
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        links=JobLinks(
            self=f"/api/v1/certificate-jobs/{job.id}",
            certificates=f"/api/v1/certificate-jobs/{job.id}/certificates",
            download_all=f"/api/v1/certificate-jobs/{job.id}/download",
        ),
    )


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    responses={
        404: {"model": ErrorDetail, "description": "Job not found"},
    },
    summary="Get job status and progress",
    description="Retrieve overall progress, counts, and status timestamps for a bulk job.",
)
def get_job_status(
    job_id: str,
    db: Session = Depends(get_db),
) -> JobStatusResponse:
    """Get real-time job generation status and progress percentage."""
    job = job_service.get_job_or_404(db, job_id)
    return job_service.get_job_summary(db, job)


@router.get(
    "/{job_id}/certificates",
    response_model=CertificateListResponse,
    responses={
        404: {"model": ErrorDetail, "description": "Job not found"},
    },
    summary="List certificates for a job",
    description="Fetch paginated certificate entries with optional status filtering.",
)
def get_job_certificates(
    job_id: str,
    status_filter: Optional[CertificateStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500, description="Max items to return (1-500)"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    db: Session = Depends(get_db),
) -> CertificateListResponse:
    """List certificates belonging to a specific job."""
    total, items = job_service.list_certificates(
        db=db,
        job_id=job_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )

    response_items = [
        CertificateItemResponse(
            id=c.id,
            row_index=c.row_index,
            recipient_name=c.recipient_name,
            recipient_email=c.recipient_email,
            status=c.status,
            serial_number=c.serial_number,
            download_url=(
                f"/api/v1/certificates/{c.id}/download"
                if c.status == CertificateStatus.SUCCESS
                else None
            ),
            error_code=c.error_code,
            error_message=c.error_message,
        )
        for c in items
    ]

    return CertificateListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=response_items,
    )


@router.post(
    "/{job_id}/retry-failed",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobStatusResponse,
    responses={
        404: {"model": ErrorDetail, "description": "Job not found"},
    },
    summary="Retry failed certificate generations",
    description="Re-queues certificates that failed with GENERATION_ERROR.",
)
def retry_failed_certificates(
    job_id: str,
    db: Session = Depends(get_db),
) -> JobStatusResponse:
    """Re-enqueue failed certificates for a job."""
    job = job_service.retry_failed(db, job_id)
    if job.status == CertificateStatus.PENDING or job.status == "pending":
        enqueue(job.id)
    return job_service.get_job_summary(db, job)


@router.get(
    "/{job_id}/download",
    responses={
        200: {"content": {"application/zip": {}}, "description": "ZIP archive of certificates"},
        404: {"model": ErrorDetail, "description": "Job not found"},
        409: {"model": ErrorDetail, "description": "No successful certificates available to download"},
    },
    summary="Download all successful certificates as a ZIP",
    description="Streams a ZIP archive containing all generated PDF certificates for the specified job.",
)
def download_job_zip(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download ZIP archive of all successfully generated certificates."""
    storage = get_storage()
    zip_path, _ = zip_service.create_job_zip_file(db, job_id, storage)

    # Clean up the temp zip file after response is transmitted
    background_tasks.add_task(_cleanup_file, zip_path)

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"certificates-{job_id}.zip",
    )
