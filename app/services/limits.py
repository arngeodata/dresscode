"""
Usage limit checking and CV count management.
All limit enforcement happens before a job is queued.
"""

import logging
import math
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass
from app.database import get_supabase
from app.models import Organisation
from app.services.billing import report_cv_usage

logger = logging.getLogger(__name__)


class LimitStatus(Enum):
    OK = "ok"                       # Comfortably within the included allowance
    APPROACHING_CAP = "approaching" # 90%+ of allowance used — process + notify
    OVER_CAP = "over_cap"           # Allowance used up — process, bill overage per CV
    EXPIRED = "expired"             # Pilot/trial window has closed — REJECT, do not process


@dataclass
class LimitCheck:
    status: LimitStatus
    org: Organisation
    message: str = ""


def check_limits(org: Organisation) -> LimitCheck:
    """
    Classify where an organisation sits against its included CV allowance.

    NOTE: Pricing model is "flat fee + overage" (see session-log.md). We no
    longer HARD-STOP at the cap — every CV is processed and usage above the cap
    is billed per-CV via Stripe metering (see billing.py + increment_cv_count).
    This function now only decides what *message* to surface, not whether to
    proceed.

    To reintroduce a hard stop (e.g. to bound overage on small Starter
    accounts), add a new status here and have the caller reject on it.

    The one exception is EXPIRED: a pilot account past its trial_ends_at is
    rejected outright by the caller. That is the only hard stop in the system.
    """
    # Pilot/trial window closed — the one status the caller must reject on.
    # Checked before the allowance because an expired account has no allowance
    # left to reason about, however many CVs are unused.
    if org.trial_ends_at:
        try:
            ends = datetime.fromisoformat(str(org.trial_ends_at).replace("Z", "+00:00"))
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > ends:
                return LimitCheck(
                    status=LimitStatus.EXPIRED,
                    org=org,
                    message=(
                        f"{org.name} trial ended {ends:%-d %b %Y} "
                        f"({org.cv_count}/{org.cv_limit} used) — rejecting"
                    ),
                )
        except (TypeError, ValueError) as e:
            # A malformed date is our bug, not the sender's — never block a CV on it.
            logger.error("Unparseable trial_ends_at for %s: %r (%s)", org.name, org.trial_ends_at, e)

    # No cap set → treat as within allowance (usage is still metered downstream).
    if org.cv_limit is None:
        return LimitCheck(status=LimitStatus.OK, org=org)

    # Allowance used up — overage territory (still processed).
    if org.cv_count >= org.cv_limit:
        return LimitCheck(
            status=LimitStatus.OVER_CAP,
            org=org,
            message=(
                f"{org.name} over included allowance "
                f"({org.cv_count}/{org.cv_limit}) — billing overage per CV"
            ),
        )

    # Approaching the allowance (90%+).
    if org.cv_count >= org.cv_limit * 0.9:
        return LimitCheck(
            status=LimitStatus.APPROACHING_CAP,
            org=org,
            message=f"{org.name} at {org.cv_count}/{org.cv_limit} CVs (approaching allowance)",
        )

    return LimitCheck(status=LimitStatus.OK, org=org)


# Per-CV overage rates above the included allowance (GBP). Keep in sync with Stripe.
OVERAGE_RATES = {"starter": 2.50, "growth": 1.50, "studio": 0.50}


def build_usage_note(tier: str, count: int, cv_limit: int | None) -> str:
    """
    One-line usage summary for the returned-CV email.

    Within allowance:  "This is CV 47/50 included in your package this month."
    Over allowance:     "This is CV 52 — 2 over your 50 included this month,
                         billed at £2.50 each (pay-as-you-go)."
    """
    if cv_limit is None:
        return f"This is CV {count} this month."

    if count <= cv_limit:
        return f"This is CV {count}/{cv_limit} included in your package this month."

    over = count - cv_limit
    rate = OVERAGE_RATES.get((tier or "").lower())
    rate_txt = f", billed at £{rate:.2f} each (pay-as-you-go)" if rate else ""
    return (
        f"This is CV {count} — {over} over your {cv_limit} included this month{rate_txt}."
    )


def days_left_in_trial(trial_ends_at: str | None) -> int | None:
    """
    Whole days remaining in a pilot window, rounded up so the final day reads
    "1 day left" rather than "0". None if there's no end date or it won't parse.
    """
    if not trial_ends_at:
        return None
    try:
        ends = datetime.fromisoformat(str(trial_ends_at).replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    secs = (ends - datetime.now(timezone.utc)).total_seconds()
    return max(0, math.ceil(secs / 86400))


def build_pilot_usage_note(count: int, cv_limit: int | None, trial_ends_at: str | None) -> str:
    """
    Two-line usage block for pilot accounts:

        CV 3 of 25
        18 days left in your trial

    Deliberately not the paying-customer note — a pilot has no package and no
    overage, so "included in your package this month" would be wrong on both
    counts. The day count is the line that actually moves people.
    """
    lines = [f"CV {count} of {cv_limit}" if cv_limit else f"CV {count}"]
    days = days_left_in_trial(trial_ends_at)
    if days is not None:
        lines.append(
            "Last day of your trial" if days == 0
            else f"{days} day{'s' if days != 1 else ''} left in your trial"
        )
    return "\n".join(lines)


def increment_cv_count(org_id: str) -> int:
    """
    Increment the CV count for an organisation after a successful job, and
    report the CV to Stripe metering for overage billing.

    The internal cv_count drives in-product messaging (approaching/over cap);
    the Stripe meter event is what actually bills overage. Both are updated
    here so they stay in lock-step — one CV delivered = one of each.

    Returns the new cv_count value.
    """
    supabase = get_supabase()

    # Read current count + the Stripe customer id in one go.
    # (Worker processes one job at a time so this is safe without atomicity.)
    org_result = (
        supabase.table("organisations")
        .select("cv_count, name, stripe_customer_id")
        .eq("id", org_id)
        .single()
        .execute()
    )
    org_data = org_result.data or {}
    current = org_data.get("cv_count", 0) or 0
    new_count = current + 1

    result = (
        supabase.table("organisations")
        .update({"cv_count": new_count})
        .eq("id", org_id)
        .execute()
    )

    # Report usage to Stripe (best-effort; never blocks delivery on a billing error).
    report_cv_usage(
        stripe_customer_id=org_data.get("stripe_customer_id"),
        org_name=org_data.get("name", org_id),
    )

    if result.data:
        return result.data[0].get("cv_count", new_count)
    return new_count


def get_organisation_by_domain(sender_domain: str) -> Organisation | None:
    """
    Look up an organisation by the sender's email domain.
    Finding the org by domain IS the authorisation check — if their domain
    isn't in allowed_domains for any active org, they're not a customer.

    e.g. 'hyperion-partners.co.uk' from 'george@hyperion-partners.co.uk'

    Returns None if not found or inactive.
    """
    supabase = get_supabase()

    result = (
        supabase.table("organisations")
        .select("*")
        .contains("allowed_domains", [sender_domain.lower().strip()])
        .eq("active", True)
        .maybe_single()
        .execute()
    )

    if not result.data:
        return None

    return Organisation(**result.data)


def get_organisation_by_email_username(username: str) -> Organisation | None:
    """
    Look up an organisation by its email_username (the local-part before @).

    Used for the public trial inbox (trial@cvdresscode.com → the 'trial' org),
    which is NOT authed by sender domain. For normal customer traffic, auth still
    happens via get_organisation_by_domain().

    Returns None if not found or inactive.
    """
    supabase = get_supabase()

    result = (
        supabase.table("organisations")
        .select("*")
        .eq("email_username", username.lower().strip())
        .eq("active", True)
        .maybe_single()
        .execute()
    )

    if not result.data:
        return None

    return Organisation(**result.data)
