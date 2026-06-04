"""
Async job worker.
Polls the async_jobs table for pending jobs and processes them one at a time.
Runs as a background task alongside the FastAPI server (via asyncio).

Formatting pipeline:
  1. Try to download org-builders/{org_id}/cv_builder.js from Supabase storage.
     If found, use the Node.js builder (build_cv_with_node).
  2. If no builder is uploaded for this org, fall back to the Python formatter
     (build_cv_docx) so the pipeline keeps working during onboarding.
"""

import asyncio
import logging
import io
from datetime import datetime, timezone

from app.config import get_settings
from app.database import get_supabase
from app.services.extractor import extract_text
from app.services.claude import parse_cv
from app.services.formatter import build_cv_docx
from app.services.node_formatter import build_cv_with_node
from app.services.emailer import send_formatted_cv, send_error_email
from app.services.limits import increment_cv_count

logger = logging.getLogger(__name__)


async def run_worker():
    """
    Main worker loop. Polls for pending jobs every `worker_poll_interval` seconds.
    Designed to run as an asyncio background task.
    """
    settings = get_settings()
    logger.info(f"Worker started. Polling every {settings.worker_poll_interval}s")

    while True:
        try:
            await process_next_job()
        except Exception as e:
            logger.error(f"Unexpected worker error: {e}", exc_info=True)

        await asyncio.sleep(settings.worker_poll_interval)


async def process_next_job():
    """
    Pick up the oldest pending job, process it end-to-end, and mark it complete or failed.
    If no jobs are pending, returns immediately.
    """
    supabase = get_supabase()

    # Claim the next pending job
    result = (
        supabase.table("async_jobs")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )

    if not result.data:
        return  # Nothing to do

    job = result.data[0]
    job_id            = job["id"]
    org_id            = job["org_id"]
    sender_email      = job["sender_email"]
    input_path        = job["input_path"]
    original_filename = job.get("original_filename", "cv.pdf")

    logger.info(f"Processing job {job_id} for org {org_id}")

    # Mark as processing
    supabase.table("async_jobs").update({"status": "processing"}).eq("id", job_id).execute()

    # ── Fetch style guide for this organisation ────────────────────────────────
    try:
        org_result = (
            supabase.table("organisations")
            .select("name, style_guide")
            .eq("id", org_id)
            .single()
            .execute()
        )

        if not org_result.data:
            raise ValueError(f"Organisation {org_id} not found")

        style_guide = org_result.data.get("style_guide") or {}
        org_name    = org_result.data.get("name", "Agency")
        logger.info(f"Style guide loaded for '{org_name}' "
                    f"({'custom' if style_guide else 'default'})")

    except Exception as e:
        await _fail_job(job_id, sender_email, f"Organisation fetch failed: {e}")
        return

    # ── Download the input CV from storage ─────────────────────────────────────
    try:
        file_bytes = supabase.storage.from_("cv-inputs").download(input_path)
    except Exception as e:
        await _fail_job(job_id, sender_email, f"Input file download failed: {e}")
        return

    # ── Extract text ───────────────────────────────────────────────────────────
    try:
        import base64
        content_type = (
            "application/pdf"
            if original_filename.lower().endswith(".pdf")
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        content_b64 = base64.b64encode(file_bytes).decode("utf-8")
        raw_text = extract_text(content_b64, content_type, original_filename)

    except Exception as e:
        await _fail_job(job_id, sender_email, f"Text extraction failed: {e}")
        return

    # ── Parse with Claude ──────────────────────────────────────────────────────
    try:
        parsed_cv, tokens_in, tokens_out = parse_cv(raw_text)
    except Exception as e:
        await _fail_job(job_id, sender_email, f"Claude parsing failed: {e}")
        return

    # ── Fetch branded header image (if configured in style guide) ─────────────
    header_image_bytes = None
    hdr_cfg    = style_guide.get("header", {})
    img_bucket = hdr_cfg.get("image_bucket")
    img_path   = hdr_cfg.get("image_path")
    if img_bucket and img_path:
        try:
            header_image_bytes = supabase.storage.from_(img_bucket).download(img_path)
            logger.info(f"Header image loaded: {img_bucket}/{img_path}")
        except Exception as e:
            logger.warning(f"Could not load header image {img_bucket}/{img_path}: {e}")

    # ── Try Node.js builder; fall back to Python formatter ────────────────────
    builder_js_bytes = None
    builder_path     = f"{org_id}/cv_builder.js"
    try:
        builder_js_bytes = supabase.storage.from_("org-builders").download(builder_path)
        logger.info(f"Node.js builder found for org {org_id}")
    except Exception:
        logger.info(f"No Node.js builder at org-builders/{builder_path} — using Python formatter")

    try:
        if builder_js_bytes:
            formatted_bytes = build_cv_with_node(
                parsed_cv, builder_js_bytes, header_image_bytes
            )
        else:
            formatted_bytes = build_cv_docx(parsed_cv, style_guide, header_image_bytes)
    except Exception as e:
        await _fail_job(job_id, sender_email, f"DOCX formatting failed: {e}")
        return

    # ── Upload formatted output ────────────────────────────────────────────────
    try:
        candidate_name  = parsed_cv.candidate.full_name or "Candidate"
        filename_format = style_guide.get("output", {}).get("filename_format", "")
        if filename_format:
            output_filename = filename_format.replace("{name}", candidate_name) + ".docx"
        else:
            output_filename = original_filename.rsplit(".", 1)[0] + "_formatted.docx"
        # Sanitize storage key: Supabase rejects en-dashes and other non-ASCII chars
        storage_filename = output_filename.encode("ascii", "ignore").decode().replace("  ", " ").strip()
        output_path = f"{org_id}/{job_id}/{storage_filename}"
        supabase.storage.from_("cv-outputs").upload(
            path=output_path,
            file=formatted_bytes,
            file_options={
                "content-type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            },
        )
    except Exception as e:
        await _fail_job(job_id, sender_email, f"Output upload failed: {e}")
        return

    # ── Send formatted CV by email ─────────────────────────────────────────────
    sent = send_formatted_cv(
        to_email=sender_email,
        candidate_name=candidate_name,
        docx_bytes=formatted_bytes,
        original_filename=output_filename,
    )

    if not sent:
        await _fail_job(job_id, sender_email, "Outbound email delivery failed")
        return

    # ── Mark complete ──────────────────────────────────────────────────────────
    supabase.table("async_jobs").update({
        "status":            "complete",
        "output_path":       output_path,
        "claude_tokens_in":  tokens_in,
        "claude_tokens_out": tokens_out,
        "completed_at":      datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    increment_cv_count(org_id)

    logger.info(
        f"Job {job_id} complete. Candidate: {candidate_name}. "
        f"Tokens: {tokens_in}/{tokens_out}. "
        f"Formatter: {'node.js' if builder_js_bytes else 'python'}"
    )


async def _fail_job(job_id: str, sender_email: str, error_message: str):
    """Mark a job as failed and send an error email to the consultant."""
    logger.error(f"Job {job_id} failed: {error_message}")

    supabase = get_supabase()
    supabase.table("async_jobs").update({
        "status":        "failed",
        "error_message": error_message,
        "completed_at":  datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    send_error_email(sender_email, reason=error_message)
