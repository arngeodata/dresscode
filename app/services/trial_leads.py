"""
Trial lead capture + daily digest.

Captures everyone who emails trial@cvdresscode.com into the trial_leads table
(lead-gen for outreach), and builds/sends a daily digest to George so he can
follow up while intent is high.

The candidate's CV is still deleted after delivery (GDPR) — we only retain the
SENDER's business-contact details (name/email/domain/phone), which is the lead.
"""

import logging
import re
from datetime import datetime, timezone, timedelta

from app.database import get_supabase
from app.config import get_settings

logger = logging.getLogger(__name__)

try:
    import phonenumbers
    _HAS_PHONENUMBERS = True
except ImportError:  # graceful degrade if dependency not yet installed
    _HAS_PHONENUMBERS = False

# Regions whose NATIONAL-format numbers we want to recognise (e.g. "07911 123456",
# "(555) 123-4567"). Any +<country-code> number is matched regardless of region.
# Order = preference when a bare national number is ambiguous — UK-first market.
_PHONE_REGIONS = ["GB", "IE", "AU", "NZ", "US", "CA"]

# Fallback loose regex (only used if phonenumbers isn't installed).
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,}\d")


def extract_phone(text: str | None) -> str | None:
    """
    Pull the first VALID phone number from an email body/signature, recognising
    UK, Ireland, Australia, New Zealand, US and Canada formats (national or
    international). Returns it in international format, e.g. "+44 7911 123456".

    Uses Google's libphonenumber (phonenumbers) for accurate detection + validation.
    Falls back to a loose regex only if the library isn't available.
    """
    if not text:
        return None

    if _HAS_PHONENUMBERS:
        for region in _PHONE_REGIONS:
            try:
                for match in phonenumbers.PhoneNumberMatcher(text, region):
                    if phonenumbers.is_valid_number(match.number):
                        return phonenumbers.format_number(
                            match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL
                        )
            except Exception:
                continue
        return None

    # Fallback: loose regex, length-guarded to avoid dates/reference numbers.
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 15:
            return m.group(0).strip()
    return None


def record_trial_lead(
    email: str,
    name: str | None,
    domain: str | None,
    phone: str | None,
) -> None:
    """
    Upsert a trial lead by email. New email → insert (sent_count=1). Existing →
    bump sent_count + last_seen, and backfill name/phone/domain if missing.

    Best-effort: logs and swallows errors so lead capture never blocks a CV.
    """
    if not email:
        return
    email = email.lower().strip()
    supabase = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        existing = (
            supabase.table("trial_leads")
            .select("id, sent_count, name, phone, domain")
            .eq("email", email)
            .maybe_single()
            .execute()
        )
        row = existing.data if existing else None

        if row:
            update = {
                "sent_count": (row.get("sent_count", 0) or 0) + 1,
                "last_seen": now_iso,
            }
            # Backfill only when we didn't already have the value.
            if not row.get("name") and name:
                update["name"] = name
            if not row.get("phone") and phone:
                update["phone"] = phone
            if not row.get("domain") and domain:
                update["domain"] = domain
            supabase.table("trial_leads").update(update).eq("id", row["id"]).execute()
            logger.info("Trial lead updated: %s (sent_count=%s)", email, update["sent_count"])
        else:
            supabase.table("trial_leads").insert({
                "email": email,
                "name": name,
                "domain": domain,
                "phone": phone,
                "sent_count": 1,
                "first_seen": now_iso,
                "last_seen": now_iso,
            }).execute()
            logger.info("Trial lead created: %s", email)

    except Exception as e:  # noqa: BLE001 — capture must never break delivery
        logger.error("record_trial_lead failed for %s: %s", email, e)


# ── Daily digest ───────────────────────────────────────────────────────────────

def _time_ago(iso_ts: str) -> str:
    """Human 'time ago' from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        mins = int(delta.total_seconds() // 60)
        if mins < 60:
            return f"{mins} min ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return iso_ts


def build_digest_text(leads: list[dict], total_count: int, window_hours: int = 24) -> str:
    """Build the plain-text daily digest body. Newest first; returning leads flagged."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    def is_new(lead: dict) -> bool:
        try:
            return datetime.fromisoformat((lead.get("first_seen") or "").replace("Z", "+00:00")) >= cutoff
        except Exception:
            return True

    new_count = sum(1 for l in leads if is_new(l))
    returning_count = len(leads) - new_count

    lines = [
        f"{len(leads)} trial lead(s) in the last {window_hours}h "
        f"— {new_count} new, {returning_count} returning.",
        f"(All-time unique trial leads: {total_count})",
        "",
        "─" * 40,
        "",
    ]

    for lead in leads:
        hot = (lead.get("sent_count", 1) or 1) > 1
        flag = "🔥 " if hot else ""
        name = lead.get("name") or "(no name)"
        email = lead.get("email") or "(no email)"
        domain = lead.get("domain") or "—"
        phone = lead.get("phone") or "—"
        count = lead.get("sent_count", 1) or 1
        seen = _time_ago(lead.get("last_seen") or "")
        tag = "new" if is_new(lead) else "returning"

        lines += [
            f"{flag}{name}  —  {email}",
            f"    Agency:   {domain}",
            f"    Phone:    {phone}",
            f"    Trialled: {count} CV(s)  ·  {seen}  ·  {tag}",
            "",
        ]

    lines += ["─" * 40, "Follow up while intent is high.", "— Dresscode"]
    return "\n".join(lines)


def send_daily_digest(window_hours: int = 24) -> bool:
    """
    Query trial leads active in the window, build the digest, and email George.
    Sends nothing on an empty day (no inbox noise). Returns True if an email
    was sent.
    """
    settings = get_settings()
    supabase = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

    try:
        recent = (
            supabase.table("trial_leads")
            .select("name, email, domain, phone, sent_count, first_seen, last_seen")
            .gte("last_seen", cutoff)
            .order("last_seen", desc=True)
            .execute()
        )
        leads = recent.data or []

        if not leads:
            logger.info("Daily trial digest: no new leads — skipping send.")
            return False

        total = supabase.table("trial_leads").select("id", count="exact").execute()
        total_count = total.count if total.count is not None else len(leads)

        body = build_digest_text(leads, total_count, window_hours)
        subject = f"Dresscode trial leads — {len(leads)} new ({datetime.now(timezone.utc):%-d %b})"

        from app.services.emailer import send_plain_email
        return send_plain_email(settings.digest_to_email, subject, body)

    except Exception as e:  # noqa: BLE001
        logger.error("send_daily_digest failed: %s", e)
        return False
