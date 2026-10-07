"""Tests for PDF generation, ReportLab rendering, and disk storage."""

import io
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.enums import CertificateStatus
from app.models import Certificate
from app.storage.local import LocalStorage


def test_generation_creates_valid_pdf_on_disk(
    client: TestClient, db_session: Session, test_environment: dict
) -> None:
    """Successful generation produces a valid PDF starting with %PDF and correct metadata."""
    payload = {
        "event_name": "Docker & Kubernetes Deep Dive",
        "title": "Certificate of Excellence",
        "issuer_name": "Cloud Native Academy",
        "recipients": [
            {"name": "Robert Downey", "email": "robert@example.com", "achievement": "With Honors"},
            {"name": "Chris Evans", "email": "chris@example.com"},
        ],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    certs = (
        db_session.query(Certificate)
        .filter(Certificate.job_id == job_id)
        .order_by(Certificate.row_index)
        .all()
    )
    assert len(certs) == 2

    storage: LocalStorage = test_environment["storage"]

    # Verify each certificate
    for cert in certs:
        assert cert.status == CertificateStatus.SUCCESS
        assert cert.file_path is not None
        assert cert.serial_number is not None
        assert cert.serial_number.startswith("CERT-")

        # Verify file exists on disk
        assert storage.exists(cert.file_path)
        disk_path = storage.open_path(cert.file_path)
        pdf_bytes = disk_path.read_bytes()

        # Check standard PDF header
        assert pdf_bytes.startswith(b"%PDF")

        # Verify PDF contents with pypdf
        reader = PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        page_text = reader.pages[0].extract_text()

        # Check that recipient name, event name, and title are rendered in the PDF text
        assert cert.recipient_name in page_text
        assert "Docker & Kubernetes Deep Dive" in page_text
        assert "CERTIFICATE OF EXCELLENCE" in page_text.upper()


def test_generation_serial_numbers_are_unique(
    client: TestClient, db_session: Session
) -> None:
    """Each generated certificate receives a unique serial number."""
    payload = {
        "event_name": "Serial Uniqueness Test",
        "recipients": [
            {"name": f"Recipient {i}", "email": f"rec_{i}@example.com"}
            for i in range(10)
        ],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    certs = db_session.query(Certificate).filter(Certificate.job_id == job_id).all()
    serials = [c.serial_number for c in certs if c.serial_number is not None]

    assert len(serials) == 10
    assert len(set(serials)) == 10  # All unique


def test_generation_handles_very_long_recipient_name(
    client: TestClient, db_session: Session, test_environment: dict
) -> None:
    """Generator handles an unusually long recipient name by auto-shrinking or truncating cleanly."""
    long_name = "Dr. Alexandria Montgomery-Fitzgerald III, Countess of Somewhere very long"
    payload = {
        "event_name": "Ultra Long Name Workshop",
        "recipients": [
            {"name": long_name, "email": "alex@example.com"},
        ],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    cert = db_session.query(Certificate).filter(Certificate.job_id == job_id).first()
    assert cert is not None
    assert cert.status == CertificateStatus.SUCCESS

    storage: LocalStorage = test_environment["storage"]
    disk_path = storage.open_path(cert.file_path)
    pdf_bytes = disk_path.read_bytes()
    assert pdf_bytes.startswith(b"%PDF")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
