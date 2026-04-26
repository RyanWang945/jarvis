from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.api.agent import router as agent_router
from app.config import get_settings
from app.knowledge_base.api import router as knowledge_base_router

router = APIRouter()
router.include_router(agent_router)
router.include_router(knowledge_base_router)


@router.get("/health", tags=["system"])
def health_check() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "timestamp": datetime.now(UTC).isoformat(),
    }
