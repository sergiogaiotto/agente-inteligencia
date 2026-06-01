"""Context Scope — endpoints da API (Fase 2 da feature).

Cobre os 2 endpoints novos no wizard de Edição de Conexão:

- `GET /api/v1/mesh/context-scope-vars` — lista vars + modos (vars panel
  + seletor de modo)
- `POST /api/v1/mesh/connections/test-context-scope` — aplica scope
  contra output simulado (preview no simulador). Fail-CLOSED — operador
  vê o erro pra corrigir antes de salvar.

PRs encadeados: Foundation (#256) → API (este) → UI Wizard (próximo).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def mesh_client():
    from app.routes.mesh import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ─── GET /context-scope-vars ────────────────────────────────────────


class TestContextScopeVarsEndpoint:
    def test_returns_vars_list_with_metadata(self, mesh_client):
        r = mesh_client.get("/api/v1/mesh/context-scope-vars")
        assert r.status_code == 200
        body = r.json()
        assert "vars" in body
        assert len(body["vars"]) >= 10
        for v in body["vars"]:
            assert "name" in v and "type" in v and "desc" in v

    def test_returns_modes_with_id_label_desc(self, mesh_client):
        """UI precisa de label humano + descrição pra cada modo."""
        r = mesh_client.get("/api/v1/mesh/context-scope-vars")
        body = r.json()
        assert "modes" in body
        mode_ids = {m["id"] for m in body["modes"]}
        assert mode_ids == {"inherit", "scoped", "isolated"}
        for m in body["modes"]:
            assert m["label"] and m["desc"]
            assert len(m["desc"]) > 20

    def test_canonical_modes_match_engine(self, mesh_client):
        """Sanity: lista canônica do endpoint == constante do engine.
        Evita drift entre UI e runtime."""
        from app.agents.engine import CONTEXT_SCOPE_MODES
        r = mesh_client.get("/api/v1/mesh/context-scope-vars")
        body = r.json()
        assert set(body["_modes_canonical"]) == set(CONTEXT_SCOPE_MODES)


# ─── POST /connections/test-context-scope ───────────────────────────


class TestTestContextScopeEndpoint:
    URL = "/api/v1/mesh/connections/test-context-scope"

    def test_mode_inherit_returns_output_cru(self, mesh_client):
        r = mesh_client.post(self.URL, json={
            "mode": "inherit",
            "output": "conteúdo completo",
            "final_state": "",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "inherit"
        assert body["output"] == "conteúdo completo"
        assert body["skip_prefix"] is False
        assert body["chars_before"] == body["chars_after"]
        assert body["reduction_pct"] == 0.0
        assert "error" not in body

    def test_mode_isolated_returns_empty_and_skip_prefix(self, mesh_client):
        r = mesh_client.post(self.URL, json={
            "mode": "isolated",
            "output": "conteúdo confidencial",
        })
        body = r.json()
        assert body["mode"] == "isolated"
        assert body["output"] == ""
        assert body["skip_prefix"] is True
        assert body["chars_after"] == 0
        assert body["reduction_pct"] == 100.0

    def test_mode_scoped_with_template_transforms(self, mesh_client):
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "template": "output | upper",
            "output": "hello",
        })
        body = r.json()
        assert body["mode"] == "scoped"
        assert body["output"] == "HELLO"
        assert body["effective_template"] == "output | upper"
        assert body["chars_before"] == 5
        assert body["chars_after"] == 5

    def test_mode_scoped_with_max_chars_truncates(self, mesh_client):
        """Atalho: max_chars=N sem template ⇢ vira `output[:N]` interno.
        `effective_template` revela o template real pra transparência."""
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "max_chars": 10,
            "output": "a" * 100,
        })
        body = r.json()
        assert body["output"] == "a" * 10
        assert body["chars_after"] == 10
        assert body["chars_before"] == 100
        assert body["effective_template"] == "output[:10]"
        assert body["reduction_pct"] == 90.0

    def test_mode_scoped_template_takes_precedence_over_max_chars(self, mesh_client):
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "template": "output | upper",
            "max_chars": 3,
            "output": "hello",
        })
        body = r.json()
        # Se max_chars vencesse: "hel"; como template ganha: "HELLO"
        assert body["output"] == "HELLO"
        assert body["effective_template"] == "output | upper"

    def test_mode_scoped_without_template_or_max_chars_returns_error(self, mesh_client):
        """Operador escolheu scoped mas não definiu regra → fail-CLOSED:
        endpoint avisa pra escolher antes de salvar."""
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "output": "hello",
        })
        body = r.json()
        assert "error" in body
        assert "scoped" in body["error"].lower()
        assert "context" in body

    def test_invalid_template_returns_error_with_context(self, mesh_client):
        """Sintaxe inválida → fail-CLOSED: operador vê o erro pra corrigir.
        Em runtime (`_resolve_context_scope`) seria fail-OPEN."""
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "template": "syntax !!!",
            "output": "hello",
        })
        body = r.json()
        assert "error" in body
        assert "context" in body
        assert body["effective_template"] == "syntax !!!"

    def test_invalid_mode_returns_error(self, mesh_client):
        r = mesh_client.post(self.URL, json={
            "mode": "nuke-everything",
            "output": "hello",
        })
        body = r.json()
        assert "error" in body
        assert "inválido" in body["error"].lower() or "invalid" in body["error"].lower()

    def test_uses_final_state_in_template(self, mesh_client):
        """`final_state` deve estar disponível no contexto Jinja — útil
        para "se anterior foi Refuse, mandar 'Refused'"."""
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "template": "'REFUSED' if final_state == 'Refuse' else output",
            "output": "texto longo aqui",
            "final_state": "Refuse",
        })
        body = r.json()
        assert body["output"] == "REFUSED"

    def test_empty_output_safe(self, mesh_client):
        """Output vazio na simulação não explode."""
        r = mesh_client.post(self.URL, json={
            "mode": "scoped",
            "max_chars": 50,
            "output": "",
        })
        body = r.json()
        assert body["output"] == ""
        assert body["chars_before"] == 0
        assert body["chars_after"] == 0
        assert body["reduction_pct"] == 0.0

    def test_returns_context_vars_for_debugging(self, mesh_client):
        """Resposta sempre inclui `context` (vars Jinja avaliadas) pra
        operador entender o que tava disponível na hora da simulação."""
        r = mesh_client.post(self.URL, json={
            "mode": "inherit",
            "output": "Hello https://x.com",
            "final_state": "Recommend",
        })
        ctx = r.json()["context"]
        # Vars expandidas devem aparecer
        assert ctx["output"] == "Hello https://x.com"
        assert ctx["contains_url"] is True
        assert ctx["is_recommend"] is True
        assert ctx["output_length"] == 19
