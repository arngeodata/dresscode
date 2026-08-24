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
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import get_supabase
from app.services.extractor import extract_text
from app.services.claude import parse_cv
from app.services.formatter import build_cv_docx
from app.services.node_formatter import build_cv_with_node
from app.services.emailer import send_formatted_cv, send_error_email
from app.services.limits import (
    increment_cv_count,
    build_usage_note,
    build_pilot_usage_note,
)
from app.services.trial_leads import send_daily_digest
from app.services.pilot_digest import send_pilot_digest

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


async def run_daily_digest():
    """
    Once per day (around settings.digest_hour_utc), email George the trial-lead
    digest and the pilot-cohort digest. Checks every 5 minutes; an in-memory
    guard prevents double-sends.

    Note: the guard resets on redeploy, so a redeploy during the digest hour
    could send a second copy that day — harmless. send_daily_digest() also
    sends nothing on a zero-lead day.
    """
    last_sent_date = None
    logger.info("Daily digest scheduler started.")

    while True:
        try:
            settings = get_settings()
            now = datetime.now(timezone.utc)
            if now.hour == settings.digest_hour_utc and last_sent_date != now.date():
                logger.info("Running daily trial-lead digest...")
                send_daily_digest()

                # Pilot cohort digest. Separate try/except so a failure in one
                # digest can't stop the other - they are independent reports.
                try:
                    logger.info("Running daily pilot digest...")
                    send_pilot_digest()
                except Exception as e:
                    logger.error(f"Pilot digest error: {e}", exc_info=True)

                last_sent_date = now.date()
        except Exception as e:
            logger.error(f"Daily digest error: {e}", exc_info=True)

        await asyncio.sleep(300)  # check every 5 minutes


# ── Retry policy ──────────────────────────────────────────────────────────────
# A CV is a customer's only copy. Before this, ANY error - a five second network
# blip, Postmark hiccuping, Claude returning a 529 - marked the job failed and
# DELETED the uploaded file. The consultant had to notice and resend.
#
# Now a transient error puts the job back in the queue with a backoff and keeps
# the file. An outage becomes a delay nobody notices. Only a genuinely broken
# input, or running out of attempts, deletes anything.
MAX_ATTEMPTS = 5
BACKOFF_MINUTES = [1, 5, 15, 45]        # wait before attempt 2, 3, 4, 5
LEASE_MINUTES = 15                      # a claimed job left this long is assumed
                                        # abandoned (worker crashed) and re-queued

# Substrings that mean "the other end had a moment", not "this input is bad".
_TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "connect", "reset by peer", "broken pipe",
    "temporarily unavailable", "service unavailable", "bad gateway", "gateway timeout",
    "overloaded", "rate limit", "rate_limit", "too many requests", "capacity",
    "429", "500", "502", "503", "504", "529",
    "internal server error", "remote disconnected", "eof occurred", "ssl",
)


# Substrings that mean "this input is broken" - retrying wastes 45 minutes and
# still tells the consultant to resend, so say so now.
_PERMANENT_HINTS = (
    "invalid json", "not valid json", "validation error", "validationerror",
    "unsupported", "corrupt", "password", "encrypted", "no text could be",
    "empty document", "cannot parse", "unable to extract",
)


def _is_transient(exc: Exception) -> bool:
    """
    Worth trying again, or not?

    Deliberately conservative: something we don't recognise is treated as
    transient, because retrying a permanent problem costs a few minutes and one
    email, while hard-failing a transient one destroys a customer's CV.
    """
    name = type(exc).__name__.lower()
    text = f"{name} {exc}".lower()
    if any(h in text for h in _PERMANENT_HINTS):
        return False
    if any(h in text for h in _TRANSIENT_HINTS):
        return True
    # Anything that looks like a network or HTTP client error
    if any(k in name for k in ("timeout", "connection", "network", "apierror",
                               "httperror", "readerror", "remoteprotocol")):
        return True
    return True   # default: give it another go


async def _requeue_job(job_id: str, attempts: int, error_message: str) -> bool:
    """
    Put a job back in the queue with a backoff. Keeps the uploaded file.
    Returns True if it was requeued, False if it is out of attempts.
    """
    if attempts >= MAX_ATTEMPTS:
        return False

    wait = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
    when = datetime.now(timezone.utc) + timedelta(minutes=wait)

    get_supabase().table("async_jobs").update({
        "status":          "pending",
        "attempts":        attempts,
        "next_attempt_at": when.isoformat(),
        "last_error":      error_message[:500],
    }).eq("id", job_id).execute()

    logger.warning(
        f"Job {job_id} attempt {attempts}/{MAX_ATTEMPTS} failed ({error_message[:120]}). "
        f"Retrying in {wait} min. CV retained."
    )
    return True


async def _handle_error(job_id, sender_email, attempts, label, exc, permanent=False):
    """
    Something went wrong mid-job. Retry it, or give up and tell the consultant.

    Every failure in process_next_job routes through here so there is exactly
    one place that decides, rather than nine scattered calls to _fail_job.
    """
    message = f"{label}: {exc}"
    if not permanent and _is_transient(exc):
        if await _requeue_job(job_id, attempts, message):
            return
        message = f"{message} (gave up after {MAX_ATTEMPTS} attempts)"
    await _fail_job(job_id, sender_email, message)


async def process_next_job():
    """
    Pick up the oldest pending job, process it end-to-end, and mark it complete or failed.
    If no jobs are pending, returns immediately.
    """
    supabase = get_supabase()
    now = datetime.now(timezone.utc)

    # Claim the oldest job that is due. "Due" means pending with no backoff
    # pending, OR left in 'processing' past its lease - which means the worker
    # that had it died, and nobody else is coming for it.
    result = (
        supabase.table("async_jobs")
        .select("*")
        .in_("status", ["pending", "processing"])
        .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now.isoformat()}")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )

    if not result.data:
        return  # Nothing due

    job = result.data[0]
    job_id            = job["id"]
    attempts          = (job.get("attempts") or 0) + 1
    org_id            = job["org_id"]
    sender_email      = job["sender_email"]
    input_path        = job["input_path"]
    reply_to_address  = job.get("reply_to_address")
    reply_subject     = job.get("reply_subject")
    reply_message_id  = job.get("reply_message_id")

    logger.info(f"Processing job {job_id} for org {org_id}")

    if job.get("status") == "processing":
        logger.warning(f"Job {job_id} was left in 'processing' - reclaiming (attempt {attempts})")

    # Mark as processing and take a lease. If this worker dies, the lease
    # expires and the job is picked up again rather than sitting there forever.
    supabase.table("async_jobs").update({
        "status":          "processing",
        "attempts":        attempts,
        "next_attempt_at": (now + timedelta(minutes=LEASE_MINUTES)).isoformat(),
    }).eq("id", job_id).execute()

    logger.info(f"Job {job_id} attempt {attempts}/{MAX_ATTEMPTS}")

    # ── Fetch style guide for this organisation ────────────────────────────────
    try:
        org_result = (
            supabase.table("organisations")
            .select("name, style_guide, cv_count, cv_limit, tier, email_username, trial_ends_at")
            .eq("id", org_id)
            .single()
            .execute()
        )

        if not org_result.data:
            raise ValueError(f"Organisation {org_id} not found")

        style_guide = org_result.data.get("style_guide") or {}
        org_name    = org_result.data.get("name", "Agency")
        org_cv_count = org_result.data.get("cv_count", 0) or 0
        org_cv_limit = org_result.data.get("cv_limit")
        org_tier     = org_result.data.get("tier", "")
        org_email_username = (org_result.data.get("email_username") or "")
        org_trial_ends_at  = org_result.data.get("trial_ends_at")
        # Pilot accounts: 25 CVs, 30 days, no package and no overage. They get
        # their own usage line and George's personal sign-off, not the brand's.
        is_pilot = (org_tier or "").lower() == "pilot"
        # Public trial jobs get NO "CV X/50 included in your package" line.
        is_trial_job = org_email_username.lower() == get_settings().trial_username.lower()
        # Slug used for human-readable Storage folder names (e.g. "Hyperion Partners" → "hyperion-partners")
        org_slug    = org_name.lower().replace(' ', '-').replace("'", '').replace('.', '')
        logger.info(f"Style guide loaded for '{org_name}' "
                    f"({'custom' if style_guide else 'default'})")

    except Exception as e:
        await _handle_error(job_id, sender_email, attempts, "Organisation fetch failed", e)
        return

    # ── Download the input CV from storage ─────────────────────────────────────
    try:
        file_bytes = supabase.storage.from_("cv-inputs").download(input_path)
    except Exception as e:
        await _handle_error(job_id, sender_email, attempts, "Input file download failed", e)
        return

    # ── Extract text ───────────────────────────────────────────────────────────
    try:
        import base64
        content_type = (
            "application/pdf"
            if input_path.lower().endswith(".pdf")
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        content_b64 = base64.b64encode(file_bytes).decode("utf-8")
        raw_text = extract_text(content_b64, content_type, input_path)

    except Exception as e:
        # A file we cannot read will not become readable on the fourth attempt.
        await _handle_error(job_id, sender_email, attempts, "Text extraction failed", e,
                            permanent=True)
        return

    # ── Parse with Claude ──────────────────────────────────────────────────────
    try:
        parsed_cv, tokens_in, tokens_out = parse_cv(raw_text)
    except Exception as e:
        # Overloaded/timeout -> retry. Malformed JSON after its own retries -> stop.
        await _handle_error(job_id, sender_email, attempts, "Claude parsing failed", e)
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
    builder_path     = f"{org_slug}/cv_builder.js"
    try:
        builder_js_bytes = supabase.storage.from_("org-builders").download(builder_path)
        logger.info(f"Node.js builder found for org {org_name} ({org_slug})")
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
        await _handle_error(job_id, sender_email, attempts, "DOCX formatting failed", e)
        return

    # ── Upload formatted output ────────────────────────────────────────────────
    try:
        raw_name        = parsed_cv.candidate.full_name or "Candidate"
        # Normalise to title case, handling hyphenated names (e.g. ALEXANDER-WALCOTT → Alexander-Walcott)
        candidate_name  = ' '.join(
            '-'.join(part.capitalize() for part in word.split('-'))
            for word in raw_name.split()
        )
        filename_format = style_guide.get("output", {}).get("filename_format", "")
        if filename_format:
            output_filename = filename_format.replace("{name}", candidate_name) + ".docx"
        else:
            output_filename = f"{candidate_name} - CV.docx"
        # Neutral storage name — no candidate data in storage or the DB. The
        # nicely-named file (output_filename) is used only as the email attachment.
        output_path = f"{org_id}/{job_id}/output.docx"
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
        await _handle_error(job_id, sender_email, attempts, "Output upload failed", e)
        return

    # ── Send formatted CV by email ─────────────────────────────────────────────
    # Usage line for the email reflects the count AFTER this CV (current + 1).
    # Suppressed for public trial jobs (the recipient isn't a paying customer).
    cv_number = org_cv_count + 1
    if is_trial_job:
        usage_note = ""
    elif is_pilot:
        usage_note = build_pilot_usage_note(cv_number, org_cv_limit, org_trial_ends_at)
    else:
        usage_note = build_usage_note(org_tier, cv_number, org_cv_limit)

    # Trial replies (only) get a soft CTA + booking link — the recipient just engaged.
    trial_cta = (
        "Want this on every CV your team sends, in your agency's own style? "
        "Just reply and I'll set it up.\n\n"
        "Prefer a quick chat? Grab a time here: https://cal.com/cvdresscode/30min"
    ) if is_trial_job else ""

    # Pilots: ask for the template feedback on the first three CVs only. After
    # that it's wallpaper, and a pilot inbox isn't monitored for replies — the
    # booking link in the signature is the only route back to a human.
    signature = ""
    if is_pilot:
        if cv_number <= 3:
            trial_cta = "Anything you'd change about how it looks? Book a call with me below."
        signature = (
            "George\n"
            f"{get_settings().dresscode_support_email}\n"
            "Book a call with me -  https://cal.com/cvdresscode/30min"
        )
    sent = send_formatted_cv(
        to_email=sender_email,
        candidate_name=candidate_name,
        docx_bytes=formatted_bytes,
        original_filename=output_filename,
        usage_note=usage_note,
        from_address=reply_to_address,
        reply_subject=reply_subject,
        reply_message_id=reply_message_id,
        trial_cta=trial_cta,
        signature=signature,
    )

    if not sent:
        await _handle_error(job_id, sender_email, attempts, "Outbound email delivery failed",
                            RuntimeError("Postmark did not accept the message"))
        return

    # ── Mark complete ──────────────────────────────────────────────────────────
    supabase.table("async_jobs").update({
        "status":            "complete",
        "next_attempt_at":   None,     # release the lease
        "output_path":       output_path,
        "claude_tokens_in":  tokens_in,
        "claude_tokens_out": tokens_out,
        "completed_at":      datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    increment_cv_count(org_id)

    # ── Delete-after-delivery (GDPR: "processed transiently, nothing retained") ──
    # The CV has been emailed (from in-memory bytes), so we can safely delete the
    # stored original + formatted copies and scrub candidate-identifying metadata.
    # Runs AFTER send, so it never delays delivery. Failures here are non-fatal.
    try:
        supabase.storage.from_("cv-inputs").remove([input_path])
        supabase.storage.from_("cv-outputs").remove([output_path])
    except Exception as e:
        logger.error(f"Post-delivery storage cleanup failed for job {job_id}: {e}")

    try:
        # Keep token counts / status / timestamps for billing; drop candidate PII.
        supabase.table("async_jobs").update({
            "original_filename": None,
            "input_path":        "deleted",
            "output_path":       "deleted",
            "reply_subject":     None,
            "reply_message_id":  None,
        }).eq("id", job_id).execute()
    except Exception as e:
        logger.error(f"Post-delivery metadata scrub failed for job {job_id}: {e}")

    logger.info(
        f"Job {job_id} complete. "
        f"Tokens: {tokens_in}/{tokens_out}. "
        f"Formatter: {'node.js' if builder_js_bytes else 'python'}. "
        f"Candidate data deleted after delivery."
    )


async def _fail_job(job_id: str, sender_email: str, error_message: str):
    """
    Mark a job as failed, DELETE its uploaded CV (failed jobs must not retain
    candidate data either), and send an error email to the consultant.
    """
    logger.error(f"Job {job_id} failed: {error_message}")

    supabase = get_supabase()

    # Delete any stored candidate data for this failed job (best-effort).
    try:
        row = (
            supabase.table("async_jobs")
            .select("input_path, org_id")
            .eq("id", job_id)
            .single()
            .execute()
        )
        data = row.data or {}
        in_path = data.get("input_path")
        fail_org_id = data.get("org_id")
        if in_path and in_path != "deleted":
            supabase.storage.from_("cv-inputs").remove([in_path])
        if fail_org_id:
            # Output may exist if failure occurred after upload (e.g. email send).
            supabase.storage.from_("cv-outputs").remove([f"{fail_org_id}/{job_id}/output.docx"])
    except Exception as e:
        logger.error(f"Failed-job cleanup error for {job_id}: {e}")

    supabase.table("async_jobs").update({
        "status":        "failed",
        "error_message": error_message,
        "input_path":    "deleted",
        "completed_at":  datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    send_error_email(sender_email, reason=error_message)
