"""User pediu (2026-06-01): 2 melhorias na UI do AI Mesh.

1. Editar Conexão deve estar pré-posicionado na conexão atual.
2. Quando Condicional, wizard passo a passo + simulação considerando
   contexto dos agentes (AOBD/AR/SA), com todas variáveis possíveis.

Stack desta PR:
- `_build_conditional_context()` em engine.py — vars centralizadas
  (output, output_lower, output_length, has_output, final_state,
  is_recommend/is_refuse/is_escalate, contains_image/url/pdf,
  lines_count). Reusado por runtime e endpoint de teste.
- `CONDITIONAL_VARS_META` — metadata declarativa (nome/tipo/desc)
  consumida pela UI do vars panel.
- `GET /api/v1/mesh/conditional-vars` — lista as vars para o frontend.
- `POST /api/v1/mesh/connections/test-conditional` — avalia expr contra
  output/final_state simulados. Fail-CLOSED (operador VER o erro).
- Frontend: modal mais largo + header com breadcrumb, tabs Contexto/
  Padrão/Refinar/Simular, vars panel clicável, simulador debounce.
- Scroll automático + highlight pulsando no item editado.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── _build_conditional_context ─────────────────────────────────────


class TestBuildConditionalContext:
    def test_all_vars_present(self):
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(output="Hello World", final_state="Recommend")
        # Checa todas as 12 keys
        expected = {
            "output", "output_lower", "output_length", "has_output",
            "final_state", "is_recommend", "is_refuse", "is_escalate",
            "contains_image", "contains_url", "contains_pdf", "lines_count",
        }
        assert expected.issubset(set(ctx.keys()))

    def test_empty_output_has_safe_defaults(self):
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(output=None, final_state=None)
        assert ctx["output"] == ""
        assert ctx["output_length"] == 0
        assert ctx["has_output"] is False
        assert ctx["final_state"] == ""
        assert ctx["is_recommend"] is False
        assert ctx["lines_count"] == 0

    def test_state_atalhos(self):
        """is_recommend / is_refuse / is_escalate são atalhos para evitar
        que o operador erre na string."""
        from app.agents.engine import _build_conditional_context
        ctx = _build_conditional_context(final_state="Recommend")
        assert ctx["is_recommend"] is True
        assert ctx["is_refuse"] is False

        ctx2 = _build_conditional_context(final_state="refuse")  # case-insensitive
        assert ctx2["is_refuse"] is True

    def test_content_detectors(self):
        """contains_image / contains_url / contains_pdf detectam padrões
        comuns para ajudar o operador a rotear por tipo de conteúdo."""
        from app.agents.engine import _build_conditional_context
        assert _build_conditional_context(output="veja foto.jpg")["contains_image"] is True
        assert _build_conditional_context(output="Imagem analisada")["contains_image"] is True
        assert _build_conditional_context(output="acesse https://x.com")["contains_url"] is True
        assert _build_conditional_context(output="relatorio.pdf anexo")["contains_pdf"] is True

    def test_lines_count(self):
        from app.agents.engine import _build_conditional_context
        assert _build_conditional_context(output="a\nb\nc")["lines_count"] == 3
        assert _build_conditional_context(output="só uma linha")["lines_count"] == 1
        assert _build_conditional_context(output="")["lines_count"] == 0


class TestConditionalVarsMeta:
    def test_meta_lists_all_vars_from_context(self):
        from app.agents.engine import _build_conditional_context, CONDITIONAL_VARS_META
        ctx_keys = set(_build_conditional_context().keys())
        meta_names = {v["name"] for v in CONDITIONAL_VARS_META}
        # Toda var do contexto runtime tem metadata correspondente — evita
        # drift entre o que está disponível e o que o user vê
        missing = ctx_keys - meta_names
        assert not missing, f"vars sem metadata: {missing}"

    def test_meta_has_type_and_desc(self):
        from app.agents.engine import CONDITIONAL_VARS_META
        for v in CONDITIONAL_VARS_META:
            assert v["name"] and isinstance(v["name"], str)
            assert v["type"] in {"str", "int", "bool", "float"}
            assert v["desc"] and len(v["desc"]) > 10


# ─── Endpoint /conditional-vars + /test-conditional ────────────────


@pytest.fixture
def mesh_client():
    from app.routes.mesh import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestConditionalVarsEndpoint:
    def test_returns_vars_list_with_metadata(self, mesh_client):
        r = mesh_client.get("/api/v1/mesh/conditional-vars")
        assert r.status_code == 200
        body = r.json()
        assert "vars" in body
        assert len(body["vars"]) >= 10  # 12 vars no mínimo
        for v in body["vars"]:
            assert "name" in v and "type" in v and "desc" in v


class TestTestConditionalEndpoint:
    def test_simple_true_expression(self, mesh_client):
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "'imagem' in output_lower", "output": "tem imagem", "final_state": ""},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["result"] is True
        assert "error" not in body

    def test_simple_false_expression(self, mesh_client):
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "'imagem' in output_lower", "output": "só texto", "final_state": ""},
        )
        assert r.json()["result"] is False

    def test_uses_expanded_vars(self, mesh_client):
        """O endpoint deve expor as vars expandidas (output_length, is_recommend, etc)."""
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "is_recommend and output_length > 5", "output": "resposta", "final_state": "Recommend"},
        )
        assert r.json()["result"] is True

    def test_empty_expr_returns_error(self, mesh_client):
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "", "output": "x", "final_state": ""},
        )
        body = r.json()
        assert "error" in body
        assert "vazia" in body["error"].lower()

    def test_invalid_expr_returns_error_with_context(self, mesh_client):
        """Sintaxe inválida → fail-CLOSED: erro vai para o operador (oposto
        do fail-OPEN do runtime). Operador vê e corrige antes de salvar."""
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "this is not valid jinja !!!", "output": "x", "final_state": ""},
        )
        body = r.json()
        assert "error" in body
        assert "context" in body  # retorna context pro user debugar
