"""Enumeration definitions for job status, certificate status, and error codes."""

from enum import Enum


class JobStatus(str, Enum):
    """Lifecycle states for a bulk certificate generation job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class CertificateStatus(str, Enum):
    """Lifecycle states for an individual certificate item."""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class ErrorCode(str, Enum):
    """Granular error codes identifying why a certificate failed."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    DUPLICATE_RECIPIENT = "DUPLICATE_RECIPIENT"
    GENERATION_ERROR = "GENERATION_ERROR"
