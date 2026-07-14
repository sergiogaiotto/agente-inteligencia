"""PR #234 — wizard de descoberta tem mais paths comuns e mensagem útil quando
a API simplesmente não publica OpenAPI.

# Contexto

Operador cadastrou Brasilapi como connector e clicou "Descobrir endpoints"
esperando que o sistema mapeasse automaticamente. O retorno foi:

    Descobrir não funcionou
    Não encontrei openapi.json nas rotas comuns. Se você tem a URL exata
    do spec, cole-a inteira. Se a API não expõe OpenAPI, preencha
    manualmente.

Verificação manual confirmou: Brasilapi NÃO publica OpenAPI/Swagger em
nenhum dos 15 caminhos comuns. Documentação deles é HTML estático
renderizado por Next.js a partir de YAML interno — não exposto.

A mensagem antiga sugeria primeiro "se você tem a URL exata" — mas o
operador NÃO tem, porque a API simplesmente não expõe. Sugestão errada
como primeira opção confunde quem é leigo em OpenAPI.

# Fixes

1. `_OPENAPI_CANDIDATE_PATHS` ganhou 7 paths comuns que estavam faltando:
   - `/v2/openapi.json`, `/api/v1/openapi.json`, `/api/v2/openapi.json`
   - `/v2/api-docs`, `/api/v3/api-docs` (Spring Boot variations)
   - `/swagger/v1/swagger.json` (ASP.NET / Swashbuckle)
   - `/api-docs/swagger.json` (Express + swagger-ui-express)

2. Mensagem reordenada: começa pela razão MAIS provável (API não publica),
   destaca o cURL wizard (atalho que funciona — PR #233 acabou de polir),
   e só depois sugere URL exata + preenchimento manual.

3. Mensagem mostra QUANTOS caminhos foram tentados — operador entende que
   já se esforçou e não vale a pena ele mesmo procurar paths obscuros.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── 1. Lista de paths candidatos ──────────────────────────────


class TestOpenApiCandidatePaths:
    def test_includes_fastapi_default(self):
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        assert "/openapi.json" in _OPENAPI_CANDIDATE_PATHS

    def test_includes_spring_boot_v3(self):
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        assert "/v3/api-docs" in _OPENAPI_CANDIDATE_PATHS

    def test_pr234_added_v2_paths(self):
        """PR #234 adicionou variações v2 que estavam faltando."""
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        for p in ("/v2/openapi.json", "/v2/api-docs",
                  "/api/v1/openapi.json", "/api/v2/openapi.json"):
            assert p in _OPENAPI_CANDIDATE_PATHS, f"falta {p}"

    def test_pr234_added_aspnet_path(self):
        """ASP.NET / Swashbuckle gera /swagger/v1/swagger.json — comum em
        APIs corporativas .NET."""
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        assert "/swagger/v1/swagger.json" in _OPENAPI_CANDIDATE_PATHS

    def test_pr234_added_express_swagger_path(self):
        """Express com swagger-ui-express comumente serve em /api-docs/swagger.json."""
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        assert "/api-docs/swagger.json" in _OPENAPI_CANDIDATE_PATHS

    def test_no_duplicates(self):
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        assert len(_OPENAPI_CANDIDATE_PATHS) == len(set(_OPENAPI_CANDIDATE_PATHS)), (
            f"duplicatas em _OPENAPI_CANDIDATE_PATHS: {_OPENAPI_CANDIDATE_PATHS}"
        )

    def test_grew_from_v0_baseline(self):
        """Sanity check: a lista não pode ter encolhido sob risco de tornar
        a descoberta menos eficaz. Se algum path for removido, este teste
        deve ser atualizado conscientemente."""
        from app.routes.api_connectors import _OPENAPI_CANDIDATE_PATHS
        assert len(_OPENAPI_CANDIDATE_PATHS) >= 12, (
            "lista de paths candidatos encolheu demais — confirme que isso é "
            "intencional. PR #234 estabeleceu o floor de 12+ paths."
        )


# ─── 2. Mensagem útil quando nada é encontrado ─────────────────


class TestHintWhenNoSpecFound:
    """O endpoint POST /introspect retorna `{found: False, hint: "..."}` para
    URLs que não expõem OpenAPI. Hint deve ser útil — sugerir primeiro a
    razão mais provável + atalhos que funcionam."""

    def _app(self, monkeypatch):
        """Mocka httpx pra que todas as tentativas retornem 404 — simula
        Brasilapi e qualquer outra API sem OpenAPI."""
        from app.routes import api_connectors

        class _Fake404Resp:
            status_code = 404
            headers = {"content-type": "text/html"}
            text = "<html>404</html>"
            def json(self): return {}

        class _FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, url, **kw): return _Fake404Resp()

        monkeypatch.setattr(api_connectors.httpx, "AsyncClient", _FakeClient)
        # SEC-01: o introspect valida o host (getaddrinfo real) antes do fetch —
        # mocka p/ IP público, senão o teste depende de DNS (brasilapi.com.br) e
        # flaka em run cheio (o guard devolveria 400 em vez do hint).
        import app.core.ssrf as _ssrf
        monkeypatch.setattr(
            _ssrf.socket, "getaddrinfo",
            lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))],
        )

        app = FastAPI()
        app.include_router(api_connectors.router)
        return TestClient(app)

    def test_hint_mentions_curl_alternative(self, monkeypatch):
        """Hint deve sugerir o cURL wizard como atalho, porque ele FUNCIONA
        para APIs sem OpenAPI (Brasilapi etc.). Sem isso, operador novato
        fica perdido."""
        client = self._app(monkeypatch)
        r = client.post("/api/v1/api-connectors/introspect", json={
            "url": "https://brasilapi.com.br",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["found"] is False
        hint = body.get("hint", "").lower()
        assert "curl" in hint, f"hint não menciona cURL: {body.get('hint')}"

    def test_hint_starts_with_likely_reason(self, monkeypatch):
        """Razão mais provável (API não publica) deve estar nas primeiras
        palavras — não enterrada no meio do texto."""
        client = self._app(monkeypatch)
        body = client.post("/api/v1/api-connectors/introspect", json={
            "url": "https://brasilapi.com.br",
        }).json()
        hint = body.get("hint", "")
        first_60 = hint[:60].lower()
        # Aceita variações: "não expõe", "não publica" etc
        assert any(s in first_60 for s in ("não expõe", "não publica", "provavelmente")), (
            f"primeiros 60 chars do hint não indicam a causa principal: {hint[:120]!r}"
        )

    def test_hint_reports_how_many_paths_tried(self, monkeypatch):
        """Mostrar a contagem evita o operador achar que faltou tentar
        algum path óbvio."""
        client = self._app(monkeypatch)
        body = client.post("/api/v1/api-connectors/introspect", json={
            "url": "https://brasilapi.com.br",
        }).json()
        hint = body.get("hint", "")
        # Deve ter algum dígito (a contagem)
        import re
        assert re.search(r"\d+\s+caminho", hint), (
            f"hint não menciona quantidade de paths tentados: {hint!r}"
        )
