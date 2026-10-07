"""Tests for Level 1 (request-level) and Level 2 (recipient-level) validation."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import CertificateStatus, ErrorCode
from app.models import Certificate
from app.services.validation import validate_recipient


# ==============================================================================
# 1. Level 1 Request-Level Validation Tests (HTTP 422)
# ==============================================================================

def test_level1_empty_recipients_list_rejected(client: TestClient) -> None:
    """Empty recipients array must be rejected with 422."""
    payload = {
        "event_name": "FastAPI Workshop",
        "recipients": [],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 422
    assert "recipients" in response.json()["detail"]


def test_level1_exceeding_max_recipients_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Submitting more than MAX_RECIPIENTS must be rejected with 422."""
    # Temporarily set MAX_RECIPIENTS to 3
    low_max_settings = Settings(
        DATABASE_URL="sqlite:///:memory:",
        STORAGE_DIR="./storage",
        MAX_RECIPIENTS=3,
        PROCESSING_MODE="inline",
    )
    monkeypatch.setattr("app.schemas.get_settings", lambda: low_max_settings)

    payload = {
        "event_name": "Large Batch",
        "recipients": [
            {"name": f"User {i}", "email": f"user{i}@example.com"}
            for i in range(4)
        ],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 422
    assert "exceeds maximum allowed count" in response.json()["detail"]


def test_level1_missing_or_blank_event_name_rejected(client: TestClient) -> None:
    """Missing or whitespace-only event_name must be rejected with 422."""
    payload1 = {
        "recipients": [{"name": "Asha", "email": "asha@example.com"}],
    }
    response1 = client.post("/api/v1/certificate-jobs", json=payload1)
    assert response1.status_code == 422

    payload2 = {
        "event_name": "   ",
        "recipients": [{"name": "Asha", "email": "asha@example.com"}],
    }
    response2 = client.post("/api/v1/certificate-jobs", json=payload2)
    assert response2.status_code == 422


# ==============================================================================
# 2. Level 2 Recipient-Level Validation Tests (Stored as failed rows)
# ==============================================================================

def test_level2_blank_name_and_bad_email_fails_row_others_succeed(
    client: TestClient, db_session: Session
) -> None:
    """Invalid recipient row fails with VALIDATION_ERROR while valid rows succeed."""
    payload = {
        "event_name": "Annual Summit",
        "recipients": [
            {"name": "Valid Recipient", "email": "valid@example.com"},
            {"name": "", "email": "invalid-email"},  # Level 2 error
        ],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    data = response.json()
    job_id = data["job_id"]

    certs = (
        db_session.query(Certificate)
        .filter(Certificate.job_id == job_id)
        .order_by(Certificate.row_index)
        .all()
    )
    assert len(certs) == 2

    # Row 0 is valid and succeeded
    assert certs[0].status == CertificateStatus.SUCCESS
    assert certs[0].recipient_name == "Valid Recipient"
    assert certs[0].recipient_email == "valid@example.com"
    assert certs[0].error_code is None

    # Row 1 is invalid and marked FAILED with VALIDATION_ERROR
    assert certs[1].status == CertificateStatus.FAILED
    assert certs[1].error_code == ErrorCode.VALIDATION_ERROR
    assert "name: must not be empty" in certs[1].error_message
    assert "email: invalid format" in certs[1].error_message


def test_level2_duplicate_email_fails_second_occurrence(
    client: TestClient, db_session: Session
) -> None:
    """Duplicate email within same request fails second occurrence with DUPLICATE_RECIPIENT."""
    payload = {
        "event_name": "Tech Conference",
        "recipients": [
            {"name": "First Instance", "email": "duplicate@example.com"},
            {"name": "Second Instance", "email": "DUPLICATE@example.com"},  # Case-insensitive duplicate
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

    assert certs[0].status == CertificateStatus.SUCCESS
    assert certs[1].status == CertificateStatus.FAILED
    assert certs[1].error_code == ErrorCode.DUPLICATE_RECIPIENT
    assert "Duplicate recipient email in request" in certs[1].error_message


def test_level2_whitespace_trimmed_and_email_lowercased(
    client: TestClient, db_session: Session
) -> None:
    """Recipient name whitespace is trimmed and email is normalized/lowercased."""
    payload = {
        "event_name": "Formatting Check",
        "recipients": [
            {
                "name": "   Jane Doe   ",
                "email": "  Jane.Doe@EXAMPLE.COM  ",
                "achievement": "  Top Performer  ",
            }
        ],
    }
    response = client.post("/api/v1/certificate-jobs", json=payload)
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    cert = db_session.query(Certificate).filter(Certificate.job_id == job_id).first()
    assert cert is not None
    assert cert.recipient_name == "Jane Doe"
    assert cert.recipient_email == "jane.doe@example.com"
    assert cert.achievement == "Top Performer"


def test_level2_non_dict_item_handled(
    client: TestClient, db_session: Session
) -> None:
    """Non-dict items in recipients array are captured as VALIDATION_ERROR without 500 error."""
    payload = {
        "event_name": "Type Safety Test",
        "recipients": [
            "this is a string, not a dict",
            12345,
            {"name": "Good Row", "email": "good@example.com"},
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
    assert len(certs) == 3

    assert certs[0].status == CertificateStatus.FAILED
    assert certs[0].error_code == ErrorCode.VALIDATION_ERROR
    assert "Recipient item must be a JSON object" in certs[0].error_message

    assert certs[1].status == CertificateStatus.FAILED
    assert certs[1].error_code == ErrorCode.VALIDATION_ERROR

    assert certs[2].status == CertificateStatus.SUCCESS


# ==============================================================================
# 3. Direct Unit Tests for validate_recipient
# ==============================================================================

def test_unit_validate_recipient_valid() -> None:
    """Pure validate_recipient returns clean data for valid inputs."""
    res = validate_recipient({
        "name": "  Asha Rao  ",
        "email": "Asha.Rao@Domain.Org",
        "achievement": "First Place",
    })
    assert res.is_valid is True
    assert res.clean_data == {
        "name": "Asha Rao",
        "email": "asha.rao@domain.org",
        "achievement": "First Place",
    }
    assert res.error_code is None


def test_unit_validate_recipient_control_characters() -> None:
    """Control characters in recipient name are rejected."""
    res = validate_recipient({
        "name": "Bad\x00Name\x07",
        "email": "valid@example.com",
    })
    assert res.is_valid is False
    assert res.error_code == ErrorCode.VALIDATION_ERROR
    assert "name: must not contain control characters" in res.error_message


def test_unit_validate_recipient_combined_errors() -> None:
    """Multiple validation errors on one row are joined with semicolons."""
    res = validate_recipient({
        "name": "",
        "email": "bad_email_format",
        "achievement": "x" * 201,
    })
    assert res.is_valid is False
    assert res.error_code == ErrorCode.VALIDATION_ERROR
    assert "name: must not be empty" in res.error_message
    assert "email: invalid format" in res.error_message
    assert "achievement: must not exceed 200 characters" in res.error_message
