"""Pydantic schemas for request validation and API responses."""

from datetime import date, datetime
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import get_settings
from app.enums import CertificateStatus, ErrorCode, JobStatus


# ==========================================
# Request Models
# ==========================================

class CreateJobRequest(BaseModel):
    """Payload for submitting a bulk certificate generation request."""

    event_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Name of the event, workshop, or course.",
    )
    title: str = Field(
        default="Certificate of Completion",
        min_length=1,
        max_length=100,
        description="Header title printed on the certificate.",
    )
    issuer_name: Optional[str] = Field(
        default=None,
        max_length=150,
        description="Optional name of the issuing organization or person.",
    )
    issued_on: date = Field(
        default_factory=date.today,
        description="Issue date in YYYY-MM-DD format.",
    )
    recipients: List[Any] = Field(
        ...,
        description="List of recipient entries. Items are loosely typed to allow individual row validation.",
    )

    @field_validator("event_name")
    @classmethod
    def validate_event_name(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("event_name must not be empty or whitespace only")
        return trimmed

    @field_validator("recipients")
    @classmethod
    def validate_recipients_list(cls, v: List[Any]) -> List[Any]:
        if not isinstance(v, list) or len(v) == 0:
            raise ValueError("recipients must be a non-empty list")
        max_allowed = get_settings().MAX_RECIPIENTS
        if len(v) > max_allowed:
            raise ValueError(f"recipients list exceeds maximum allowed count of {max_allowed}")
        return v


# ==========================================
# Response Models
# ==========================================

class JobLinks(BaseModel):
    """HATEOAS-style resource navigation links for a job."""

    self: str
    certificates: str
    download_all: str


class CreateJobResponse(BaseModel):
    """Response returned upon accepting a bulk generation job (202 Accepted)."""

    job_id: str
    status: JobStatus
    total_count: int
    accepted_count: int
    rejected_count: int
    links: JobLinks


class JobStatusResponse(BaseModel):
    """Detailed progress and status for a specific job."""

    job_id: str
    status: JobStatus
    event_name: str
    total_count: int
    success_count: int
    failed_count: int
    pending_count: int
    progress_percent: float
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class CertificateItemResponse(BaseModel):
    """Summary of an individual certificate result."""

    id: str
    row_index: int
    recipient_name: Optional[str] = None
    recipient_email: Optional[str] = None
    status: CertificateStatus
    serial_number: Optional[str] = None
    download_url: Optional[str] = None
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CertificateListResponse(BaseModel):
    """Paginated list of certificates belonging to a job."""

    total: int
    limit: int
    offset: int
    items: List[CertificateItemResponse]


class ErrorDetail(BaseModel):
    """Standard error response format."""

    detail: str
