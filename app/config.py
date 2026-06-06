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
    dresscode_from_email: str = "george@hyperion-partners.co.uk"
    dresscode_support_email: str = "george@hyperion-partners.co.uk"  # support@cvdresscode.com mailbox doesn't exist yet
    trial_username: str = "trial"  # local-part of the public lead-magnet inbox: trial@cvdresscode.com

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
