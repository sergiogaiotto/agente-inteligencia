"""Hardening do rate-limit: bucket correto p/ LLM + GC de memória.

- `_bucket_for_path`: rotas que disparam LLM (invoke de agent/pipeline, workspace,
  wizard) precisam cair no bucket caro 'workspace' — a versão antiga sofria de
  precedência de operador e as deixava no bucket genérico (findings 27/30).
- `_MemoryLimiter._gc`: remove chaves ociosas (finding 46 — CWE-400).
"""
from __future__ import annotations

import pytest

from app.core.ratelimit import _MemoryLimiter, _bucket_for_path


@pytest.mark.parametrize("path", [
    "/api/v1/workspace/chat",
    "/api/v1/wizard/skill",
    "/api/v1/agents/abc-123/invoke",
    "/api/v1/agents/abc-123/run",
    "/api/v1/pipelines/p1/invoke",
    "/api/v1/pipelines/p1/invoke/stream",
])
def test_llm_routes_use_workspace_bucket(path):
    bucket, _ = _bucket_for_path(path)
    assert bucket == "workspace", f"{path} deveria estar no bucket caro 'workspace'"


def test_login_bucket():
    assert _bucket_for_path("/api/v1/users/login")[0] == "auth"


def test_generic_api_bucket():
    assert _bucket_for_path("/api/v1/agents")[0] == "api"          # listagem, não invoke
    assert _bucket_for_path("/api/v1/catalog/entries")[0] == "api"


def test_static_exempt():
    assert _bucket_for_path("/static/js/app.js") == ("static", 0)


def test_memory_limiter_gc_removes_idle_keys():
    lim = _MemoryLimiter()
    now = 1_000_000.0
    # chave ociosa (último acesso muito antigo) e chave ativa (agora)
    lim._buckets["rl:api:ip:old"] = [now - lim._GC_HORIZON - 10]
    lim._buckets["rl:api:ip:active"] = [now]
    lim._gc(now)
    assert "rl:api:ip:old" not in lim._buckets
    assert "rl:api:ip:active" in lim._buckets


@pytest.mark.asyncio
async def test_memory_limiter_enforces_limit():
    lim = _MemoryLimiter()
    ok = []
    for _ in range(5):
        allowed, remaining, _ = await lim.check("k", limit=3, window=60)
        ok.append(allowed)
    # 3 primeiras passam, as demais são bloqueadas
    assert ok == [True, True, True, False, False]
