FROM python:3.11-slim

# ── Install Node.js 22 ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Install LibreOffice (headless) for reading messy Word INPUT files ─────────
# Converts .docx/.doc/.rtf → PDF so text-box / legacy .doc content is captured
# (python-docx silently drops it). Output CVs are still written by the builder.
# libreoffice-writer pulls the minimal core; fonts-liberation avoids font warnings.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── PDF text extraction + OCR fallback ───────────────────────────────────────
# poppler-utils is REQUIRED ON THE MAIN PATH, not just for OCR. It provides:
#   pdftotext — extractor.py's first-choice PDF reader, run with -layout so a
#               CV that sets its Education or Skills block out in columns keeps
#               its rows intact. Without it we fall back to pdfplumber, and
#               without that to pdfminer, which flattens columns into separate
#               lists and mis-pairs dates with qualifications.
#   pdftoppm  — pdf2image's rasteriser, used only by the OCR fallback.
# tesseract-ocr is the OCR engine, used only when a PDF has no text layer.
# DO NOT drop poppler-utils if OCR is ever turned off.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Node.js dependencies (docx library for cv_builder.js scripts) ─────────────
COPY package.json .
RUN npm install --omit=dev

# NODE_PATH lets cv_builder.js scripts require('docx') without a local install
ENV NODE_PATH=/app/node_modules

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# ── Start ─────────────────────────────────────────────────────────────────────
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
