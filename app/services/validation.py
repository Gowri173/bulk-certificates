"""Recipient validation logic for Level 2 recipient-level validation."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from email_validator import EmailNotValidError, validate_email

from app.enums import ErrorCode


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating an individual recipient entry."""

    is_valid: bool
    clean_data: Optional[Dict[str, Any]] = None
    raw_name: Optional[str] = None
    raw_email: Optional[str] = None
    raw_achievement: Optional[str] = None
    error_code: Optional[ErrorCode] = None
    error_message: Optional[str] = None


def has_control_characters(s: str) -> bool:
    """Check whether a string contains ASCII control characters."""
    return any(ord(c) < 32 for c in s)


def validate_recipient(raw: Any) -> ValidationResult:
    """Validate a single raw recipient payload per Level 2 rules.

    Pure function that collects all validation issues for a single row into
    a human-readable composite error message.

    Args:
        raw: The raw input object (expected to be a dictionary).

    Returns:
        ValidationResult indicating success with clean data, or failure with error details.
    """
    if not isinstance(raw, dict):
        return ValidationResult(
            is_valid=False,
            error_code=ErrorCode.VALIDATION_ERROR,
            error_message="Recipient item must be a JSON object",
        )

    raw_name_val = raw.get("name")
    raw_email_val = raw.get("email")
    raw_ach_val = raw.get("achievement")

    # Keep string representations for storing on the certificate row even if invalid
    raw_name_str = str(raw_name_val) if raw_name_val is not None and isinstance(raw_name_val, str) else None
    raw_email_str = str(raw_email_val) if raw_email_val is not None and isinstance(raw_email_val, str) else None
    raw_ach_str = str(raw_ach_val) if raw_ach_val is not None and isinstance(raw_ach_val, str) else None

    problems: List[str] = []

    # -------------------------------------------------------------
    # 1. Name Validation (1..100 chars, trimmed, no control chars)
    # -------------------------------------------------------------
    clean_name: Optional[str] = None
    if raw_name_val is None:
        problems.append("name: is required")
    elif not isinstance(raw_name_val, str):
        problems.append("name: must be a string")
    else:
        trimmed_name = raw_name_val.strip()
        if len(trimmed_name) == 0:
            problems.append("name: must not be empty")
        elif len(trimmed_name) > 100:
            problems.append("name: must not exceed 100 characters")
        elif has_control_characters(trimmed_name):
            problems.append("name: must not contain control characters")
        else:
            clean_name = trimmed_name

    # -------------------------------------------------------------
    # 2. Email Validation (required, valid format, lowercased)
    # -------------------------------------------------------------
    clean_email: Optional[str] = None
    if raw_email_val is None:
        problems.append("email: is required")
    elif not isinstance(raw_email_val, str):
        problems.append("email: must be a string")
    else:
        trimmed_email = raw_email_val.strip()
        if len(trimmed_email) == 0:
            problems.append("email: must not be empty")
        else:
            try:
                # Validate syntax without DNS lookup for speed and offline testing
                validated = validate_email(trimmed_email, check_deliverability=False)
                clean_email = validated.normalized.lower()
            except EmailNotValidError:
                problems.append("email: invalid format")

    # -------------------------------------------------------------
    # 3. Achievement Validation (optional, string <= 200 chars)
    # -------------------------------------------------------------
    clean_achievement: Optional[str] = None
    if raw_ach_val is not None:
        if not isinstance(raw_ach_val, str):
            problems.append("achievement: must be a string")
        else:
            trimmed_ach = raw_ach_val.strip()
            if len(trimmed_ach) > 200:
                problems.append("achievement: must not exceed 200 characters")
            else:
                clean_achievement = trimmed_ach if trimmed_ach else None

    # -------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------
    if problems:
        return ValidationResult(
            is_valid=False,
            raw_name=raw_name_str,
            raw_email=raw_email_str,
            raw_achievement=raw_ach_str,
            error_code=ErrorCode.VALIDATION_ERROR,
            error_message="; ".join(problems),
        )

    return ValidationResult(
        is_valid=True,
        clean_data={
            "name": clean_name,
            "email": clean_email,
            "achievement": clean_achievement,
        },
        raw_name=clean_name,
        raw_email=clean_email,
        raw_achievement=clean_achievement,
    )
