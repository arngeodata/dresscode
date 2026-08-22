"""
Pilot account tracking — daily digest.

The 30-account pilot: each agency gets its own inbox, 25 CVs and 30 days.
This builds and sends the daily read on how that cohort is behaving, so George
knows who to call and who has gone quiet.

Deliberately mirrors trial_leads.py: same plain-text house style, same
send_plain_email(), same "sends nothing when there's nothing to say" rule.
All the arithmetic lives in the pilot_dashboard view, so this file is just
presentation.
"""

import logging
from datetime import datetime, timezone

from app.database import get_supabase
from app.config import get_settings

logger = logging.getLogger(__name__)

RULE = "─" * 40


def _time_ago(iso_ts: str | None) -> str:
    """Human 'time ago' from an ISO timestamp. Same behaviour as trial_leads."""
    if not iso_ts:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        mins = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
        if mins < 60:
            return f"{mins} min ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return iso_ts


def _calls_due(rows: list[dict]) -> list[tuple[dict, int]]:
    """
    The day-15 and day-30 calls that haven't been logged yet.

    Uses >= rather than == so a call missed on day 15 keeps appearing instead
    of falling silently off the list on day 16. Expired accounts are included —
    the day-30 call still needs making after the inbox stops working. Accounts
    with an outcome set are done, so they drop off.
    """
    due = []
    for r in rows:
        if r.get("outcome"):
            continue
        day = r.get("day_number") or 0
        if day >= 30 and not r.get("day30_call_at"):
            due.append((r, 30))
        elif day >= 15 and not r.get("day15_call_at"):
            due.append((r, 15))
    return due


def build_pilot_digest_text(rows: list[dict]) -> str:
    """
    Build the plain-text pilot digest body.

    Three sections, ordered by what they ask of you: calls you owe, accounts
    that aren't moving, accounts that are.
    """
    live = [r for r in rows if r.get("state") != "expired"]
    activated = [r for r in rows if (r.get("cvs_used") or 0) > 0]
    cvs_total = sum(r.get("cvs_used") or 0 for r in rows)
    cvs_yday = sum(r.get("cvs_yesterday") or 0 for r in rows)
    pct = round(100 * len(activated) / len(rows)) if rows else 0

    lines = [
        f"{len(activated)}/{len(rows)} pilot accounts activated ({pct}%)  ·  "
        f"{cvs_total} CVs all-time  ·  {cvs_yday} yesterday",
        "",
        RULE,
        "",
    ]

    due = _calls_due(rows)
    if due:
        lines += ["CALLS DUE", ""]
        for r, which in due:
            lines += [
                f"Day {which}  ·  {r.get('name')}  —  {r.get('owner_name') or '(no name)'}",
                f"    Phone:  {r.get('owner_phone') or '—'}",
                f"    Used:   {r.get('cvs_used') or 0}/{r.get('cv_limit')} CVs  ·  "
                f"day {r.get('day_number') or 0}",
                "",
            ]
        lines += [RULE, ""]

    # 'never used' outranks 'stalled': a dead account on day 4 is a different
    # failure from one that started and petered out.
    rank = {"never used": 0, "stalled": 1}
    problems = [r for r in live if r.get("state") in rank]
    problems.sort(key=lambda r: (rank[r["state"]], -(r.get("day_number") or 0)))
    if problems:
        lines += ["NOT MOVING", ""]
        for r in problems:
            lines += [
                f"{str(r.get('name'))[:28]:<28}  day {str(r.get('day_number') or 0):>2}"
                f"  ·  {r.get('cvs_used') or 0}/{r.get('cv_limit')}"
                f"  ·  {r.get('state')}"
                f"  ·  last {_time_ago(r.get('last_cv_at'))}",
            ]
        lines += ["", RULE, ""]

    working = [r for r in live if r.get("state") in ("active", "used up")]
    working.sort(key=lambda r: -(r.get("cvs_used") or 0))
    if working:
        lines += ["WORKING", ""]
        for r in working:
            flag = "  🔥" if (r.get("cvs_7d") or 0) >= 5 else ""
            lines += [
                f"{str(r.get('name'))[:28]:<28}  day {str(r.get('day_number') or 0):>2}"
                f"  ·  {r.get('cvs_used') or 0}/{r.get('cv_limit')}"
                f"  ·  {r.get('cvs_7d') or 0} this week{flag}",
            ]
        lines += ["", RULE, ""]

    expired = [r for r in rows if r.get("state") == "expired"]
    if expired:
        undecided = sum(1 for r in expired if not r.get("outcome"))
        note = f"{len(expired)} expired"
        if undecided:
            note += f", {undecided} with no outcome set"
        lines += [note, ""]

    lines += ["— Dresscode"]
    return "\n".join(lines)


def send_pilot_digest() -> bool:
    """
    Query the pilot dashboard, build the digest, and email George.
    Sends nothing when there are no pilot accounts (no inbox noise before the
    cohort exists). Returns True if an email was sent.
    """
    settings = get_settings()
    supabase = get_supabase()

    try:
        result = (
            supabase.table("pilot_dashboard")
            .select("*")
            .order("day_number", desc=True)
            .execute()
        )
        rows = result.data or []

        if not rows:
            logger.info("Pilot digest: no pilot accounts — skipping send.")
            return False

        activated = sum(1 for r in rows if (r.get("cvs_used") or 0) > 0)
        body = build_pilot_digest_text(rows)
        subject = (
            f"Dresscode pilot — {activated}/{len(rows)} activated "
            f"({datetime.now(timezone.utc):%-d %b})"
        )

        from app.services.emailer import send_plain_email
        return send_plain_email(settings.digest_to_email, subject, body)

    except Exception as e:  # noqa: BLE001 — the digest must never kill the worker loop
        logger.error("send_pilot_digest failed: %s", e)
        return False
