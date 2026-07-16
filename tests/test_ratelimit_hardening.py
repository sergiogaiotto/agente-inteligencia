"""Hardening do rate-limit: bucket correto p/ LLM + GC de memória.

- `_bucket_for_path`: rotas que disparam LLM (invoke de agent/pipeline, workspace,
  wizard) precisam cair no bucket caro 'workspace' — a versão antiga sofria de
  precedência de operador e as deixava no bucket genérico (findings 27/30).
- `_MemoryLimiter._gc`: remove chaves ociosas (finding 46 — CWE-400).
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.core.ratelimit import (
    _MemoryLimiter,
    _bucket_for_path,
    _client_identity,
    _resolve_client_ip,
)


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


# ── Incidente "tela em branco no clique rápido" (QA E2E VPS) ──
# Clicar em vários menus rápido dispara muitos fetches de leitura por página;
# com o balde 'api' em 60/60s isso batia 429 e a tela ficava EM BRANCO (o front
# engolia o erro). O balde de leituras precisa folgar SEM afrouxar os caros.

def test_leituras_da_ui_folgadas_sem_afrouxar_llm():
    from app.core.config import get_settings
    s = get_settings()
    api_limit = _bucket_for_path("/api/v1/agents")[1]
    ws_limit = _bucket_for_path("/api/v1/workspace/chat")[1]
    auth_limit = _bucket_for_path("/api/v1/users/login")[1]
    # leituras: generosas o bastante para navegação humana intensa
    # (uma página pesada faz ~7-10 fetches; ~10 páginas/janela não pode 429).
    assert api_limit >= 240, "balde de leituras baixo demais reintroduz a tela em branco"
    # os baldes de custo/segurança seguem MUITO mais apertados que o de leitura
    assert ws_limit <= 40 and ws_limit < api_limit
    assert auth_limit <= 15 and auth_limit < api_limit
    assert s.rate_limit_window_seconds == 60


def test_uma_pagina_pesada_nao_estoura_o_balde_de_leitura():
    """O Fluxo de agentes dispara ~7 GETs; várias navegações numa janela têm
    que caber com folga no balde 'api' (regressão do incidente)."""
    fetches_por_pagina = [
        "/api/v1/mesh/topology", "/api/v1/mesh/layout", "/api/v1/mesh/groups",
        "/api/v1/mesh/conditional-vars", "/api/v1/pipelines",
        "/api/v1/agents", "/api/v1/llm/health",
    ]
    for p in fetches_por_pagina:
        assert _bucket_for_path(p)[0] in ("api",), f"{p} deveria ser leitura barata 'api'"
    api_limit = _bucket_for_path("/api/v1/agents")[1]
    paginas_por_janela = api_limit // len(fetches_por_pagina)
    assert paginas_por_janela >= 30, "poucas páginas por janela → clique rápido volta a dar 429"


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


# ═══════════════════════════════════════════════════════════════
# SEC-05 — X-Forwarded-For resistente a spoof
# ═══════════════════════════════════════════════════════════════


def _make_request(peer_host: str, xff: str | None = None) -> Request:
    """Request Starlette mínima com peer + XFF, sem cookie/API-key (cai no path de IP)."""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/users/login",
        "raw_path": b"/api/v1/users/login",
        "query_string": b"",
        "headers": headers,
        "client": (peer_host, 4444),
        "scheme": "http",
        "server": ("app", 8000),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from app.core import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


class TestXffTrustedProxy:
    def test_peer_publico_direto_ignora_xff_forjado(self):
        # Conexão direta de IP público → XFF é spoof, deve ser ignorado.
        req = _make_request("203.0.113.7", xff="9.9.9.9")
        assert _resolve_client_ip(req) == "203.0.113.7"

    def test_peer_privado_confiavel_honra_xff(self):
        # Caddy (rede Docker, IP privado) → confia no XFF = cliente real.
        req = _make_request("172.20.0.5", xff="198.51.100.23")
        assert _resolve_client_ip(req) == "198.51.100.23"

    def test_spoof_prepend_usa_mais_a_direita_nao_confiavel(self):
        # Atacante injeta 1.2.3.4 à esquerda; Caddy anexa o real à direita.
        req = _make_request("172.20.0.5", xff="1.2.3.4, 198.51.100.23")
        assert _resolve_client_ip(req) == "198.51.100.23"

    def test_peer_confiavel_sem_xff_usa_peer(self):
        req = _make_request("127.0.0.1", xff=None)
        assert _resolve_client_ip(req) == "127.0.0.1"

    def test_todos_hops_confiaveis_cai_no_peer(self):
        req = _make_request("10.0.0.9", xff="10.0.0.1, 172.16.0.2")
        assert _resolve_client_ip(req) == "10.0.0.9"

    def test_identidade_do_ratelimit_usa_ip_resolvido(self):
        # O bypass fechado: spoof não gera balde novo — identidade = peer direto.
        req = _make_request("203.0.113.7", xff="9.9.9.9")
        assert _client_identity(req) == "ip:203.0.113.7"

    def test_trusted_proxies_configurado_restringe_a_ips_exatos(self, monkeypatch):
        from app.core import config

        monkeypatch.setenv("TRUSTED_PROXIES", "192.0.2.10/32")
        config.get_settings.cache_clear()
        # peer NÃO está na allowlist exata → conexão direta, ignora XFF
        assert _resolve_client_ip(_make_request("172.20.0.5", xff="9.9.9.9")) == "172.20.0.5"
        # peer na allowlist exata → confia no XFF
        assert _resolve_client_ip(_make_request("192.0.2.10", xff="198.51.100.5")) == "198.51.100.5"
