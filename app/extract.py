"""Extracción de texto de PDF, DOCX y texto plano."""
import io

MAX_CHARS = 24_000  # ~9k tokens: suficiente para un TFI, evita facturas sorpresa
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class ExtractionError(Exception):
    pass


def extract_text(filename: str, data: bytes) -> tuple[str, bool]:
    """Devuelve (texto, truncado)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ExtractionError("El archivo supera el máximo de 15 MB.")
    name = (filename or "").lower()

    if name.endswith(".pdf"):
        text = _from_pdf(data)
    elif name.endswith(".docx"):
        text = _from_docx(data)
    elif name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ExtractionError("Formato no soportado. Subí un PDF, DOCX, TXT o MD.")

    text = text.strip()
    if not text:
        raise ExtractionError(
            "No se pudo extraer texto del archivo. Si es un PDF escaneado, exportalo con texto seleccionable."
        )
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, False


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"No se pudo leer el PDF: {exc}") from exc


def _from_docx(data: bytes) -> str:
    from docx import Document

    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"No se pudo leer el DOCX: {exc}") from exc
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)
