from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str = "https://byhypleahdvigczhgozx.supabase.co"
    supabase_service_key: str = "REPLACE_WITH_SERVICE_ROLE_KEY"  # Settings > API in Supabase dashboard

    # ── Anthropic ─────────────────────────────────────────────────────────────
    anthropic_api_key: str = "REPLACE_WITH_ANTHROPIC_KEY"
    claude_model: str = "claude-haiku-4-5-20251001"
    claude_max_tokens: int = 64000

    # ── Postmark ──────────────────────────────────────────────────────────────
    postmark_server_token: str = "REPLACE_WITH_POSTMARK_SERVER_TOKEN"
    postmark_inbound_webhook_token: str = "REPLACE_WITH_WEBHOOK_SECRET"  # Optional — for verifying inbound webhooks
    dresscode_domain: str = "dresscode.com"
    dresscode_from_email: str = "noreply@cvdresscode.com"  # fallback sender only; real replies send from the address written to (trial@/hyperion@). Must be a Postmark-verified domain (cvdresscode.com)
    dresscode_support_email: str = "george@getcvdresscode.com"  # shown in reply footers. NB: cvdresscode.com can't host a mailbox (MX = Postmark catch-all), so support lives on getcvdresscode.com
    trial_username: str = "trial"  # local-part of the public lead-magnet inbox: trial@cvdresscode.com
    digest_to_email: str = "george@hyperion-partners.co.uk"  # daily trial-lead digest recipient
    digest_hour_utc: int = 7  # hour (UTC) to send the daily trial-lead digest (~7-8am UK)

    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_secret_key: str = "REPLACE_WITH_STRIPE_SECRET_KEY"
    stripe_webhook_secret: str = "REPLACE_WITH_STRIPE_WEBHOOK_SECRET"

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: str = "development"          # development | production
    worker_poll_interval: int = 15        # seconds between job polls
    max_attachment_size_mb: int = 10      # reject attachments larger than this

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
