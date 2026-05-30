import logging
from fastapi import APIRouter
from app.database import get_supabase

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    """Basic health check. Verifies DB connectivity."""
    try:
        get_supabase().table("async_jobs").select("id").limit(1).execute()
        db_status = "ok"
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        db_status = "error"

    return {"status": "ok", "db": db_status}
