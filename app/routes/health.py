from fastapi import APIRouter
from app.database import get_supabase

router = APIRouter()


@router.get("/health")
async def health_check():
    """Basic health check. Verifies DB connectivity."""
    try:
        get_supabase().table("organisations").select("id").limit(1).execute()
        db_status = "ok"
    except Exception:
        db_status = "error"

    return {"status": "ok", "db": db_status}
