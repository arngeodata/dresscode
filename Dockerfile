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

# ── OCR fallback for scanned/image-only PDFs ─────────────────────────────────
# tesseract-ocr = OCR engine; poppler-utils = pdf2image's pdftoppm rasteriser.
# Only used when a PDF has no text layer (scanned/photographed CVs).
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
