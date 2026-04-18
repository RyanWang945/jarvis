import logging

import uvicorn
from fastapi import FastAPI, Request

from app.api.routes import router
from app.config import get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(title=settings.app_name)
    app.include_router(router)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        logger.info("request started method=%s path=%s", request.method, request.url.path)
        response = await call_next(request)
        logger.info(
            "request completed method=%s path=%s status_code=%s",
            request.method,
            request.url.path,
            response.status_code,
        )
        return response

    return app


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        host=settings.host,
        port=settings.port,
        factory=True,
        reload=settings.environment == "development",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
