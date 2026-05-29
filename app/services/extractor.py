"""
CV text extraction from PDF and DOCX files.
Returns plain text suitable for passing to the Claude API.
"""

import base64
import io
import logging

logger = logging.getLogger(__name__)


def extract_text(content_b64: str, content_type: str, filename: str) -> str:
    """
    Extract plain text from a base64-encoded CV file.

    Args:
        content_b64: Base64-encoded file content from Postmark attachment
        content_type: MIME type from Postmark (e.g. 'application/pdf')
        filename: Original filename — used as fallback to determine type

    Returns:
        Plain text content of the CV

    Raises:
        ValueError: If file type is unsupported or extraction fails
    """
    file_bytes = base64.b64decode(content_b64)
    ct = content_type.lower()
    fn = filename.lower()

    if "pdf" in ct or fn.endswith(".pdf"):
        return _extract_from_pdf(file_bytes)
    elif "word" in ct or "openxmlformats" in ct or fn.endswith(".docx") or fn.endswith(".doc"):
        return _extract_from_docx(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {content_type} / {filename}")


def _extract_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF using pdfminer."""
    try:
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams

        output = io.StringIO()
        extract_text_to_fp(
            io.BytesIO(file_bytes),
            output,
            laparams=LAParams(),
            output_type="text",
            codec="utf-8",
        )
        text = output.getvalue().strip()

        if not text:
            raise ValueError("PDF appears to be scanned/image-only — no extractable text found.")

        return text

    except ImportError:
        raise RuntimeError("pdfminer.six is not installed. Run: pip install pdfminer.six")
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        raise ValueError(f"Could not extract text from PDF: {e}") from e


def _extract_from_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX using python-docx."""
    try:
        from docx import Document

        doc = Document(io.BytesIO(file_bytes))
        paragraphs = []

        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                paragraphs.append(text)

        # Also extract text from tables
        for table in doc.tables:
            for row in table.rows:
                row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if row_texts:
                    paragraphs.append(" | ".join(row_texts))

        text = "\n".join(paragraphs).strip()

        if not text:
            raise ValueError("DOCX appears to be empty — no extractable text found.")

        return text

    except ImportError:
        raise RuntimeError("python-docx is not installed. Run: pip install python-docx")
    except Exception as e:
        logger.error(f"DOCX extraction failed: {e}")
        raise ValueError(f"Could not extract text from DOCX: {e}") from e
