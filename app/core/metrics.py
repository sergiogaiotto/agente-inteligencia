"""Métricas Prometheus de 1ª classe (OBS-1) — RED + escalonamento.

Expostas em ``GET /metrics`` (formato de exposição Prometheus). Usa o registry
DEFAULT do ``prometheus_client``: o app roda em processo ÚNICO (uvicorn 1 worker),
então o registry global cobre todas as requests. Se um dia virar multi-worker,
migrar para o modo multiprocess (``PROMETHEUS_MULTIPROC_DIR``).

Instrumentação vive no recorder OFF-PATH do invoke
(``pipelines._record_invoke_analytics``): ``inc()``/``observe()`` são operações
in-memory e NÃO bloqueiam a resposta — preserva o invariante de desempenho.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# Buckets cobrindo de sub-100ms (rota rápida / cache de topologia) a 2min
# (invoke com cadeia longa de LLM). A cauda (p95/p99) é o que quebra SLA.
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, float("inf"))

INVOCATIONS = Counter(
    "maestro_invocations_total",
    "Invocações de pipeline, por tipo e status terminal",
    ["kind", "status"],
)
INVOCATION_LATENCY = Histogram(
    "maestro_invocation_duration_seconds",
    "Latência ponta-a-ponta da invocação (segundos)",
    ["kind"],
    buckets=_LATENCY_BUCKETS,
)
INVOCATION_ERRORS = Counter(
    "maestro_invocation_errors_total",
    "Invocações que terminaram em erro/falha",
    ["kind"],
)
ESCALATIONS = Counter(
    "maestro_escalations_total",
    "Invocações cujo estado terminal foi de escalonamento",
    ["kind"],
)


def record_invocation(
    *,
    kind: str,
    status: str,
    duration_s: float,
    escalated: bool = False,
    error: bool = False,
) -> None:
    """Registra uma invocação nas métricas RED. Não-bloqueante (in-memory)."""
    INVOCATIONS.labels(kind=kind, status=status).inc()
    if duration_s and duration_s > 0:
        INVOCATION_LATENCY.labels(kind=kind).observe(duration_s)
    if error:
        INVOCATION_ERRORS.labels(kind=kind).inc()
    if escalated:
        ESCALATIONS.labels(kind=kind).inc()


def render_latest() -> tuple[bytes, str]:
    """Payload de exposição Prometheus + content-type, para o handler /metrics."""
    return generate_latest(), CONTENT_TYPE_LATEST
