"""Service for generating ZIP archives of generated certificates."""

from pathlib import Path
import re
import tempfile
import zipfile
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.enums import CertificateStatus
from app.models import Certificate, Job
from app.storage.base import Storage


def sanitize_filename_component(name: str) -> str:
    """Sanitize string to only alphanumeric characters, dots, underscores, and dashes."""
    if not name:
        return "recipient"
    # Replace any character that is not [A-Za-z0-9._-] with an underscore
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    # Collapse consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("._-")
    return sanitized or "recipient"


def create_job_zip_file(db: Session, job_id: str, storage: Storage) -> tuple[Path, int]:
    """Package all successful certificates for a job into a temporary ZIP file.

    Args:
        db: Active database session.
        job_id: ID of the job to bundle.
        storage: Storage provider to locate PDF files.

    Returns:
        Tuple of (Path to temporary ZIP file, count of included certificates).

    Raises:
        HTTPException: 404 if job not found, 409 if no successful certificates exist.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Certificate job not found: {job_id}",
        )

    successful_certs = (
        db.query(Certificate)
        .filter(
            Certificate.job_id == job_id,
            Certificate.status == CertificateStatus.SUCCESS,
        )
        .order_by(Certificate.row_index)
        .all()
    )

    if not successful_certs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No successful certificates are available for download for this job yet.",
        )

    # Use a named temporary file to avoid keeping large ZIPs in memory
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_zip_path = Path(temp_zip.name)
    temp_zip.close()

    added_count = 0
    with zipfile.ZipFile(temp_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_out:
        for cert in successful_certs:
            if not cert.file_path or not storage.exists(cert.file_path):
                continue

            safe_name = sanitize_filename_component(cert.recipient_name or "certificate")
            serial = cert.serial_number or cert.id
            entry_name = f"{serial}_{safe_name}.pdf"

            abs_pdf_path = storage.open_path(cert.file_path)
            zip_out.write(abs_pdf_path, arcname=entry_name)
            added_count += 1

    if added_count == 0:
        if temp_zip_path.exists():
            temp_zip_path.unlink()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No certificate files could be found in storage for this job.",
        )

    return temp_zip_path, added_count
