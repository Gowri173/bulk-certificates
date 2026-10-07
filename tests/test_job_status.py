"""Tests for GET /api/v1/certificate-jobs/{job_id} status, counts, pagination, and crash recovery."""

from datetime import datetime, timezone
import uuid
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.enums import CertificateStatus, ErrorCode, JobStatus
from app.models import Certificate, Job
from app.worker.queue import recover_incomplete_jobs
from tests.conftest import make_payload


def test_job_status_all_successful_completed(client: TestClient) -> None:
    """A job with only valid recipients finishes with status=completed and 100% progress."""
    payload = make_payload(n=2)
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    status_resp = client.get(f"/api/v1/certificate-jobs/{job_id}")
    assert status_resp.status_code == 200
    data = status_resp.json()

    assert data["job_id"] == job_id
    assert data["status"] == "completed"
    assert data["total_count"] == 2
    assert data["success_count"] == 2
    assert data["failed_count"] == 0
    assert data["pending_count"] == 0
    assert data["progress_percent"] == 100.0
    assert data["completed_at"] is not None


def test_job_status_mixed_completed_with_errors(client: TestClient) -> None:
    """A job with some valid and some invalid recipients finishes with status=completed_with_errors."""
    payload = {
        "event_name": "Mixed Status Event",
        "recipients": [
            {"name": "Good One", "email": "good@example.com"},
            {"name": "", "email": "bad-email"},  # Invalid
        ],
    }
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    status_resp = client.get(f"/api/v1/certificate-jobs/{job_id}")
    assert status_resp.status_code == 200
    data = status_resp.json()

    assert data["status"] == "completed_with_errors"
    assert data["total_count"] == 2
    assert data["success_count"] == 1
    assert data["failed_count"] == 1
    assert data["pending_count"] == 0
    assert data["progress_percent"] == 100.0


def test_job_status_all_invalid_failed(client: TestClient) -> None:
    """A job with 0 successful certificates finishes with status=failed."""
    payload = {
        "event_name": "All Bad Event",
        "recipients": [
            {"name": "", "email": "bad1"},
            {"name": "", "email": "bad2"},
        ],
    }
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    status_resp = client.get(f"/api/v1/certificate-jobs/{job_id}")
    assert status_resp.status_code == 200
    data = status_resp.json()

    assert data["status"] == "failed"
    assert data["total_count"] == 2
    assert data["success_count"] == 0
    assert data["failed_count"] == 2
    assert data["pending_count"] == 0
    assert data["progress_percent"] == 100.0


def test_job_status_404_for_unknown_job(client: TestClient) -> None:
    """Requesting an unknown job_id returns 404."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/certificate-jobs/{fake_id}")
    assert resp.status_code == 404
    assert f"Certificate job not found: {fake_id}" in resp.json()["detail"]


def test_retry_failed_and_download_zip_404_for_unknown_job(client: TestClient) -> None:
    """Retry-failed and download-zip endpoints return 404 for non-existent jobs."""
    fake_id = str(uuid.uuid4())

    retry_resp = client.post(f"/api/v1/certificate-jobs/{fake_id}/retry-failed")
    assert retry_resp.status_code == 404
    assert f"Certificate job not found: {fake_id}" in retry_resp.json()["detail"]

    zip_resp = client.get(f"/api/v1/certificate-jobs/{fake_id}/download")
    assert zip_resp.status_code == 404
    assert f"Certificate job not found: {fake_id}" in zip_resp.json()["detail"]


def test_job_without_issuer_or_achievement_generates_successfully(
    client: TestClient, db_session: Session
) -> None:
    """A job submitted without optional issuer_name or achievement renders and completes successfully."""
    payload = {
        "event_name": "Minimalist Conference",
        "recipients": [
            {"name": "Solo Attendee", "email": "solo@example.com"}
        ],
    }
    resp = client.post("/api/v1/certificate-jobs", json=payload)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status_resp = client.get(f"/api/v1/certificate-jobs/{job_id}")
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "completed"

    cert = db_session.query(Certificate).filter(Certificate.job_id == job_id).first()
    assert cert is not None
    assert cert.status == CertificateStatus.SUCCESS
    assert cert.achievement is None


def test_crash_recovery_resets_stuck_rows_and_completes_job(
    db_session: Session, client: TestClient
) -> None:
    """Startup crash recovery resets rows stuck in 'processing' and completes the job."""
    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        event_name="Crash Recovery Event",
        title="Certificate of Resilience",
        total_count=2,
        success_count=0,
        failed_count=0,
        status=JobStatus.PROCESSING,
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )
    db_session.add(job)

    # Add 1 stuck in PROCESSING (simulating dead worker) and 1 still PENDING
    c1 = Certificate(
        id=str(uuid.uuid4()),
        job_id=job_id,
        row_index=0,
        recipient_name="Stuck User",
        recipient_email="stuck@example.com",
        status=CertificateStatus.PROCESSING,
    )
    c2 = Certificate(
        id=str(uuid.uuid4()),
        job_id=job_id,
        row_index=1,
        recipient_name="Pending User",
        recipient_email="pending@example.com",
        status=CertificateStatus.PENDING,
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    # Trigger recovery function
    recover_incomplete_jobs()

    # Verify job and certificates resumed and finished
    db_session.refresh(job)
    db_session.refresh(c1)
    db_session.refresh(c2)

    assert job.status == JobStatus.COMPLETED
    assert job.success_count == 2
    assert job.failed_count == 0
    assert c1.status == CertificateStatus.SUCCESS
    assert c1.file_path is not None
    assert c2.status == CertificateStatus.SUCCESS
    assert c2.file_path is not None


def test_job_certificates_pagination_and_status_filter(
    client: TestClient, db_session: Session
) -> None:
    """Listing certificates supports pagination (limit, offset) and status filtering."""
    payload = {
        "event_name": "Pagination Event",
        "recipients": [
            {"name": "User 1", "email": "u1@example.com"},
            {"name": "User 2", "email": "u2@example.com"},
            {"name": "User 3", "email": "u3@example.com"},
            {"name": "", "email": "bad"},  # Failed row
        ],
    }
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    # 1. Fetch page 1 (limit=2, offset=0)
    page1 = client.get(f"/api/v1/certificate-jobs/{job_id}/certificates?limit=2&offset=0")
    assert page1.status_code == 200
    p1_data = page1.json()
    assert p1_data["total"] == 4
    assert len(p1_data["items"]) == 2
    assert p1_data["items"][0]["row_index"] == 0
    assert p1_data["items"][1]["row_index"] == 1

    # 2. Fetch page 2 (limit=2, offset=2)
    page2 = client.get(f"/api/v1/certificate-jobs/{job_id}/certificates?limit=2&offset=2")
    assert page2.status_code == 200
    p2_data = page2.json()
    assert p2_data["total"] == 4
    assert len(p2_data["items"]) == 2
    assert p2_data["items"][0]["row_index"] == 2
    assert p2_data["items"][1]["row_index"] == 3

    # 3. Filter by status=failed
    failed_filter = client.get(f"/api/v1/certificate-jobs/{job_id}/certificates?status=failed")
    assert failed_filter.status_code == 200
    f_data = failed_filter.json()
    assert f_data["total"] == 1
    assert len(f_data["items"]) == 1
    assert f_data["items"][0]["status"] == "failed"
    assert f_data["items"][0]["error_code"] == "VALIDATION_ERROR"
    assert f_data["items"][0]["download_url"] is None

    # 4. Filter by status=success
    success_filter = client.get(f"/api/v1/certificate-jobs/{job_id}/certificates?status=success")
    assert success_filter.status_code == 200
    s_data = success_filter.json()
    assert s_data["total"] == 3
    for item in s_data["items"]:
        assert item["status"] == "success"
        assert item["download_url"] is not None


def test_partial_progress_state(client: TestClient, db_session: Session) -> None:
    """A seeded job in partial progress state reports correct pending_count and progress_percent."""
    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        event_name="Progress Test",
        total_count=4,
        success_count=2,
        failed_count=0,
        status=JobStatus.PROCESSING,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(job)

    # 2 success, 1 processing, 1 pending
    db_session.add(Certificate(id=str(uuid.uuid4()), job_id=job_id, row_index=0, status=CertificateStatus.SUCCESS, recipient_name="U1"))
    db_session.add(Certificate(id=str(uuid.uuid4()), job_id=job_id, row_index=1, status=CertificateStatus.SUCCESS, recipient_name="U2"))
    db_session.add(Certificate(id=str(uuid.uuid4()), job_id=job_id, row_index=2, status=CertificateStatus.PROCESSING, recipient_name="U3"))
    db_session.add(Certificate(id=str(uuid.uuid4()), job_id=job_id, row_index=3, status=CertificateStatus.PENDING, recipient_name="U4"))
    db_session.commit()

    resp = client.get(f"/api/v1/certificate-jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "processing"
    assert data["total_count"] == 4
    assert data["success_count"] == 2
    assert data["pending_count"] == 2  # 1 processing + 1 pending
    assert data["progress_percent"] == 50.0
