"""
POST /webhooks/inbound
Receives Postmark inbound email webhooks, validates the request,
and queues a job in async_jobs. All heavy processing is done by the worker.
"""

import base64
import logging
import uuid
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from app.models import PostmarkInboundPayload
from app.database import get_supabase
from app.config import get_settings
from app.services.limits import (
    get_organisation_by_username,
    is_sender_allowed,
    check_limits,
    LimitStatus,
)
from app.services.emailer import (
    send_not_authorised_email,
    send_no_attachment_email,
    send_limit_reached_email,
    send_limit_warning_email,
)

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/webhooks/inbound")
async def handle_inbound(request: Request):
    """
    Main inbound webhook handler. Called by Postmark for every email received.
    Returns 200 quickly — all processing is async.
    """
    settings = get_settings()
    supabase = get_supabase()

    # Parse the Postmark payload
    try:
        body = await request.json()
        payload = PostmarkInboundPayload(**body)
    except Exception as e:
        logger.error(f"Failed to parse inbound webhook payload: {e}")
        # Return 200 to prevent Postmark retrying a malformed payload
        return {"status": "ignored", "reason": "invalid_payload"}

    sender_email = payload.From
    recipient = payload.OriginalRecipient
    username = payload.agency_username()
    sender_domain = payload.sender_domain()

    logger.info(f"Inbound email from {sender_email} to {recipient}")

    # ── 1. Look up the organisation ───────────────────────────────────────────
    org = get_organisation_by_username(username)
    if not org:
        logger.warning(f"No active organisation found for username: {username}")
        # Don't send an email — this might be spam/probing
        return {"status": "ignored", "reason": "unknown_recipient"}

    # ── 2. Check sender is authorised ─────────────────────────────────────────
    if not is_sender_allowed(org, sender_domain):
        logger.warning(f"Sender {sender_email} not authorised for org {org.name}")
        send_not_authorised_email(sender_email, recipient)
        return {"status": "rejected", "reason": "sender_not_authorised"}

    # ── 3. Check for a valid CV attachment ────────────────────────────────────
    attachment = payload.first_cv_attachment()
    if not attachment:
        logger.info(f"No CV attachment found in email from {sender_email}")
        send_no_attachment_email(sender_email)
        return {"status": "rejected", "reason": "no_attachment"}

    # Check attachment size
    estimated_size = len(attachment.Content) * 3 // 4  # base64 decode estimate
    if estimated_size > MAX_SIZE_BYTES:
        logger.warning(f"Attachment too large from {sender_email}: ~{estimated_size // 1024}KB")
        send_no_attachment_email(sender_email)
        return {"status": "rejected", "reason": "attachment_too_large"}

    # ── 4. Check usage limits ─────────────────────────────────────────────────
    limit_check = check_limits(org)

    if limit_check.status == LimitStatus.EXCEEDED:
        logger.info(f"Limit exceeded for {org.name} ({org.cv_count}/{org.cv_limit})")
        send_limit_reached_email(sender_email, org.name, org.cv_limit)
        return {"status": "rejected", "reason": "limit_exceeded"}

    if limit_check.status == LimitStatus.WARNING:
        # Process the CV but also fire the warning email
        logger.info(f"Limit warning for {org.name} ({org.cv_count}/{org.cv_limit})")
        send_limit_warning_email(sender_email, org.name, org.cv_count, org.cv_limit)

    # ── 5. Store the input file in Supabase Storage ───────────────────────────
    job_id = str(uuid.uuid4())
    input_path = f"{org.id}/{job_id}/{attachment.Name}"

    try:
        file_bytes = base64.b64decode(attachment.Content)
        supabase.storage.from_("cv-inputs").upload(
            path=input_path,
            file=file_bytes,
            file_options={"content-type": attachment.ContentType},
        )
    except Exception as e:
        logger.error(f"Failed to upload input CV to storage: {e}")
        raise HTTPException(status_code=500, detail="Storage error")

    # ── 6. Queue the job ──────────────────────────────────────────────────────
    try:
        supabase.table("async_jobs").insert({
            "id": job_id,
            "org_id": org.id,
            "sender_email": sender_email,
            "original_filename": attachment.Name,
            "input_path": input_path,
            "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"Failed to queue job: {e}")
        raise HTTPException(status_code=500, detail="Queue error")

    logger.info(f"Job {job_id} queued for org {org.name} (sender: {sender_email})")
    return {"status": "queued", "job_id": job_id}
