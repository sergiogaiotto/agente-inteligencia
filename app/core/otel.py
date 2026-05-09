"""OpenTelemetry bootstrap (Onda 2 — Observabilidade self-hosted).

Init idempotente. Quando `settings.otel_enabled is False`, todas as funções
viram no-op e o app roda exatamente como antes — não há overhead nem risco.

Quando ativo:
- Cria TracerProvider com Resource (service.name, service.version, env)
- Configura BatchSpanProcessor + OTLPSpanExporter (gRPC → Tempo)
- Instrumenta automaticamente: FastAPI, asyncpg, httpx, redis, logging
- LoggingInstrumentor injeta `trace_id` e `span_id` em cada log record,
  permitindo correlação Tempo ↔ Loki no Grafana.

Robustez:
- Falha de DNS/conexão com Tempo NÃO derruba o app — exporter tem reconnect
  automático e BatchSpanProcessor descarta spans em buffer overflow.
- `get_tracer()` sempre retorna um tracer válido (NoOpTracer quando OTel desligado),
  então spans manuais em código de domínio são seguros independentemente da flag.

Reverter Onda 2:
- Setar `OTEL_ENABLED=false` no .env, ou
- Remover este módulo + os 2 imports em main.py + os blocos `with _tracer.start_*` nos
  3 arquivos de domínio.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Flag de idempotência. init_otel() pode ser chamado múltiplas vezes
# (lifespan reload, testes) sem registrar provider duplicado.
_initialized = False


def init_otel(app: "FastAPI") -> None:
    """Inicializa OpenTelemetry. No-op se OTEL_ENABLED=false ou já inicializado.

    Chamado uma vez no startup do FastAPI (em main.py, logo após criação do app).
    """
    global _initialized
    if _initialized:
        return

    settings = get_settings()
    if not settings.otel_enabled:
        logger.info("OTel desligado (OTEL_ENABLED=false) — pulando bootstrap.")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
    except ImportError as e:
        logger.warning(
            f"OTEL_ENABLED=true mas dependências faltando ({e}). "
            "Rode `pip install -r requirements.txt`. Continuando sem OTel."
        )
        return

    try:
        resource = Resource.create({
            "service.name": settings.otel_service_name,
            "service.version": settings.otel_service_version,
            "deployment.environment": settings.app_env,
        })

        provider = TracerProvider(resource=resource)
        # OTLPSpanExporter: insecure=True porque dentro da rede docker `aimesh`,
        # sem TLS (Onda 4 traz mTLS). Endpoint inclui scheme http://.
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=True,
        )
        # BatchSpanProcessor: assíncrono, com buffer; falhas de export viram log
        # em vez de propagar para o caller — é exatamente o comportamento que queremos.
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Instrumentações automáticas — cobrem 95% dos spans sem código manual.
        FastAPIInstrumentor.instrument_app(app)
        AsyncPGInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        RedisInstrumentor().instrument()
        # set_logging_format=True anexa "%(otelTraceID)s %(otelSpanID)s" ao logger root,
        # então cada linha de log carrega o trace_id e Promtail captura no Loki.
        LoggingInstrumentor().instrument(set_logging_format=True)

        _initialized = True
        logger.info(
            f"OTel inicializado: service={settings.otel_service_name} "
            f"endpoint={settings.otel_exporter_otlp_endpoint} "
            f"sampler={settings.otel_traces_sampler}"
        )
    except Exception as e:
        # Qualquer falha na inicialização não pode derrubar o app.
        # Log warning e seguir sem OTel — equivale a OTEL_ENABLED=false.
        logger.warning(f"Falha ao inicializar OTel: {type(e).__name__}: {e}. App continua sem tracing.")


def get_tracer(name: str):
    """Retorna um Tracer para uso em código de domínio (spans manuais).

    Sempre retorna algo válido:
    - Se OTel inicializado: tracer real, spans vão para Tempo.
    - Se OTel desligado: ProxyTracer da API que vira NoOpTracer (zero overhead).

    Uso típico:
        from app.core.otel import get_tracer
        _tracer = get_tracer(__name__)

        async def do_thing():
            with _tracer.start_as_current_span("thing.do") as span:
                span.set_attribute("thing.id", "...")
                # ... lógica
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        # OTel não instalado mesmo. Retorna um shim mínimo para que `with _tracer.start_as_current_span(...)`
        # funcione sem quebrar. Improvável em prod (deps no requirements.txt) mas defensivo.
        return _NoOpTracerShim()


class _NoOpTracerShim:
    """Fallback se opentelemetry-api não estiver instalado. Compatível com a API mínima usada."""

    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpanContext()


class _NoOpSpanContext:
    def __enter__(self):
        return _NoOpSpan()

    def __exit__(self, *args):
        return False


class _NoOpSpan:
    def set_attribute(self, *args, **kwargs):
        pass

    def add_event(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass
