import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

from app.api.routes import router
from app.channels.feishu import build_feishu_channel
from app.config import get_settings
from app.logging_config import configure_logging
from app.observability import configure_observability, instrument_fastapi_app
from app.scheduler.runtime import SchedulerWorker
from app.skills.bootstrap import bootstrap_registries

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registries = bootstrap_registries(force=True)
    app.state.skill_registry = registries.skill_registry

    # Start Feishu persistent WebSocket channel
    feishu_channel = build_feishu_channel()
    if feishu_channel is not None:
        feishu_channel.start()
        app.state.feishu_channel = feishu_channel
        scheduler_worker = SchedulerWorker(delivery=feishu_channel)
        scheduler_worker.start()
        app.state.scheduler_worker = scheduler_worker
    else:
        scheduler_worker = None
    try:
        yield
    finally:
        if scheduler_worker is not None:
            scheduler_worker.stop()
        if feishu_channel is not None:
            feishu_channel.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_observability(settings)
    configure_logging(settings)
    logger.info(
        "observability settings otel_enabled=%s service_name=%s endpoint=%s protocol=%s sampler=%s sampler_arg=%s",
        settings.otel_enabled,
        settings.otel_service_name,
        settings.otel_exporter_otlp_endpoint,
        settings.otel_exporter_otlp_protocol,
        settings.otel_traces_sampler,
        settings.otel_traces_sampler_arg,
    )

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(router)
    instrument_fastapi_app(app, settings)

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
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
