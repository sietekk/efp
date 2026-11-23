"""Synthetic PDFs for the documents tests. Nothing here comes from a real filing: the text is
invented, and the names are placeholders ("Alex Example", "Sam Sample")."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pypdf import PdfReader, PdfWriter

PAGE_W, PAGE_H = 612, 792  # US Letter, in points

LINES_A = [
    "UNITED STATES DISTRICT COURT FOR THE EXAMPLE DISTRICT",
    "The defendant, Alex Example, pleaded guilty to one count of making a false statement",
    "to a federal agency. The court accepted the plea after reviewing the written agreement",
    "of the parties and ordered a presentence report. Sentencing was set for the spring term.",
    "The defendant was released on bond pending sentencing under the conditions stated.",
]
LINES_B = [
    "The government and the defendant, Sam Sample, filed a joint stipulation regarding",
    "the facts of the case. The stipulation states that the registration form was submitted",
    "by mail and that the county clerk received it before the stated deadline.",
    "The court approved the stipulation and dismissed the remaining counts of the information.",
]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def born_digital(lines_per_page: list[list[str]]) -> bytes:
    """A PDF with a real text layer (Helvetica), one entry per page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # patched below
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids: list[int] = []
    for lines in lines_per_page:
        stream = ["BT", "/F1 11 Tf", "14 TL", f"54 {PAGE_H - 72} Td"]
        for line in lines:
            stream.append(f"({_escape(line)}) Tj T*")
        stream.append("ET")
        data = "\n".join(stream).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
        page = add(
            f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        kids.append(page)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    kid_refs = " ".join(f"{k} 0 R" for k in kids)
    objects[pages_obj - 1] = f"<< /Type /Pages /Kids [{kid_refs}] /Count {len(kids)} >>".encode()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _page_image(lines: list[str], dpi: int = 200, size: int = 15) -> Image.Image:
    scale = dpi / 72
    image = Image.new("L", (round(PAGE_W * scale), round(PAGE_H * scale)), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=round(size * scale / 1.6))
    y = round(72 * scale)
    for line in lines:
        draw.text((round(54 * scale), y), line, fill=0, font=font)
        y += round(20 * scale)
    return image


def _pdf_from_images(images: list[Image.Image], dpi: int) -> bytes:
    buffer = io.BytesIO()
    first, rest = images[0].convert("RGB"), [i.convert("RGB") for i in images[1:]]
    first.save(buffer, format="PDF", save_all=True, append_images=rest, resolution=dpi)
    return buffer.getvalue()


def image_only(lines_per_page: list[list[str]], dpi: int = 200) -> bytes:
    """Pages that are only a picture of the text: no text layer."""
    return _pdf_from_images([_page_image(lines, dpi) for lines in lines_per_page], dpi)


def degraded(lines_per_page: list[list[str]], dpi: int = 200) -> bytes:
    """The same pictures, thrown to a third of the resolution, blurred and noised."""
    out = []
    for lines in lines_per_page:
        image = _page_image(lines, dpi)
        small = image.resize((image.width // 3, image.height // 3)).filter(
            ImageFilter.GaussianBlur(1.4)
        )
        out.append(small.resize(image.size))
    return _pdf_from_images(out, dpi)


def blank_page(dpi: int = 100) -> bytes:
    return _pdf_from_images(
        [Image.new("L", (round(PAGE_W * dpi / 72), round(PAGE_H * dpi / 72)), 255)], dpi
    )


def merge(*pdfs: bytes) -> bytes:
    """One PDF made of every page of each input, in order (a mixed document)."""
    writer = PdfWriter()
    for data in pdfs:
        for page in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
