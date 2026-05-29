"""
Usage limit checking and CV count management.
All limit enforcement happens before a job is queued.
"""

import logging
from enum import Enum
from dataclasses import dataclass
from app.database import get_supabase
from app.models import Organisation

logger = logging.getLogger(__name__)


class LimitStatus(Enum):
    OK = "ok"                   # Under limit, proceed
    WARNING = "warning"         # 90%+ used — process but send warning
    EXCEEDED = "exceeded"       # At or over limit — reject
    UNLIMITED = "unlimited"     # Studio tier — always proceed


@dataclass
class LimitCheck:
    status: LimitStatus
    org: Organisation
    message: str = ""


def check_limits(org: Organisation) -> LimitCheck:
    """
    Check whether an organisation can process another CV.

    Returns a LimitCheck with the status and the organisation record.
    """
    # Studio tier — unlimited
    if org.cv_limit is None:
        return LimitCheck(status=LimitStatus.UNLIMITED, org=org)

    # Hard block
    if org.cv_count >= org.cv_limit:
        return LimitCheck(
            status=LimitStatus.EXCEEDED,
            org=org,
            message=f"Organisation {org.name} has reached limit ({org.cv_count}/{org.cv_limit})",
        )

    # 90% warning threshold
    if org.cv_count >= org.cv_limit * 0.9:
        return LimitCheck(
            status=LimitStatus.WARNING,
            org=org,
            message=f"Organisation {org.name} at {org.cv_count}/{org.cv_limit} CVs (warning threshold)",
        )

    return LimitCheck(status=LimitStatus.OK, org=org)


def increment_cv_count(org_id: str) -> int:
    """
    Increment the CV count for an organisation after a successful job.

    Returns the new cv_count value.
    """
    supabase = get_supabase()

    # Atomic increment using RPC
    result = (
        supabase.table("organisations")
        .update({"cv_count": supabase.raw("cv_count + 1")})
        .eq("id", org_id)
        .execute()
    )

    if result.data:
        return result.data[0].get("cv_count", 0)
    return 0


def get_organisation_by_username(username: str) -> Organisation | None:
    """
    Look up an organisation by their email username (e.g. 'acme' from 'acme@dresscode.com').

    Returns None if not found or inactive.
    """
    supabase = get_supabase()

    result = (
        supabase.table("organisations")
        .select("*")
        .eq("email_username", username.lower().strip())
        .eq("active", True)
        .single()
        .execute()
    )

    if not result.data:
        return None

    return Organisation(**result.data)


def is_sender_allowed(org: Organisation, sender_domain: str) -> bool:
    """
    Check if the sender's email domain is whitelisted for this organisation.
    An empty allowed_domains list blocks all senders (misconfiguration — log it).
    """
    if not org.allowed_domains:
        logger.warning(f"Organisation {org.name} has no allowed_domains configured")
        return False

    return sender_domain.lower() in [d.lower() for d in org.allowed_domains]
