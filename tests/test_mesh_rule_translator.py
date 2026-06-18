"""Tradutor NL→Jinja (Fatia 4) — descrição em pt-BR → regra condicional.

DNA "IA sugere → sistema PROVA": o LLM propõe a expressão e o backend
reconcilia contra `CONDITIONAL_VARS_META`. O teste mais importante aqui trava
a ARMADILHA que o estudo apontou como killer objection: `meta.
find_undeclared_variables` sobre expressão NUA retorna vazio → guardrail vira
selo sempre-verde. `validate_conditional_expression` envolve em `{{ }}` antes
de parsear; o teste abaixo falha se alguém remover esse embrulho.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.conditional_suggest import (
    build_suggest_messages,
    extract_expression,
    validate_conditional_expression,
)


def _canonical() -> set[str]:
    from app.agents.engine import CONDITIONAL_VARS_META
    return {v["name"] for v in CONDITIONAL_VARS_META}


# ─── validate_conditional_expression (o cofre) ───────────────────────────────

class TestValidateExpression:
    def test_valid_expr_known_vars(self):
        r = validate_conditional_expression("'pix' in output_lower or has_document", _canonical())
        assert r["valid"] is True
        assert "output_lower" in r["used_vars"] and "has_document" in r["used_vars"]
        assert r["unknown_vars"] == []

    def test_unknown_var_is_caught(self):
        """TRAVA DA ARMADILHA: var inexistente DEVE ser pega. Se o {{ }} sumisse,
        find_undeclared_variables devolveria set() vazio e isto viraria valid=True."""
        r = validate_conditional_expression("foo_bar_inexistente and output_lower", _canonical())
        assert r["valid"] is False
        assert "foo_bar_inexistente" in r["unknown_vars"]
        assert "output_lower" not in r["unknown_vars"]  # essa existe
        assert r["error"]

    def test_bare_expression_actually_sees_variables(self):
        """Prova direta de que o embrulho {{ }} faz as variáveis aparecerem —
        used_vars NÃO pode ser vazio para uma expressão que cita variáveis."""
        r = validate_conditional_expression("is_refuse", _canonical())
        assert r["used_vars"] == ["is_refuse"]
        assert r["valid"] is True

    def test_invalid_jinja_syntax(self):
        r = validate_conditional_expression("'pix' in in in", _canonical())
        assert r["valid"] is False
        assert "inválida" in r["error"].lower() or "invalid" in r["error"].lower()

    def test_empty_expr(self):
        r = validate_conditional_expression("   ", _canonical())
        assert r["valid"] is False
        assert r["used_vars"] == []

    def test_filters_are_not_flagged_as_unknown_vars(self):
        """Filtros Jinja (length, upper) não são variáveis — não viram unknown."""
        r = validate_conditional_expression("output_lower | length > 5", _canonical())
        assert r["valid"] is True
        assert "length" not in r["unknown_vars"]


# ─── extract_expression ──────────────────────────────────────────────────────

class TestExtractExpression:
    def test_plain(self):
        assert extract_expression("'pix' in output_lower") == "'pix' in output_lower"

    def test_strips_code_fence(self):
        assert extract_expression("```jinja\nhas_document\n```") == "has_document"

    def test_keeps_single_quotes(self):
        """Aspas simples são literais Jinja — NÃO podem ser removidas."""
        assert extract_expression("'pix' in output_lower").startswith("'pix'")

    def test_first_meaningful_line(self):
        assert extract_expression("\n\nis_refuse\nlixo depois") == "is_refuse"


# ─── build_suggest_messages ──────────────────────────────────────────────────

class TestBuildMessages:
    def test_uses_live_vocabulary(self):
        from app.agents.engine import CONDITIONAL_VARS_META
        msgs = build_suggest_messages("se mencionar pix", CONDITIONAL_VARS_META)
        assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "se mencionar pix"
        # o catálogo de vars vivo entra no prompt (fonte única, sem drift)
        assert "output_lower" in msgs[0]["content"]
        assert "has_document" in msgs[0]["content"]


# ─── Endpoint /connections/suggest-conditional (LLM mockado) ─────────────────

@pytest.fixture
def client(monkeypatch):
    from app.routes.mesh import router
    from app.core.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "u1", "role": "comum"}

    async def _fake_resolve(task_type, has_image=False):
        return ("openai", "gpt-x")
    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", _fake_resolve)
    return app, monkeypatch


def test_endpoint_returns_validated_expr(client):
    app, monkeypatch = client

    async def _fake_llm(messages, provider, model, *, route, temperature=None, response_format=None):
        return ("'pix' in output_lower or has_document", provider, model)
    monkeypatch.setattr("app.routes.wizard._wizard_llm_complete", _fake_llm)

    c = TestClient(app)
    r = c.post("/api/v1/mesh/connections/suggest-conditional", json={"description": "se falar de pix ou anexar documento"})
    assert r.status_code == 200
    body = r.json()
    assert body["expr"] == "'pix' in output_lower or has_document"
    assert body["valid"] is True
    assert body["unknown_vars"] == []


def test_endpoint_flags_hallucinated_var(client):
    """IA alucina uma var inexistente → endpoint marca valid=False (não engole)."""
    app, monkeypatch = client

    async def _fake_llm(messages, provider, model, *, route, temperature=None, response_format=None):
        return ("cliente_irritado and output_lower", provider, model)
    monkeypatch.setattr("app.routes.wizard._wizard_llm_complete", _fake_llm)

    c = TestClient(app)
    r = c.post("/api/v1/mesh/connections/suggest-conditional", json={"description": "se o cliente estiver irritado"})
    body = r.json()
    assert body["valid"] is False
    assert "cliente_irritado" in body["unknown_vars"]


def test_endpoint_empty_description(client):
    app, _ = client
    c = TestClient(app)
    r = c.post("/api/v1/mesh/connections/suggest-conditional", json={"description": "  "})
    assert "error" in r.json()


# ─── Template cabeado (tradutor na galeria) ──────────────────────────────────

def test_template_wires_translator():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh_flow.html").read_text(encoding="utf-8")
    assert "suggestRule()" in html
    assert "useSuggestion()" in html
    assert "/api/v1/mesh/connections/suggest-conditional" in html
    assert 'x-model="editor.nlDesc"' in html
    # cai no modo manual ao usar (did-you-mean assiste se a IA escorregar)
    assert "this.editor.ruleMode = 'manual'" in html
