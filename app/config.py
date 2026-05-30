from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # -- Supabase ---------------------------------------------------------------
    supabase_url: str = "https://byhypleahdvigczhgozx.supabase.co"
    supabase_service_key: str = "REPLACE_WITH_SERVICE_ROLE_KEY"

    # -- Anthropic --------------------------------------------------------------
    anthropic_api_key: str = "REPLACE_WITH_ANTHROPIC_KEY"
    claude_model: str = "claude-haiku-4-5-20251001"
    claude_max_tokens: int = 64000

    # -- Postmark ---------------------------------------------------------------
    postmark_server_token: str = "REPLACE_WITH_POSTMARK_SERVER_TOKEN"
    postmark_inbound_webhook_token: str = "REPLACE_WITH_WEBHOOK_SECRET"
    dresscode_domain: str = "dresscode.com"
    dresscode_support_email: str = "support@dresscode.com"

    # -- Stripe -----------------------------------------------------------------
    stripe_secret_key: str = "REPLACE_WITH_STRIPE_SECRET_KEY"
    stripe_webhook_secret: str = "REPLACE_WITH_STRIPE_WEBHOOK_SECRET"

    # -- App --------------------------------------------------------------------
    app_env: str = "development"
    worker_poll_interval: int = 15
    max_attachment_size_mb: int = 10

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
