"""
Stripe usage metering.

Reports CV-formatting events to Stripe Billing Meters so per-CV overage can be
billed on top of the flat subscription fee.

Design:
  - Report EVERY successful CV (value = 1). The metered Price on each
    subscription uses *graduated* tiers (first [cap] units at £0, units above at
    the overage rate), so Stripe applies the free allowance automatically — this
    code just reports raw totals.
  - Metering is BEST-EFFORT. A Stripe failure must never break CV delivery or
    raise into the worker; missed events can be reconciled. Product first.

Stripe setup required (dashboard, one-time — see session-log.md):
  1. Create a Billing Meter with event name = CV_METER_EVENT_NAME, aggregation
     = sum over the `value` payload key, customer mapping on `stripe_customer_id`.
  2. Create a graduated metered Price per tier referencing that meter and attach
     it (alongside the flat licensed price) to each subscription.
"""

import logging

import stripe

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

# Must match the "event name" of the Meter configured in the Stripe dashboard.
CV_METER_EVENT_NAME = "cv_formatted"


def report_cv_usage(stripe_customer_id: str | None, org_name: str = "") -> bool:
    """
    Report a single CV usage event (value = 1) to Stripe Billing Meters.

    The subscription's graduated metered price decides how much (if any) of this
    usage is billable — we always report the raw event.

    Best-effort: returns True on success, False on skip/failure. Never raises.
    """
    if not stripe_customer_id:
        # Free / test orgs (e.g. Hyperion, Abingdon) have no Stripe customer yet.
        logger.info(
            "No stripe_customer_id for %s — skipping meter event (free/test org).",
            org_name or "org",
        )
        return False

    try:
        stripe.billing.MeterEvent.create(
            event_name=CV_METER_EVENT_NAME,
            payload={
                "stripe_customer_id": stripe_customer_id,
                "value": "1",
            },
        )
        logger.info("Reported CV meter event for %s (%s).", org_name, stripe_customer_id)
        return True
    except Exception as exc:  # noqa: BLE001 — billing must never break delivery
        logger.error(
            "Stripe meter event FAILED for %s (%s): %s. CV still delivered; reconcile later.",
            org_name,
            stripe_customer_id,
            exc,
        )
        return False
