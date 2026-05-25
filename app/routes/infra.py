"""Status da infraestrutura — checa todos os serviços do compose em paralelo.

Endpoint único `/api/v1/infra/status` retorna lista de services com:
- ok: bool (responde no health check)
- latency_ms: float
- error: str opcional (se ok=False)
- url: link pra UI nativa quando existe (Qdrant dashboard, Grafana, etc.)
- hint: dica contextual (ex: "rode com --profile full")

Frontend renderiza isso como cards.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/infra", tags=["infra"])


# Timeout curto: cada check tem que ser rápido — o usuário está olhando a página.
_TIMEOUT = 1.5


def _is_container_absent(exc: Exception) -> bool:
    """Heurística: container do serviço não existe na network.

    Em serviços opcionais (profile_full), isso é estado esperado, não erro.
    Cobre 2 cenários:
    1. DNS lookup falha (Linux puro, Errno -3/-2): "name not known", "getaddrinfo"
    2. Connect timeout (Docker Desktop): DNS embedded espera resposta que nunca chega
       — `ConnectTimeout`, `ConnectError`, `ReadTimeout` em hostname desconhecido.

    Falsos positivos teóricos: serviço existente mas unresponsive. Aceito o
    tradeoff — em VPS Linux a heurística de DNS é precisa; em Docker Desktop
    o pior caso é mostrar "não iniciado" pra um serviço que travou (UX OK).
    """
    msg = str(exc).lower()
    type_name = type(exc).__name__.lower()
    dns_indicators = (
        "name or service not known",
        "temporary failure in name resolution",
        "errno -3",
        "errno -2",
        "nodename nor servname provided",
        "getaddrinfo failed",
    )
    if any(ind in msg for ind in dns_indicators):
        return True
    # httpx em Docker Desktop com container ausente: ConnectTimeout
    if "connecttimeout" in type_name or "connecterror" in type_name:
        return True
    return False


async def _check_http(
    name: str,
    health_url: str,
    *,
    description: str,
    ui_url: Optional[str] = None,
    expect_status: tuple = (200,),
    profile_full: bool = False,
) -> dict:
    """Checa um serviço HTTP com httpx GET. Retorna dict pro frontend.

    Estado "not_started" (novo): serviço opcional cujo container nem existe
    na network. UI renderiza como info/cinza, não erro/vermelho.
    """
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(health_url)
            ok = r.status_code in expect_status
            return {
                "name": name,
                "ok": ok,
                "not_started": False,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "description": description,
                "ui_url": ui_url,
                "health_url": health_url,
                "status_code": r.status_code,
                "error": None if ok else f"HTTP {r.status_code}",
                "hint": None if ok else (
                    "Serviço opcional — rode `docker compose --profile full up -d`"
                    if profile_full else None
                ),
            }
    except Exception as e:
        # Serviço opcional + falha de conexão (DNS/timeout) = container não
        # existe. NÃO é erro: é estado normal pra quem não rodou --profile full.
        not_started = profile_full and _is_container_absent(e)
        return {
            "name": name,
            "ok": False,
            "not_started": not_started,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": description,
            "ui_url": ui_url,
            "health_url": health_url,
            "status_code": None,
            "error": None if not_started else f"{type(e).__name__}: {str(e)[:80]}",
            "hint": (
                "Serviço opcional não iniciado — rode `docker compose --profile full up -d` se quiser observabilidade"
                if not_started else (
                    "Serviço opcional — rode `docker compose --profile full up -d`"
                    if profile_full else None
                )
            ),
        }


async def _check_postgres() -> dict:
    """Postgres não tem HTTP — usa o pool asyncpg do app pra um SELECT 1."""
    from app.core.database import _get_pool
    t0 = time.perf_counter()
    try:
        pool = _get_pool()
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
        return {
            "name": "postgres",
            "ok": True,
            "not_started": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Banco principal (agentes, interações, evidências)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": None,
            "hint": None,
        }
    except Exception as e:
        return {
            "name": "postgres",
            "ok": False,
            "not_started": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Banco principal (agentes, interações, evidências)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "hint": None,
        }


async def _check_redis() -> dict:
    """Redis ping via redis.asyncio (mesmo client usado em ratelimit.py)."""
    t0 = time.perf_counter()
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        client = aioredis.from_url(url, socket_timeout=_TIMEOUT)
        try:
            pong = await client.ping()
        finally:
            await client.close()
        ok = bool(pong)
        return {
            "name": "redis",
            "ok": ok,
            "not_started": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Cache de contexto + rate-limit (Onda 1)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": None if ok else "PING não respondeu PONG",
            "hint": None,
        }
    except Exception as e:
        return {
            "name": "redis",
            "ok": False,
            "not_started": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Cache de contexto + rate-limit (Onda 1)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "hint": None,
        }


async def _check_duckdb() -> dict:
    """DuckDB é lib embarcada (não serviço de rede). Latência = import + connect
    em `:memory:`. Saudável se conseguir abrir conexão e executar SELECT 1.

    Não tem UI nativa. Diretório raiz das tabelas é `data/tabular/`.
    Description menciona Onda Tabular para alinhar com outros services.
    """
    from pathlib import Path
    t0 = time.perf_counter()
    description = "Engine SQL embarcado para tabelas tabulares (Onda Tabular)"
    try:
        import duckdb  # type: ignore
        con = duckdb.connect(":memory:")
        try:
            con.execute("SELECT 1").fetchone()
        finally:
            con.close()
        return {
            "name": "duckdb",
            "ok": True,
            "not_started": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": description,
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": None,
            "hint": None,
        }
    except ImportError:
        # Lib não instalada: trata como "não iniciado" (similar a profile_full
        # quando o container não está up) — feature está disponível mas inativa.
        return {
            "name": "duckdb",
            "ok": False,
            "not_started": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": description,
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": None,
            "hint": "Lib não instalada — rode `pip install duckdb>=1.0.0` (ou `pip install -r requirements.txt`)",
        }
    except Exception as e:
        return {
            "name": "duckdb",
            "ok": False,
            "not_started": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": description,
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "hint": None,
        }


async def _duckdb_details() -> dict:
    """Contadores operacionais do DuckDB: versão da lib, total de tabelas
    promovidas, linhas totais, espaço em disco dos arquivos .duckdb.

    NÃO faz queries em todas as tabelas (custo proibitivo) — confia nos
    contadores cacheados em `data_tables.row_count` e `size_bytes`, que
    são atualizados no momento da ingestão (promote_to_table).
    """
    from pathlib import Path
    out = {
        "ok": False,
        "version": None,
        "tables_ready": 0,
        "tables_error": 0,
        "rows_total": 0,
        "size_bytes_total": 0,
        "size_bytes_on_disk": 0,
        "files_on_disk": 0,
        "tabular_root": "data/tabular",
    }
    try:
        import duckdb  # type: ignore
        out["version"] = duckdb.__version__
    except ImportError:
        out["error"] = "duckdb não instalado"
        return out

    # Contadores do Postgres (data_tables.row_count + size_bytes já populados)
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'ready')::bigint AS ready,
                    COUNT(*) FILTER (WHERE status = 'error')::bigint AS errored,
                    COALESCE(SUM(row_count) FILTER (WHERE status = 'ready'), 0)::bigint AS rows_total,
                    COALESCE(SUM(size_bytes) FILTER (WHERE status = 'ready'), 0)::bigint AS bytes_total
                FROM data_tables
            """)
            r = rows[0]
            out["tables_ready"] = int(r["ready"])
            out["tables_error"] = int(r["errored"])
            out["rows_total"] = int(r["rows_total"])
            out["size_bytes_total"] = int(r["bytes_total"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return out

    # Sanidade física: caminha em data/tabular/ e mede o que está em disco.
    # Pode divergir do total cacheado se houver arquivos órfãos (tabela
    # deletada do DB mas .duckdb não removido) ou vice-versa.
    try:
        root = Path("data") / "tabular"
        if root.exists():
            files = list(root.rglob("*.duckdb"))
            out["files_on_disk"] = len(files)
            out["size_bytes_on_disk"] = sum(f.stat().st_size for f in files)
    except Exception:
        # disk walk best-effort — não falha o response
        pass

    out["ok"] = True
    return out


async def _qdrant_details() -> dict:
    """Lista coleções Qdrant com points_count + dimensão dos vetores."""
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{qdrant_url}/collections")
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}", "collections": []}
            cols = (r.json().get("result") or {}).get("collections") or []
            # Para cada coleção, busca detalhes em paralelo
            async def _one(name: str) -> dict:
                try:
                    rr = await client.get(f"{qdrant_url}/collections/{name}")
                    if rr.status_code != 200:
                        return {"name": name, "error": f"HTTP {rr.status_code}"}
                    res = (rr.json().get("result") or {})
                    vec = ((res.get("config") or {}).get("params") or {}).get("vectors") or {}
                    # Qdrant pode retornar `vectors` como dict simples OU como dict de named vectors.
                    # Para named vectors, pega o primeiro size disponível.
                    size = vec.get("size")
                    if size is None and isinstance(vec, dict):
                        for v in vec.values():
                            if isinstance(v, dict) and "size" in v:
                                size = v["size"]
                                break
                    return {
                        "name": name,
                        "points_count": res.get("points_count", 0),
                        "indexed_vectors_count": res.get("indexed_vectors_count", 0),
                        "segments_count": res.get("segments_count", 0),
                        "vector_size": size,
                        "status": res.get("status", "unknown"),
                    }
                except Exception as e:
                    return {"name": name, "error": f"{type(e).__name__}: {str(e)[:60]}"}

            details = await asyncio.gather(*[_one(c["name"]) for c in cols])
            return {"ok": True, "collections": details}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}", "collections": []}


async def _redis_details() -> dict:
    """Estatísticas do Redis via INFO."""
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        client = aioredis.from_url(url, socket_timeout=_TIMEOUT, decode_responses=True)
        try:
            info = await client.info()
            # Hit rate: hits / (hits + misses). Útil pra avaliar eficiência do cache.
            hits = int(info.get("keyspace_hits", 0))
            misses = int(info.get("keyspace_misses", 0))
            total = hits + misses
            hit_rate = round(hits / total * 100, 1) if total > 0 else None
            # Keys count: o INFO retorna db0 como string "keys=N,expires=N,avg_ttl=N"
            db0 = info.get("db0", {})
            if isinstance(db0, dict):
                keys = int(db0.get("keys", 0))
            else:
                # Fallback: parsing string
                keys = 0
                for part in str(db0).split(","):
                    if part.startswith("keys="):
                        keys = int(part.split("=")[1])
                        break
        finally:
            await client.close()
        return {
            "ok": True,
            "used_memory_human": info.get("used_memory_human", "?"),
            "connected_clients": info.get("connected_clients", 0),
            "total_commands_processed": info.get("total_commands_processed", 0),
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "hit_rate_pct": hit_rate,
            "keys_db0": keys,
            "redis_version": info.get("redis_version", "?"),
            "uptime_in_days": info.get("uptime_in_days", 0),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


async def _postgres_details() -> dict:
    """Contagens das tabelas principais (agentes, interações, evidências, etc.).

    Reusa o pool asyncpg do app — query única com UNION ALL pra performance.
    """
    from app.core.database import _get_pool
    try:
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch("""
                SELECT 'agents' AS table_name, COUNT(*)::bigint AS count FROM agents
                UNION ALL SELECT 'skills', COUNT(*)::bigint FROM skills
                UNION ALL SELECT 'interactions', COUNT(*)::bigint FROM interactions
                UNION ALL SELECT 'turns', COUNT(*)::bigint FROM turns
                UNION ALL SELECT 'knowledge_sources', COUNT(*)::bigint FROM knowledge_sources
                UNION ALL SELECT 'api_connectors', COUNT(*)::bigint FROM api_connectors
                UNION ALL SELECT 'audit_log', COUNT(*)::bigint FROM audit_log
            """)
            return {"ok": True, "counts": {r["table_name"]: r["count"] for r in rows}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


@router.get("/details")
async def infra_details():
    """Métricas detalhadas dos serviços de dados — Qdrant collections,
    Redis INFO e contagens das tabelas Postgres principais.

    Diferente de /status (binário ok/error), /details traz contadores e
    configuração que mudam ao longo do uso.
    """
    qdrant, redis, pg, duck = await asyncio.gather(
        _qdrant_details(),
        _redis_details(),
        _postgres_details(),
        _duckdb_details(),
    )
    return {"qdrant": qdrant, "redis": redis, "postgres": pg, "duckdb": duck}


@router.get("/status")
async def infra_status():
    """Status agregado de todos os serviços do compose.

    Checa em paralelo (asyncio.gather) — total ~1.5s no pior caso (timeout).
    """
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    opa_url = os.environ.get("OPA_URL", "http://opa:8181")

    # UIs nativas — link clicado pelo browser do USUÁRIO (não do servidor).
    # Configurável via env para suportar deploys diversos:
    #
    # Desenvolvimento local (sem Caddy):
    #   GRAFANA_UI_URL=http://localhost:3000
    #   QDRANT_UI_URL=http://localhost:6333/dashboard
    #
    # Produção atrás de Caddy (mesma origin, recomendado):
    #   GRAFANA_UI_URL=/grafana/                ← path relativo, browser resolve
    #   QDRANT_UI_URL=/qdrant/dashboard
    #   → Caddy faz reverse_proxy /grafana/* → grafana:3000 e /qdrant/* → qdrant:6333
    #     (ver infra/caddy/Caddyfile). Funciona em qualquer domínio sem mexer aqui.
    #
    # Acesso via VPN/SSH tunnel local:
    #   (deixa default = path relativo; user faz `ssh -L 3000:127.0.0.1:3000` antes)
    #
    # Defaults: path relativo (assume Caddy). Quem rodar `uvicorn` puro em dev
    # local deve sobrescrever no .env.
    ui_qdrant = os.environ.get("QDRANT_UI_URL", "/qdrant/dashboard")
    ui_grafana = os.environ.get("GRAFANA_UI_URL", "/grafana/")
    # Deep links para Grafana Explore com datasource pré-selecionado.
    # Útil pra "Abrir UI" em Tempo/Loki ir DIRETO pra busca de traces/logs
    # em vez de cair na home do Grafana. Os UIDs Tempo/Loki batem com o
    # provisioning em infra/grafana/provisioning/datasources/datasources.yaml.
    # Configuráveis via env caso o user customize.
    ui_grafana_explore_tempo = os.environ.get(
        "GRAFANA_EXPLORE_TEMPO_URL",
        ui_grafana.rstrip("/") + '/explore?orgId=1&left=%7B%22datasource%22:%22tempo%22%7D',
    )
    ui_grafana_explore_loki = os.environ.get(
        "GRAFANA_EXPLORE_LOKI_URL",
        ui_grafana.rstrip("/") + '/explore?orgId=1&left=%7B%22datasource%22:%22loki%22,%22queries%22:%5B%7B%22expr%22:%22%7Bcontainer_name%3D~%5C%22.%2Aagente.%2A%5C%22%7D%22%7D%5D%7D',
    )

    checks = await asyncio.gather(
        _check_postgres(),
        _check_redis(),
        _check_http(
            "qdrant",
            f"{qdrant_url}/healthz",
            description="Vector DB para RAG (Onda 3)",
            ui_url=ui_qdrant,
        ),
        _check_duckdb(),
        _check_http(
            "opa",
            f"{opa_url}/health",
            description="Policy as Code — autorização (Onda 4a)",
        ),
        # Profile full: opcionais, podem não estar subidos.
        _check_http(
            "tempo",
            "http://tempo:3200/ready",
            description="Backend de traces OTLP (Onda 2)",
            ui_url=ui_grafana_explore_tempo,
            profile_full=True,
        ),
        _check_http(
            "loki",
            "http://loki:3100/ready",
            description="Backend de logs estruturados (Onda 2)",
            ui_url=ui_grafana_explore_loki,
            profile_full=True,
        ),
        _check_http(
            "grafana",
            "http://grafana:3000/api/health",
            description="UI de traces, logs e métricas",
            ui_url=ui_grafana,
            profile_full=True,
        ),
        _check_http(
            "promtail",
            # promtail expõe /metrics e /ready em :9080
            "http://promtail:9080/ready",
            description="Coletor de logs do docker → Loki",
            profile_full=True,
        ),
    )

    not_started_count = sum(1 for c in checks if c.get("not_started"))
    return {
        "services": checks,
        "summary": {
            "total": len(checks),
            "healthy": sum(1 for c in checks if c["ok"]),
            # Apenas serviços problemáticos REAIS (não conta opcionais que
            # nunca foram subidos — esses ficam em "not_started").
            "unhealthy": sum(1 for c in checks if not c["ok"] and not c.get("not_started")),
            "not_started": not_started_count,
        },
    }
