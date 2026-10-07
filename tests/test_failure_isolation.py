"""Tests for per-certificate failure isolation and the retry-failed endpoint."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.enums import CertificateStatus, ErrorCode, JobStatus
from app.generator.base import CertificateData, CertificateGenerator
from app.generator.pdf import ReportLabCertificateGenerator
from app.models import Certificate, Job


class FaultyGenerator(CertificateGenerator):
    """Test generator that raises an exception for a specific recipient name."""

    def __init__(self, failing_name: str, real_gen: CertificateGenerator) -> None:
        self.failing_name = failing_name
        self.real_gen = real_gen

    def generate(self, data: CertificateData) -> bytes:
        if data.recipient_name == self.failing_name:
            raise RuntimeError(f"Simulated generation crash for {data.recipient_name}")
        return self.real_gen.generate(data)


def test_failure_isolation_and_retry_failed(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception generating one certificate does not crash other rows, and retry-failed recovers it."""
    real_gen = ReportLabCertificateGenerator()
    faulty_gen = FaultyGenerator(failing_name="Error Prone User", real_gen=real_gen)

    # Monkeypatch get_generator to return our faulty generator
    monkeypatch.setattr("app.worker.processor.get_generator", lambda: faulty_gen)

    payload = {
        "event_name": "Failure Isolation Workshop",
        "recipients": [
            {"name": "Alice Good", "email": "alice@example.com"},
            {"name": "Error Prone User", "email": "error@example.com"},
            {"name": "Bob Good", "email": "bob@example.com"},
        ],
    }

    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    assert create_resp.status_code == 202
    job_id = create_resp.json()["job_id"]

    # 1. Inspect certificate rows
    certs = (
        db_session.query(Certificate)
        .filter(Certificate.job_id == job_id)
        .order_by(Certificate.row_index)
        .all()
    )
    assert len(certs) == 3

    # Alice and Bob succeeded
    assert certs[0].status == CertificateStatus.SUCCESS
    assert certs[0].recipient_name == "Alice Good"
    assert certs[2].status == CertificateStatus.SUCCESS
    assert certs[2].recipient_name == "Bob Good"

    # Error Prone User failed with GENERATION_ERROR
    assert certs[1].status == CertificateStatus.FAILED
    assert certs[1].recipient_name == "Error Prone User"
    assert certs[1].error_code == ErrorCode.GENERATION_ERROR
    assert "Simulated generation crash" in certs[1].error_message

    # Job ended with completed_with_errors
    job = db_session.query(Job).filter(Job.id == job_id).first()
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert job.success_count == 2
    assert job.failed_count == 1

    # 2. Fix generator patch and retry failed
    monkeypatch.setattr("app.worker.processor.get_generator", lambda: real_gen)

    retry_resp = client.post(f"/api/v1/certificate-jobs/{job_id}/retry-failed")
    assert retry_resp.status_code == 202

    # Verify that the failed row was re-processed and is now SUCCESS
    db_session.refresh(certs[1])
    assert certs[1].status == CertificateStatus.SUCCESS
    assert certs[1].error_code is None
    assert certs[1].error_message is None
    assert certs[1].file_path is not None

    # Verify job status transitioned to completed
    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert job.success_count == 3
    assert job.failed_count == 0
