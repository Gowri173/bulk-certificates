"""Tests for POST /api/v1/certificate-jobs endpoint and idempotency."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.enums import JobStatus
from app.models import Certificate, Job
from tests.conftest import make_payload


def test_create_job_valid_returns_202_and_counts(client: TestClient, db_session: Session) -> None:
    """Valid request returns 202 with job_id, links, and correct initial counts."""
    payload = make_payload(n=3)
    response = client.post("/api/v1/certificate-jobs", json=payload)

    assert response.status_code == 202
    data = response.json()

    assert "job_id" in data
    job_id = data["job_id"]
    assert data["total_count"] == 3
    assert data["accepted_count"] == 3
    assert data["rejected_count"] == 0

    # Verify HATEOAS links
    assert data["links"]["self"] == f"/api/v1/certificate-jobs/{job_id}"
    assert data["links"]["certificates"] == f"/api/v1/certificate-jobs/{job_id}/certificates"
    assert data["links"]["download_all"] == f"/api/v1/certificate-jobs/{job_id}/download"

    # Verify database persistence
    job_in_db = db_session.query(Job).filter(Job.id == job_id).first()
    assert job_in_db is not None
    assert job_in_db.event_name == payload["event_name"]
    assert job_in_db.total_count == 3

    # In inline processing mode, job completes immediately
    certs_in_db = db_session.query(Certificate).filter(Certificate.job_id == job_id).all()
    assert len(certs_in_db) == 3


def test_create_job_idempotency_returns_same_job(client: TestClient, db_session: Session) -> None:
    """Submitting the same Idempotency-Key returns the existing job without duplicate rows."""
    payload = make_payload(n=2)
    idempotency_key = "test-idem-key-12345"

    headers = {"Idempotency-Key": idempotency_key}

    # First request
    resp1 = client.post("/api/v1/certificate-jobs", json=payload, headers=headers)
    assert resp1.status_code == 202
    data1 = resp1.json()
    job_id1 = data1["job_id"]

    # Second request with same idempotency key
    resp2 = client.post("/api/v1/certificate-jobs", json=payload, headers=headers)
    assert resp2.status_code == 202
    data2 = resp2.json()
    job_id2 = data2["job_id"]

    assert job_id1 == job_id2

    # Verify no duplicate jobs or certificate rows were created
    total_jobs = db_session.query(Job).filter(Job.idempotency_key == idempotency_key).count()
    assert total_jobs == 1

    total_certs = db_session.query(Certificate).filter(Certificate.job_id == job_id1).count()
    assert total_certs == 2


def test_create_job_with_partial_invalid_recipients(client: TestClient) -> None:
    """Job with mixed valid and invalid recipients returns accepted and rejected counts."""
    payload = {
        "event_name": "Mixed Batch Event",
        "recipients": [
            {"name": "Alice Valid", "email": "alice@example.com"},
            {"name": "", "email": "bad-email"},  # Invalid
            {"name": "Bob Valid", "email": "bob@example.com"},
        ],
    }

    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    data = response.json()

    assert data["total_count"] == 3
    assert data["accepted_count"] == 2
    assert data["rejected_count"] == 1


def test_create_job_with_all_invalid_recipients_fails_immediately(
    client: TestClient, db_session: Session
) -> None:
    """If all recipients are invalid, job is accepted (202) but marked status=failed immediately."""
    payload = {
        "event_name": "All Invalid Event",
        "recipients": [
            {"name": "", "email": "not-an-email"},
            {"name": "   ", "email": "also-bad"},
        ],
    }

    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    data = response.json()
    job_id = data["job_id"]

    assert data["total_count"] == 2
    assert data["accepted_count"] == 0
    assert data["rejected_count"] == 2

    job_in_db = db_session.query(Job).filter(Job.id == job_id).first()
    assert job_in_db is not None
    assert job_in_db.status == JobStatus.FAILED
    assert job_in_db.completed_at is not None
