"""Base protocols and dataclasses for certificate generation."""

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol


@dataclass(frozen=True)
class CertificateData:
    """Immutable data required to render a certificate."""

    recipient_name: str
    achievement: Optional[str]
    event_name: str
    title: str
    issuer_name: Optional[str]
    issued_on: date
    serial_number: str


class CertificateGenerator(Protocol):
    """Protocol defining the interface for certificate renderers."""

    def generate(self, data: CertificateData) -> bytes:
        """Render certificate data into PDF byte content.

        Args:
            data: The CertificateData values to render.

        Returns:
            Raw PDF bytes.
        """
        ...
