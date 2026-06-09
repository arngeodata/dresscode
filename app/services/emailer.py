"""
Outbound email via Postmark.
Handles all emails Dresscode sends: formatted CVs, errors, limit warnings.
"""

import base64
import logging
from app.config import get_settings

logger = logging.getLogger(__name__)


def _postmark_client():
    from postmarker.core import PostmarkClient
    settings = get_settings()
    return PostmarkClient(server_token=settings.postmark_server_token)


def send_formatted_cv(
    to_email: str,
    candidate_name: str,
    docx_bytes: bytes,
    original_filename: str,
    usage_note: str = "",
) -> bool:
    """
    Email the formatted CV back to the consultant.

    Args:
        to_email: Consultant's email address
        candidate_name: Used in subject line
        docx_bytes: The formatted DOCX file as bytes
        original_filename: Original filename, used to name the attachment
        usage_note: Optional one-line usage summary (e.g. "This is CV 47/50
            included in your package this month.") shown above the signature.

    Returns:
        True if sent successfully
    """
    settings = get_settings()

    # Use the filename worker.py computed from the org's style_guide filename_format
    attachment_name = original_filename

    body_text = (
        f"Hi,\n\n"
        f"Your formatted CV for {candidate_name or 'the candidate'} is attached.\n\n"
        + (f"{usage_note}\n\n" if usage_note else "")
        + f"— Dresscode\n"
        f"{settings.dresscode_support_email}"
    )

    try:
        import requests as _requests
        content_b64 = base64.b64encode(docx_bytes).decode("utf-8")
        payload = {
            "From": f"Dresscode <{settings.dresscode_from_email}>",
            "To": to_email,
            "Subject": f"Your formatted CV is ready — {candidate_name or 'Candidate'}",
            "TextBody": body_text,
            "Attachments": [
                {
                    "Name": attachment_name,
                    "Content": content_b64,
                    "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                }
            ],
        }
        logger.info(
            f"Sending CV email to {to_email} | from={settings.dresscode_from_email} "
            f"| ({len(docx_bytes):,} bytes)"
        )
        resp = _requests.post(
            "https://api.postmarkapp.com/email",
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": settings.postmark_server_token,
            },
            timeout=30,
        )
        logger.info(f"Postmark response: HTTP {resp.status_code} — {resp.text[:300]}")
        resp.raise_for_status()
        logger.info(f"Formatted CV sent to {to_email}")
        return True

    except Exception as e:
        logger.error(f"Failed to send formatted CV to {to_email}: {e}")
        return False


def send_error_email(to_email: str, reason: str = "processing") -> bool:
    """
    Send a failure notification to the consultant.

    Args:
        to_email: Consultant's email
        reason: Short reason string for logging (not shown to user verbatim)
    """
    settings = get_settings()

    body_text = (
        "Hi,\n\n"
        "We weren't able to process the CV you sent. This can happen if:\n"
        "  • The file is password-protected\n"
        "  • The PDF is a scanned image with no extractable text\n"
        "  • The file is corrupted\n\n"
        "Please try again with a different file, or contact us at "
        f"{settings.dresscode_support_email} if the problem persists.\n\n"
        "— Dresscode"
    )

    try:
        client = _postmark_client()
        client.emails.send(
            From=f"Dresscode <{settings.dresscode_from_email}>",
            To=to_email,
            Subject="We couldn't process this CV",
            TextBody=body_text,
        )
        logger.info(f"Error email sent to {to_email} (reason: {reason})")
        return True
    except Exception as e:
        logger.error(f"Failed to send error email to {to_email}: {e}")
        return False


def send_plain_email(to_email: str, subject: str, body_text: str) -> bool:
    """Send a simple internal text email (no attachment). Used for the daily trial digest."""
    settings = get_settings()
    try:
        client = _postmark_client()
        client.emails.send(
            From=f"Dresscode <{settings.dresscode_from_email}>",
            To=to_email,
            Subject=subject,
            TextBody=body_text,
        )
        logger.info(f"Plain email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send plain email to {to_email}: {e}")
        return False


def send_limit_warning_email(to_email: str, org_name: str, cv_count: int, cv_limit: int) -> bool:
    """Send a 90% usage warning to the consultant."""
    settings = get_settings()
    remaining = cv_limit - cv_count

    body_text = (
        f"Hi {org_name},\n\n"
        f"You've used {cv_count} of your {cv_limit} monthly CVs — "
        f"just {remaining} remaining.\n\n"
        f"To avoid interruption, upgrade your plan at dresscode.com/billing.\n\n"
        "— Dresscode"
    )

    try:
        client = _postmark_client()
        client.emails.send(
            From=f"Dresscode <{settings.dresscode_from_email}>",
            To=to_email,
            Subject=f"You're nearly at your CV limit this month — {remaining} remaining",
            TextBody=body_text,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send limit warning to {to_email}: {e}")
        return False


def send_limit_reached_email(to_email: str, org_name: str, cv_limit: int) -> bool:
    """Send a hard-block notification when the monthly limit is hit."""
    settings = get_settings()

    body_text = (
        f"Hi {org_name},\n\n"
        f"You've reached your {cv_limit} CV limit for this month. "
        f"CVs sent to Dresscode will not be processed until your limit resets or you upgrade.\n\n"
        f"Upgrade your plan at dresscode.com/billing — takes 60 seconds.\n\n"
        "— Dresscode"
    )

    try:
        client = _postmark_client()
        client.emails.send(
            From=f"Dresscode <{settings.dresscode_from_email}>",
            To=to_email,
            Subject="You've reached your monthly CV limit",
            TextBody=body_text,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send limit-reached email to {to_email}: {e}")
        return False


def send_not_authorised_email(to_email: str, dresscode_address: str) -> bool:
    """Notify a sender that their domain isn't whitelisted for this agency address."""
    settings = get_settings()

    body_text = (
        f"Hi,\n\n"
        f"Your email address isn't authorised to send CVs to {dresscode_address}.\n\n"
        f"If you think this is a mistake, ask your agency admin to add your email domain "
        f"in the Dresscode settings, or contact {settings.dresscode_support_email}.\n\n"
        "— Dresscode"
    )

    try:
        client = _postmark_client()
        client.emails.send(
            From=f"Dresscode <{settings.dresscode_from_email}>",
            To=to_email,
            Subject="Your email address isn't authorised for this Dresscode address",
            TextBody=body_text,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send not-authorised email to {to_email}: {e}")
        return False


def send_no_attachment_email(to_email: str) -> bool:
    """Notify sender that no valid CV attachment was found."""
    settings = get_settings()

    body_text = (
        "Hi,\n\n"
        "We received your email but couldn't find a CV attachment (PDF or Word document).\n\n"
        "Please re-send with the CV file attached.\n\n"
        "— Dresscode"
    )

    try:
        client = _postmark_client()
        client.emails.send(
            From=f"Dresscode <{settings.dresscode_from_email}>",
            To=to_email,
            Subject="No CV attachment found",
            TextBody=body_text,
        )
        return True
    except Exception as e:
        logger.error(f"Failed to send no-attachment email to {to_email}: {e}")
        return False
