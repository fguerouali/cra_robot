"""The PNG signature from Settings, stamped on the bottom-right of the monthly PDF."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from paths import HAND_FONT, SIGNATURE

MAX_BYTES = 2 * 1024 * 1024
MARGIN = 40
MAX_WIDTH = 280
MAX_HEIGHT = 96
LINE_GAP = 6
BLOCK_GAP = 14
DATE_SIZE = 14
NAME_SIZE = 20
_PNG = b"\x89PNG\r\n\x1a\n"
_FONT = "IndieFlower"
_FONT_FILES = (str(HAND_FONT),)


def signature_status() -> dict:
    if not SIGNATURE.is_file() or SIGNATURE.stat().st_size == 0:
        return {"saved": False, "updated_at": None}
    updated = datetime.fromtimestamp(SIGNATURE.stat().st_mtime, timezone.utc).isoformat()
    return {"saved": True, "updated_at": updated}


def has_signature() -> bool:
    return signature_status()["saved"]


def save_signature(data: bytes) -> dict:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Choose a PNG image up to 2 MB")
    if not data.startswith(_PNG):
        raise ValueError("Choose a PNG image")
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.format != "PNG" or image.width < 1 or image.height < 1:
                raise ValueError("Choose a PNG image")
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError):
        raise ValueError("That image could not be read. Choose a PNG file.")
    temporary = SIGNATURE.with_suffix(".png.saving")
    temporary.write_bytes(data)
    temporary.replace(SIGNATURE)
    return signature_status()


def remove_signature() -> dict:
    SIGNATURE.unlink(missing_ok=True)
    return signature_status()


def sign_pdf(
    path: Path,
    name: str = "",
    include_name: bool = True,
    include_datetime: bool = True,
) -> None:
    """Draw the Settings stamp (date, name, signature) on every page of the monthly PDF."""
    if not has_signature():
        return
    label = " ".join((name or "").split())
    when = _stamp_date() if include_datetime else ""
    try:
        reader = PdfReader(str(path))
        if not reader.pages:
            raise RuntimeError("empty")
        writer = PdfWriter()
        writer.append(reader)
        for page in writer.pages:
            if page.rotation:
                page.transfer_rotation_to_content()
            page.merge_page(_overlay(page, when, label if include_name else ""))
        temporary = path.with_name(path.name + ".signing")
        try:
            with temporary.open("wb") as handle:
                writer.write(handle)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        writer.close()
    except RuntimeError as exc:
        if str(exc) == "empty":
            raise RuntimeError("Could not add your signature. The exported PDF has no pages.") from None
        raise
    except Exception:
        raise RuntimeError("Could not add your signature. Replace the image in Settings and try again.") from None


def _overlay(page, when: str, name: str) -> object:
    box = page.mediabox
    left, bottom = float(box.left), float(box.bottom)
    width, height = float(box.width), float(box.height)
    draw_w, draw_h = _fitted_size(width, height)
    packet = BytesIO()
    drawing = canvas.Canvas(packet, pagesize=(width, height))
    font = _text_font()
    label = _fit_text(drawing, font, NAME_SIZE, name, width) if name else ""
    block_w = max(
        draw_w,
        drawing.stringWidth(when, font, DATE_SIZE) if when else 0,
        drawing.stringWidth(label, font, NAME_SIZE) if label else 0,
    )
    center = width - MARGIN - block_w / 2
    y = MARGIN
    drawing.drawImage(
        str(SIGNATURE),
        center - draw_w / 2,
        y,
        width=draw_w,
        height=draw_h,
        mask="auto",
        preserveAspectRatio=True,
        anchor="sw",
    )
    y += draw_h + BLOCK_GAP
    if label:
        drawing.setFillColorRGB(0.063, 0.094, 0.157)
        drawing.setFont(font, NAME_SIZE)
        drawing.drawCentredString(center, y, label)
        y += NAME_SIZE + LINE_GAP
    if when:
        drawing.setFillColorRGB(0.400, 0.439, 0.522)
        drawing.setFont(font, DATE_SIZE)
        drawing.drawCentredString(center, y, when)
    drawing.save()
    packet.seek(0)
    stamp = PdfReader(packet).pages[0]
    if left or bottom:
        stamp.add_transformation(Transformation().translate(left, bottom))
        stamp.mediabox = RectangleObject((left, bottom, left + width, bottom + height))
    return stamp


def _fitted_size(page_w: float, page_h: float) -> tuple[float, float]:
    with Image.open(SIGNATURE) as image:
        img_w, img_h = image.size
    available_w = max(page_w - 2 * MARGIN, 1)
    available_h = max(page_h - 2 * MARGIN, 1)
    scale = min(MAX_WIDTH / img_w, MAX_HEIGHT / img_h, available_w / img_w, available_h / img_h)
    return img_w * scale, img_h * scale


def _stamp_date(now: datetime | None = None) -> str:
    """Date on the stamp, as written on a form: 30/10/2026."""
    return (now or datetime.now()).strftime("%d/%m/%Y")


def _text_font() -> str:
    return _register_font(_FONT, _FONT_FILES, "Helvetica")


def _register_font(name: str, files: tuple[str, ...], fallback: str) -> str:
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    for path in files:
        if Path(path).is_file():
            pdfmetrics.registerFont(TTFont(name, path))
            return name
    return fallback


def _fit_text(drawing: canvas.Canvas, font: str, size: float, text: str, page_w: float) -> str:
    limit = max(page_w - 2 * MARGIN, 1)
    if drawing.stringWidth(text, font, size) <= limit:
        return text
    ellipsis = "..." if font == "Helvetica" else "…"
    for length in range(len(text) - 1, 0, -1):
        clipped = text[:length].rstrip() + ellipsis
        if drawing.stringWidth(clipped, font, size) <= limit:
            return clipped
    return ellipsis
