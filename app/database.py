from supabase import create_client, Client
from functools import lru_cache
from app.config import get_settings


@lru_cache()
def get_supabase() -> Client:
    """
    Returns a cached Supabase client using the service role key.
    Service role bypasses RLS — only use on the backend, never expose to clients.
    """
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key)
