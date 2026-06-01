"""User pediu (2026-06-01): como definir as regras do "Conditional" no AI Mesh?

Antes deste PR, o tipo "Conditional" no form do mesh era decorativo —
gravava `connection_type='conditional'` + `config="{}"` mas:
- UI: nenhum campo para definir a regra
- Runtime: `_resolve_ordered_chain` ignorava `connection_type`,
  executando BFS puro

Agora:
- UI: textarea aparece quando type=conditional, com placeholder Jinja
- Storage: `config={"expr": "<jinja>"}` em mesh_repo
- Runtime: `_should_skip_conditional` avalia expr contra output do
  agente upstream antes de cada execução em `execute_pipeline`

Política de erro: **fail-open**. Qualquer falha (config malformado,
expr inválida, exception no Jinja) loga warning e NÃO skipa — agente
executa, operador vê o erro em errors.log. Melhor executar errado do
que perder dado silenciosamente.
"""
from __future__ import annotations

import json
import logging

import pytest


# ─── _eval_conditional (helper isolado) ─────────────────────────────


class TestEvalConditional:
    def test_simple_true_expression(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "tem imagem", "output_lower": "tem imagem", "final_state": "Recommend"}
        assert _eval_conditional("'imagem' in output_lower", ctx) is True

    def test_simple_false_expression(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "só texto", "output_lower": "só texto", "final_state": "Recommend"}
        assert _eval_conditional("'imagem' in output_lower", ctx) is False

    def test_final_state_match(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "x", "output_lower": "x", "final_state": "Refuse"}
        assert _eval_conditional("final_state == 'Refuse'", ctx) is True

    def test_length_check(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "x" * 100, "output_lower": "x" * 100, "final_state": ""}
        assert _eval_conditional("output|length > 50", ctx) is True
        assert _eval_conditional("output|length > 500", ctx) is False

    def test_invalid_expr_raises(self):
        """Sintaxe inválida → exception (caller decide se fail-open ou closed)."""
        from app.agents.engine import _eval_conditional
        with pytest.raises(Exception):
            _eval_conditional("this is not valid jinja !!!", {"output": ""})

    def test_undefined_variable_does_not_explode(self):
        """ChainableUndefined: usar var não definida não levanta — útil
        para exprs de operadores menos familiarizados (não derruba pipeline
        por typo)."""
        from app.agents.engine import _eval_conditional
        ctx = {"output": "x", "output_lower": "x", "final_state": ""}
        # `intent` não existe — deve avaliar para falsy sem explodir
        result = _eval_conditional("intent == 'image'", ctx)
        assert result is False


# ─── _should_skip_conditional ────────────────────────────────────────


class TestShouldSkipConditional:
    @pytest.mark.asyncio
    async def test_non_conditional_connection_never_skips(self, monkeypatch):
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "sequential",
                "config": "{}",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b", last_output="x", last_final_state="",
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_conditional_without_expr_passes(self, monkeypatch):
        """config={} (sem expr) = sempre passa (equivalente a sequencial)."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": "{}",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b", last_output="x", last_final_state="",
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_conditional_expr_true_does_not_skip(self, monkeypatch):
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "'imagem' in output_lower"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b",
            last_output="tem imagem aqui", last_final_state="",
        )
        assert out is False  # expr=true → não skipa

    @pytest.mark.asyncio
    async def test_conditional_expr_false_skips(self, monkeypatch):
        """REGRESSÃO do bug central: quando a regra avalia false, agente é
        pulado (passthrough)."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "'imagem' in output_lower"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b",
            last_output="só texto puro", last_final_state="",
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_malformed_config_fails_open_and_logs(self, monkeypatch, caplog):
        """Config JSON inválido → fail-open + log warning."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": "{ not valid json",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._should_skip_conditional(
                source_id="a", target_id="b", last_output="x", last_final_state="",
            )

        assert out is False  # fail-open
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.conditional" in events

    @pytest.mark.asyncio
    async def test_eval_error_fails_open_and_logs(self, monkeypatch, caplog):
        """Expr com erro de sintaxe → fail-open + log warning."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "syntax !!!"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._should_skip_conditional(
                source_id="a", target_id="b", last_output="x", last_final_state="",
            )

        assert out is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.conditional" in events
        # Extra carrega a expr e o tipo do erro pra troubleshooting
        rec = next(r for r in caplog.records if getattr(r, "event", None) == "mesh.conditional")
        assert getattr(rec, "expr", None) == "syntax !!!"


# ─── Source smoke: UI tem o campo, backend topology expõe config ──


class TestUiAndBackendWiring:
    def test_mesh_html_has_condition_expr_field(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh.html").read_text(encoding="utf-8")
        # Textarea x-show condicional
        assert "x-show=\"connForm.connection_type === 'conditional'\"" in src
        assert "x-model=\"connForm.condition_expr\"" in src
        # Helpers mostrados na UI
        assert "output_lower" in src
        assert "final_state" in src

    def test_save_connection_serializes_expr_when_conditional(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh.html").read_text(encoding="utf-8")
        # Payload usa expr só quando type=conditional
        assert "if (this.connForm.connection_type === 'conditional')" in src
        # 2026-06-01: refactor do saveConnection para suportar dois eixos
        # ortogonais no mesmo `config` (expr + context_scope). A expr
        # continua sendo serializada exclusivamente quando type=conditional,
        # mas agora via `cfg.expr = expr` em vez do ternário inline antigo.
        assert "cfg.expr = expr" in src
        assert "JSON.stringify(cfg)" in src

    def test_topology_endpoint_returns_config(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "routes" / "mesh.py").read_text(encoding="utf-8")
        # Edge agora carrega config (string JSON)
        assert "\"config\": c.get(\"config\") or \"{}\"" in src
