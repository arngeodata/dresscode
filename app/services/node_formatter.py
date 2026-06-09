"""
Node.js CV formatter runner.

Downloads the org's cv_builder.js from Supabase storage, writes it to a
temp directory alongside the parsed CV JSON and header image, then runs
it with Node.js and returns the resulting DOCX bytes.

Storage convention: org-builders/{org_id}/cv_builder.js
"""

import json
import logging
import os
import subprocess
import tempfile

from app.models import ParsedCV

logger = logging.getLogger(__name__)

# NODE_PATH is set in the Dockerfile to /app/node_modules so that
# cv_builder.js scripts can require('docx') without a local install.
_NODE_PATH = os.environ.get("NODE_PATH", "/app/node_modules")


def build_cv_with_node(
    parsed_cv: ParsedCV,
    builder_js_bytes: bytes,
    header_image_bytes: bytes | None = None,
    timeout: int = 30,
) -> bytes:
    """
    Run the org's cv_builder.js with the parsed CV data.

    Args:
        parsed_cv:           Structured CV data from Claude parsing.
        builder_js_bytes:    The org's cv_builder.js script as bytes.
        header_image_bytes:  Raw PNG bytes for the branded header image,
                             or None if the org has no header image.
        timeout:             Maximum seconds to allow the Node.js process.

    Returns:
        Raw DOCX bytes ready for upload and email attachment.

    Raises:
        RuntimeError: If the Node.js process exits non-zero or times out.
    """
    with tempfile.TemporaryDirectory() as tmpdir:

        # 1. Write the builder script
        builder_path = os.path.join(tmpdir, "cv_builder.js")
        with open(builder_path, "wb") as f:
            f.write(builder_js_bytes)

        # 2. Write the parsed CV as JSON
        cv_path = os.path.join(tmpdir, "parsed_cv.json")
        with open(cv_path, "w", encoding="utf-8") as f:
            json.dump(parsed_cv.model_dump(), f, ensure_ascii=False)

        # 3. Write header image (optional)
        header_path = ""
        if header_image_bytes:
            header_path = os.path.join(tmpdir, "header.png")
            with open(header_path, "wb") as f:
                f.write(header_image_bytes)

        # 4. Output path
        output_path = os.path.join(tmpdir, "output.docx")

        # 5. Run: node cv_builder.js <cv_json> <header_img_or_empty> <output>
        env = {**os.environ, "NODE_PATH": _NODE_PATH}
        try:
            result = subprocess.run(
                ["node", builder_path, cv_path, header_path, output_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmpdir,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"cv_builder.js timed out after {timeout}s"
            )

        if result.returncode != 0:
            stderr = (result.stderr or "")[:600]
            logger.error(f"cv_builder.js stderr: {stderr}")
            raise RuntimeError(
                f"cv_builder.js exited {result.returncode}: {stderr}"
            )

        if not os.path.exists(output_path):
            raise RuntimeError(
                "cv_builder.js completed successfully but produced no output file"
            )

        with open(output_path, "rb") as f:
            docx_bytes = f.read()

        logger.info(f"Node.js builder produced {len(docx_bytes):,} bytes")
        return docx_bytes
