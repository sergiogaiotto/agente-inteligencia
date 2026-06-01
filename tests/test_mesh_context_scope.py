"""Context Scope no AI Mesh (2026-06-01).

Controla o QUE do output do agente anterior vira contexto do próximo na
mesh chain. Três modos por conexão (`mesh_connections.config.context_scope`):

- `inherit` (default): output cru vira prefix "## Contexto do agente
  anterior" — comportamento histórico, retrocompatível.
- `scoped`: output passa por transform Jinja sandboxed antes de virar
  prefix. Economiza tokens (cap de tamanho) e governance (extrair só a
  parte relevante). Reusa o `SandboxedEnvironment` do conditional.
- `isolated`: próximo agente recebe SÓ `user_input`, sem prefix algum —
  útil para SAs atômicos que não devem ser contaminados pelo histórico.

Funciona em complemento ao conditional routing — eles coexistem na mesma
conexão. Conditional decide SE o agente downstream executa; scope decide
QUE PARTE do output anterior entra como contexto.

Política de erro: **fail-OPEN** em runtime (igual conditional). Qualquer
falha (config malformado, template Jinja inválido, repo error) loga
warning/error em `errors.log` e retorna `inherit` — melhor over-share
contexto que perder dado por bug de regra.
"""
from __future__ import annotations

import json
import logging

import pytest


# ─── _apply_context_scope_template (helper isolado) ──────────────────


class TestApplyContextScopeTemplate:
    def test_truncate_via_slicing(self):
        from app.agents.engine import _apply_context_scope_template
        ctx = {"output": "a" * 100, "output_lower": "", "output_length": 100}
        out = _apply_context_scope_template("output[:20]", ctx)
        assert out == "a" * 20

    def test_first_line_via_split(self):
        from app.agents.engine import _apply_context_scope_template
        ctx = {"output": "primeira\nsegunda\nterceira", "output_lower": "", "output_length": 0}
        out = _apply_context_scope_template("output.split('\\n')[0]", ctx)
        assert out == "primeira"

    def test_jinja_filter_upper(self):
        from app.agents.engine import _apply_context_scope_template
        ctx = {"output": "hello", "output_lower": "hello", "output_length": 5}
        out = _apply_context_scope_template("output | upper", ctx)
        assert out == "HELLO"

    def test_none_result_coerces_to_empty(self):
        from app.agents.engine import _apply_context_scope_template
        ctx = {"output": "x", "output_lower": "x", "output_length": 1}
        # `none` é literal Jinja; resultado None vira ""
        out = _apply_context_scope_template("none", ctx)
        assert out == ""

    def test_non_string_result_coerces_to_str(self):
        from app.agents.engine import _apply_context_scope_template
        ctx = {"output": "abc", "output_lower": "abc", "output_length": 3}
        out = _apply_context_scope_template("output_length", ctx)
        assert out == "3"
        assert isinstance(out, str)

    def test_invalid_template_raises(self):
        """Sintaxe inválida → levanta (caller decide fail-open)."""
        from app.agents.engine import _apply_context_scope_template
        with pytest.raises(Exception):
            _apply_context_scope_template("not valid jinja !!!", {"output": ""})


# ─── _resolve_context_scope (decisor principal) ──────────────────────


def _patch_mesh(monkeypatch, *, source_id="a", target_id="b", connection_type="sequential", config="{}"):
    """Helper: monkeypatcha `mesh_repo.find_all` pra devolver uma única
    conexão source→target com config controlada."""
    async def fake_find_all(source_agent_id=None, **_):
        return [{
            "source_agent_id": source_id,
            "target_agent_id": target_id,
            "connection_type": connection_type,
            "config": config,
        }]
    monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)


class TestResolveContextScope:
    @pytest.mark.asyncio
    async def test_no_connection_returns_inherit(self, monkeypatch):
        """source não tem nenhuma edge → inherit puro."""
        from app.agents import engine as eng

        async def fake_find_all(**_):
            return []
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b", last_output="hello", last_final_state="",
        )
        assert out["mode"] == "inherit"
        assert out["output"] == "hello"
        assert out["skip_prefix"] is False
        assert out["chars_before"] == 5
        assert out["chars_after"] == 5

    @pytest.mark.asyncio
    async def test_config_without_context_scope_returns_inherit(self, monkeypatch):
        """Conexão existe mas config.context_scope ausente → inherit."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({"expr": "true"}))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b", last_output="hello", last_final_state="",
        )
        assert out["mode"] == "inherit"
        assert out["output"] == "hello"
        assert out["skip_prefix"] is False

    @pytest.mark.asyncio
    async def test_mode_inherit_explicit_returns_output_cru(self, monkeypatch):
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "inherit"},
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="conteudo completo aqui", last_final_state="Recommend",
        )
        assert out["mode"] == "inherit"
        assert out["output"] == "conteudo completo aqui"
        assert out["skip_prefix"] is False

    @pytest.mark.asyncio
    async def test_mode_isolated_returns_empty_and_skip_prefix(self, monkeypatch, caplog):
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "isolated"},
        }))

        with caplog.at_level(logging.INFO, logger="app.agents.engine"):
            out = await eng._resolve_context_scope(
                source_id="a", target_id="b",
                last_output="conteudo confidencial", last_final_state="",
            )

        assert out["mode"] == "isolated"
        assert out["output"] == ""
        assert out["skip_prefix"] is True
        assert out["chars_before"] == 21
        assert out["chars_after"] == 0
        # Telemetria emite info pra observability
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.context_scope" in events

    @pytest.mark.asyncio
    async def test_mode_scoped_with_template_transforms_output(self, monkeypatch):
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "scoped", "template": "output | upper"},
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="hello world", last_final_state="",
        )
        assert out["mode"] == "scoped"
        assert out["output"] == "HELLO WORLD"
        assert out["skip_prefix"] is False
        assert out["chars_after"] == 11

    @pytest.mark.asyncio
    async def test_mode_scoped_with_max_chars_truncates(self, monkeypatch):
        """Atalho UX: max_chars=N sem template ⇢ vira template `output[:N]`."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "scoped", "max_chars": 10},
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="a" * 100, last_final_state="",
        )
        assert out["mode"] == "scoped"
        assert out["output"] == "a" * 10
        assert out["chars_before"] == 100
        assert out["chars_after"] == 10

    @pytest.mark.asyncio
    async def test_mode_scoped_template_takes_precedence_over_max_chars(self, monkeypatch):
        """Se ambos `template` e `max_chars` setados, `template` ganha."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {
                "mode": "scoped",
                "template": "output | upper",
                "max_chars": 5,
            },
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="hello", last_final_state="",
        )
        # Se max_chars vencesse, output seria "hello" truncado em 5 = "hello".
        # Como template ganha → "HELLO" (5 chars, mas em uppercase).
        assert out["output"] == "HELLO"

    @pytest.mark.asyncio
    async def test_mode_scoped_without_template_or_max_chars_falls_back_to_inherit(self, monkeypatch):
        """scoped sem template nem max_chars = operador escolheu mas não
        definiu a regra → fail-open pra inherit, sem warning ruidoso."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "scoped"},
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="hello", last_final_state="",
        )
        assert out["mode"] == "inherit"
        assert out["output"] == "hello"

    @pytest.mark.asyncio
    async def test_invalid_mode_falls_back_to_inherit_and_logs(self, monkeypatch, caplog):
        """Mode desconhecido → warning + inherit."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "nuke-everything"},
        }))

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._resolve_context_scope(
                source_id="a", target_id="b",
                last_output="hello", last_final_state="",
            )

        assert out["mode"] == "inherit"
        assert out["output"] == "hello"
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.context_scope" in events

    @pytest.mark.asyncio
    async def test_malformed_config_fails_open_and_logs(self, monkeypatch, caplog):
        """Config JSON inválido → fail-open + warning."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config="{ not valid json")

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._resolve_context_scope(
                source_id="a", target_id="b",
                last_output="hello", last_final_state="",
            )

        assert out["mode"] == "inherit"
        assert out["output"] == "hello"
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.context_scope" in events

    @pytest.mark.asyncio
    async def test_template_eval_error_fails_open_and_logs_with_exc_info(self, monkeypatch, caplog):
        """Template Jinja inválido em runtime → fail-open + warning com
        exc_info (vai pro errors.log conforme feedback-error-logging)."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "scoped", "template": "syntax !!!"},
        }))

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._resolve_context_scope(
                source_id="a", target_id="b",
                last_output="hello", last_final_state="",
            )

        assert out["mode"] == "inherit"
        assert out["output"] == "hello"
        rec = next(
            r for r in caplog.records
            if getattr(r, "event", None) == "mesh.context_scope"
            and "eval_failed" in r.getMessage()
        )
        # exc_info preenchido → JsonFormatter serializa traceback no errors.log
        assert rec.exc_info is not None
        assert getattr(rec, "template", None) == "syntax !!!"

    @pytest.mark.asyncio
    async def test_repo_lookup_error_fails_open_and_logs_error(self, monkeypatch, caplog):
        """Se mesh_repo.find_all levanta, retorna inherit + logger.error
        (não warning) — repo down é problema mais grave."""
        from app.agents import engine as eng

        async def boom(**_):
            raise RuntimeError("db connection lost")
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", boom)

        with caplog.at_level(logging.ERROR, logger="app.agents.engine"):
            out = await eng._resolve_context_scope(
                source_id="a", target_id="b",
                last_output="hello", last_final_state="",
            )

        assert out["mode"] == "inherit"
        assert out["output"] == "hello"
        rec = next(
            r for r in caplog.records
            if getattr(r, "event", None) == "mesh.context_scope"
            and r.levelno >= logging.ERROR
        )
        assert rec.exc_info is not None

    @pytest.mark.asyncio
    async def test_scoped_passes_final_state_to_template_context(self, monkeypatch):
        """`final_state` deve estar disponível no Jinja do template — útil
        para "se anterior foi Refuse, passar só 'Refused'"."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {
                "mode": "scoped",
                "template": "'REFUSED' if final_state == 'Refuse' else output",
            },
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="texto longo aqui", last_final_state="Refuse",
        )
        assert out["output"] == "REFUSED"

    @pytest.mark.asyncio
    async def test_empty_last_output_safe(self, monkeypatch):
        """last_output vazio não explode em nenhum modo."""
        from app.agents import engine as eng
        _patch_mesh(monkeypatch, config=json.dumps({
            "context_scope": {"mode": "scoped", "max_chars": 100},
        }))

        out = await eng._resolve_context_scope(
            source_id="a", target_id="b",
            last_output="", last_final_state="",
        )
        assert out["chars_before"] == 0
        assert out["chars_after"] == 0
        assert out["output"] == ""


# ─── Metadata sanity ────────────────────────────────────────────────


class TestContextScopeMetadata:
    def test_modes_tuple_complete(self):
        from app.agents.engine import CONTEXT_SCOPE_MODES
        assert set(CONTEXT_SCOPE_MODES) == {"inherit", "scoped", "isolated"}

    def test_vars_meta_reuses_conditional(self):
        """Mantemos um único conjunto de vars entre conditional e scope
        pra não fragmentar mental model do operador no wizard."""
        from app.agents.engine import (
            CONTEXT_SCOPE_VARS_META, CONDITIONAL_VARS_META,
        )
        assert CONTEXT_SCOPE_VARS_META is CONDITIONAL_VARS_META

    def test_vars_meta_has_required_fields(self):
        from app.agents.engine import CONTEXT_SCOPE_VARS_META
        assert len(CONTEXT_SCOPE_VARS_META) > 0
        for v in CONTEXT_SCOPE_VARS_META:
            assert "name" in v and "type" in v and "desc" in v


# ─── Integração: propagação do scope para `execute_interaction` ─────


class TestPipelinePropagation:
    """Smoke do bloco em `execute_pipeline` que aplica scope antes de
    montar `current_input` e `pipeline_context`.

    Não exercita o LLM — só valida que a lógica de propagação do scope
    está cabeada no lugar certo no engine."""

    def test_execute_pipeline_calls_resolve_context_scope(self):
        """Garante que a função foi cabeada no loop do pipeline — guard
        contra regressão de "removeram a chamada por engano"."""
        from pathlib import Path
        src_path = Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"
        src = src_path.read_text(encoding="utf-8")
        # Chamada estática que sabemos existir no bloco do pipeline
        assert "await _resolve_context_scope(" in src
        # E o evento de telemetria que emitimos pro stream
        assert "\"context_scope_applied\"" in src

    def test_isolated_path_bypasses_prefix(self):
        """Verifica que o caminho `skip_prefix=True` realmente faz o
        `current_input` virar só `user_input` no source."""
        from pathlib import Path
        src_path = Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py"
        src = src_path.read_text(encoding="utf-8")
        # Bloco em execute_pipeline: quando skip_prefix → current_input = user_input
        assert "scope_resolution[\"skip_prefix\"]" in src
        assert "current_input = user_input" in src
