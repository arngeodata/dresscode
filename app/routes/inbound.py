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
    get_organisation_by_domain,
    get_organisation_by_email_username,
    check_limits,
    LimitStatus,
)
from app.services.emailer import (
    send_not_authorised_email,
    send_no_attachment_email,
    send_limit_warning_email,
)
from app.services.trial_leads import record_trial_lead, extract_phone

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
    sender_domain = payload.sender_domain()

    logger.info(f"Inbound email from {sender_email} to {recipient}")

    # Is this the public trial inbox (trial@cvdresscode.com)?
    # ONLY a recipient whose local-part matches settings.trial_username triggers
    # trial mode. Every other address falls through to normal customer auth below.
    recipient_local = (recipient or "").split("@")[0].strip().lower()
    is_trial = recipient_local == settings.trial_username.lower()

    # ── 1. Resolve the organisation ────────────────────────────────────────────
    if is_trial:
        # Trial mode: public lead magnet — skip FROM-domain auth, use the 'trial' org.
        org = get_organisation_by_email_username(settings.trial_username)
        if not org:
            logger.error(
                f"Trial org (email_username='{settings.trial_username}') not found — "
                f"cannot process trial CV from {sender_email}"
            )
            return {"status": "ignored", "reason": "trial_org_missing"}
        logger.info(f"Trial request from {sender_email} → routed to '{org.name}' org")
        # Capture the lead (best-effort — never blocks the CV).
        record_trial_lead(
            email=sender_email,
            name=payload.FromName,
            domain=sender_domain,
            phone=extract_phone(payload.TextBody),
        )
    else:
        # Normal mode: finding the org by sender domain IS the auth check.
        org = get_organisation_by_domain(sender_domain)
        if not org:
            logger.warning(f"No active organisation found for sender domain: {sender_domain}")
            # Don't send an email back — could be spam/probing
            return {"status": "ignored", "reason": "unknown_sender_domain"}

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

    # ── 4. Check usage limits (customer orgs only — never for the public trial) ─
    # Pricing model is "flat fee + overage" — we no longer reject at the cap.
    # Every CV is processed; usage above the included allowance is billed per-CV
    # via Stripe metering (see limits.increment_cv_count / billing.report_cv_usage).
    if not is_trial:
        limit_check = check_limits(org)

        if limit_check.status == LimitStatus.OVER_CAP:
            # Over the included allowance — process anyway, overage is metered/billed.
            logger.info(
                f"Overage for {org.name} ({org.cv_count}/{org.cv_limit}) — processing, billing per CV"
            )

        elif limit_check.status == LimitStatus.APPROACHING_CAP:
            # Process the CV and warn that the included allowance is nearly used.
            logger.info(f"Approaching allowance for {org.name} ({org.cv_count}/{org.cv_limit})")
            send_limit_warning_email(sender_email, org.name, org.cv_count, org.cv_limit)

    # ── 5. Store the input file in Supabase Storage ───────────────────────────
    job_id = str(uuid.uuid4())
    # Neutral storage name — never put the candidate's filename in storage or the DB.
    # Keep the extension so the worker can detect PDF vs Word for extraction.
    _ext = attachment.Name.rsplit(".", 1)[-1].lower() if "." in attachment.Name else "dat"
    input_path = f"{org.id}/{job_id}/input.{_ext}"

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
            "input_path": input_path,
            "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"Failed to queue job: {e}")
        raise HTTPException(status_code=500, detail="Queue error")

    logger.info(f"Job {job_id} queued for org {org.name} (sender: {sender_email})")
    return {"status": "queued", "job_id": job_id}
