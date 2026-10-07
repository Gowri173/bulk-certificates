"""ReportLab-based PDF certificate generator for A4 landscape certificates."""

import io
from typing import Optional
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from app.generator.base import CertificateData, CertificateGenerator


# NOTE on Fonts:
# Built-in PDF fonts (Helvetica, Times-Roman, Courier) only support standard Latin-1 encodings.
# For full non-Latin script support (e.g. Cyrillic, Devanagari, Arabic, CJK), register a bundled
# TrueType font (TTF) using reportlab.pdfbase.ttfonts.TTFont before drawing text.


class ReportLabCertificateGenerator:
    """Renders high-quality, standardized PDF certificates on A4 landscape."""

    def __init__(self) -> None:
        self.page_width, self.page_height = landscape(A4)

    def _fit_text(
        self,
        text: str,
        font_name: str,
        initial_size: float,
        min_size: float,
        max_width: float,
    ) -> tuple[str, float]:
        """Auto-shrink font size to fit within max_width.

        If text still overflows at min_size, truncate with an ellipsis.
        """
        size = initial_size
        while size > min_size:
            width = stringWidth(text, font_name, size)
            if width <= max_width:
                return text, size
            size -= 1.0

        # At minimum size, truncate with ellipsis if necessary
        truncated = text
        while truncated and stringWidth(f"{truncated}...", font_name, min_size) > max_width:
            truncated = truncated[:-1]

        if len(truncated) < len(text):
            return f"{truncated}...", min_size

        return text, min_size

    def generate(self, data: CertificateData) -> bytes:
        """Render a single certificate and return PDF binary bytes."""
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=(self.page_width, self.page_height))

        # -------------------------------------------------------------
        # 1. Decorative Borders
        # -------------------------------------------------------------
        outer_margin = 24.0
        inner_margin = 32.0

        # Outer border: Deep Navy
        c.setStrokeColor(colors.HexColor("#1A365D"))
        c.setLineWidth(3.0)
        c.rect(
            outer_margin,
            outer_margin,
            self.page_width - 2 * outer_margin,
            self.page_height - 2 * outer_margin,
        )

        # Inner border: Gold Accent
        c.setStrokeColor(colors.HexColor("#C5A059"))
        c.setLineWidth(1.2)
        c.rect(
            inner_margin,
            inner_margin,
            self.page_width - 2 * inner_margin,
            self.page_height - 2 * inner_margin,
        )

        # Subtle corner accents
        c.setFillColor(colors.HexColor("#C5A059"))
        accent_size = 8.0
        for x, y in [
            (inner_margin + 4, inner_margin + 4),
            (self.page_width - inner_margin - 4 - accent_size, inner_margin + 4),
            (inner_margin + 4, self.page_height - inner_margin - 4 - accent_size),
            (self.page_width - inner_margin - 4 - accent_size, self.page_height - inner_margin - 4 - accent_size),
        ]:
            c.rect(x, y, accent_size, accent_size, fill=1, stroke=0)

        center_x = self.page_width / 2.0
        max_text_width = self.page_width - 160.0

        # -------------------------------------------------------------
        # 2. Certificate Header / Title
        # -------------------------------------------------------------
        title_text, title_size = self._fit_text(
            data.title.upper(), "Helvetica-Bold", 26.0, 16.0, max_text_width
        )
        c.setFont("Helvetica-Bold", title_size)
        c.setFillColor(colors.HexColor("#1A365D"))
        c.drawCentredString(center_x, 480.0, title_text)

        # Horizontal accent line under title
        c.setStrokeColor(colors.HexColor("#C5A059"))
        c.setLineWidth(1.0)
        c.line(center_x - 120.0, 465.0, center_x + 120.0, 465.0)

        # -------------------------------------------------------------
        # 3. Certification Lead-in
        # -------------------------------------------------------------
        c.setFont("Helvetica-Oblique", 13.0)
        c.setFillColor(colors.HexColor("#4A5568"))
        c.drawCentredString(center_x, 430.0, "This is to certify that")

        # -------------------------------------------------------------
        # 4. Recipient Name (Auto-Shrinking & Truncation)
        # -------------------------------------------------------------
        name_text, name_size = self._fit_text(
            data.recipient_name, "Helvetica-Bold", 32.0, 16.0, max_text_width
        )
        c.setFont("Helvetica-Bold", name_size)
        c.setFillColor(colors.HexColor("#1A202C"))
        c.drawCentredString(center_x, 370.0, name_text)

        # Decorative line below name
        name_line_width = min(stringWidth(name_text, "Helvetica-Bold", name_size) + 40.0, max_text_width)
        c.setStrokeColor(colors.HexColor("#E2E8F0"))
        c.setLineWidth(1.0)
        c.line(center_x - (name_line_width / 2.0), 355.0, center_x + (name_line_width / 2.0), 355.0)

        # -------------------------------------------------------------
        # 5. Achievement (Optional)
        # -------------------------------------------------------------
        has_achievement = bool(data.achievement and data.achievement.strip())
        current_y = 325.0

        if has_achievement:
            ach_text, ach_size = self._fit_text(
                data.achievement.strip(), "Helvetica-Bold", 13.0, 10.0, max_text_width
            )
            c.setFont("Helvetica-Bold", ach_size)
            c.setFillColor(colors.HexColor("#2B6CB0"))
            c.drawCentredString(center_x, current_y, ach_text)
            current_y -= 30.0

        # -------------------------------------------------------------
        # 6. Completion Clause & Event Name
        # -------------------------------------------------------------
        c.setFont("Helvetica", 13.0)
        c.setFillColor(colors.HexColor("#4A5568"))
        c.drawCentredString(center_x, current_y, "has successfully completed")
        current_y -= 35.0

        event_text, event_size = self._fit_text(
            data.event_name, "Helvetica-Bold", 20.0, 12.0, max_text_width
        )
        c.setFont("Helvetica-Bold", event_size)
        c.setFillColor(colors.HexColor("#1A365D"))
        c.drawCentredString(center_x, current_y, event_text)

        # -------------------------------------------------------------
        # 7. Issuer and Date Information
        # -------------------------------------------------------------
        date_str = data.issued_on.strftime("%B %d, %Y")
        if data.issuer_name and data.issuer_name.strip():
            # Dual column footer
            left_center = self.page_width * 0.32
            right_center = self.page_width * 0.68

            # Left: Date
            c.setFont("Helvetica-Bold", 11.0)
            c.setFillColor(colors.HexColor("#2D3748"))
            c.drawCentredString(left_center, 130.0, date_str)
            c.setFont("Helvetica", 9.5)
            c.setFillColor(colors.HexColor("#718096"))
            c.drawCentredString(left_center, 115.0, "Date of Issue")

            # Right: Issuer
            issuer_text, issuer_size = self._fit_text(
                data.issuer_name.strip(), "Helvetica-Bold", 11.0, 9.0, 200.0
            )
            c.setFont("Helvetica-Bold", issuer_size)
            c.setFillColor(colors.HexColor("#2D3748"))
            c.drawCentredString(right_center, 130.0, issuer_text)
            c.setFont("Helvetica", 9.5)
            c.setFillColor(colors.HexColor("#718096"))
            c.drawCentredString(right_center, 115.0, "Issuing Authority")
        else:
            # Centered date
            c.setFont("Helvetica-Bold", 11.0)
            c.setFillColor(colors.HexColor("#2D3748"))
            c.drawCentredString(center_x, 130.0, date_str)
            c.setFont("Helvetica", 9.5)
            c.setFillColor(colors.HexColor("#718096"))
            c.drawCentredString(center_x, 115.0, "Date of Issue")

        # -------------------------------------------------------------
        # 8. Serial Number & Authenticity Footer
        # -------------------------------------------------------------
        # Placed clearly above the gold corner accent with legible contrast
        c.setFont("Helvetica", 9.0)
        c.setFillColor(colors.HexColor("#4A5568"))
        c.drawString(
            inner_margin + 20.0,
            inner_margin + 14.0,
            f"Certificate ID: {data.serial_number}",
        )

        c.showPage()
        c.save()

        buffer.seek(0)
        return buffer.getvalue()


_default_generator = ReportLabCertificateGenerator()


def get_generator() -> CertificateGenerator:
    """Return the active CertificateGenerator instance.

    Allows monkeypatching during tests.
    """
    return _default_generator
