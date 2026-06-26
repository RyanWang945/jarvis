from __future__ import annotations

import logging
import os
from threading import Lock
from typing import Any

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from app.config import Settings

logger = logging.getLogger(__name__)

_SETUP_LOCK = Lock()
_CONFIGURED = False
_FASTAPI_INSTRUMENTED = False
_SQLALCHEMY_ENGINES: set[int] = set()


def configure_observability(settings: Settings) -> None:
    global _CONFIGURED
    if not settings.otel_enabled:
        return
    with _SETUP_LOCK:
        if _CONFIGURED:
            return
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": "0.1.0",
                "deployment.environment": settings.environment,
            }
        )
        provider = TracerProvider(
            resource=resource,
            sampler=_build_sampler(settings),
        )
        exporter = OTLPSpanExporter(
            endpoint=_otlp_traces_endpoint(settings),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _CONFIGURED = True
        logger.info(
            "observability configured service_name=%s endpoint=%s protocol=%s",
            settings.otel_service_name,
            settings.otel_exporter_otlp_endpoint,
            settings.otel_exporter_otlp_protocol,
        )


def instrument_fastapi_app(app: FastAPI, settings: Settings) -> None:
    global _FASTAPI_INSTRUMENTED
    if not settings.otel_enabled:
        return
    with _SETUP_LOCK:
        if _FASTAPI_INSTRUMENTED:
            return
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        _FASTAPI_INSTRUMENTED = True


def instrument_sqlalchemy_engine(engine: Engine, settings: Settings) -> None:
    if not settings.otel_enabled:
        return
    engine_key = id(engine)
    with _SETUP_LOCK:
        if engine_key in _SQLALCHEMY_ENGINES:
            return
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
        _SQLALCHEMY_ENGINES.add(engine_key)


def _build_sampler(settings: Settings) -> Any:
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    sampler_name = str(settings.otel_traces_sampler or "").strip().lower()
    ratio = max(0.0, min(float(settings.otel_traces_sampler_arg), 1.0))
    if sampler_name in {"parentbased_traceidratio", "parentbased"}:
        return ParentBased(TraceIdRatioBased(ratio))
    if sampler_name in {"traceidratio", "traceidratiobased"}:
        return TraceIdRatioBased(ratio)
    return ParentBased(TraceIdRatioBased(ratio))


def _otlp_traces_endpoint(settings: Settings) -> str:
    base = str(settings.otel_exporter_otlp_endpoint or "").rstrip("/")
    protocol = str(settings.otel_exporter_otlp_protocol or "").strip().lower()
    if protocol == "http/protobuf" and not base.endswith("/v1/traces"):
        return f"{base}/v1/traces"
    return base
