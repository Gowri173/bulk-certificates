"""API endpoints for single certificate retrieval and download."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.enums import CertificateStatus
from app.models import Certificate
from app.schemas import ErrorDetail
from app.storage.local import get_storage

router = APIRouter(prefix="/certificates", tags=["Certificates"])


@router.get(
    "/{certificate_id}/download",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "Generated PDF certificate"},
        404: {"model": ErrorDetail, "description": "Certificate not found"},
        409: {"model": ErrorDetail, "description": "Certificate is not ready or failed"},
    },
    summary="Download an individual PDF certificate",
    description="Returns the PDF certificate file as an attachment.",
)
def download_certificate(
    certificate_id: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download a single generated certificate PDF."""
    cert = db.query(Certificate).filter(Certificate.id == certificate_id).first()
    if not cert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Certificate not found: {certificate_id}",
        )

    if cert.status != CertificateStatus.SUCCESS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Certificate is not available for download (status: {cert.status.value}).",
        )

    if not cert.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Certificate file path is not set.",
        )

    storage = get_storage()
    try:
        abs_path = storage.open_path(cert.file_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Certificate file was not found in storage.",
        )

    download_name = f"{cert.serial_number or cert.id}.pdf"
    return FileResponse(
        path=str(abs_path),
        media_type="application/pdf",
        filename=download_name,
    )
