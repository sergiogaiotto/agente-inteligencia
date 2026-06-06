"""Grounded-by-default — princípio anti-conhecimento-paramétrico (2026-06-06).

Pedido do operador (verbatim): "Globalmente o conhecimento do modelo NUNCA deve
ser usado, a não ser que seja solicitado CLARAMENTE."

Implementado em 3 camadas + escape hatch:
- Layer A: diretiva estrita injetada no system prompt (_build_grounding_directive
  / _build_grounding_closing), suprimida quando o agente tem o escape hatch.
- Layer B: guarda no VerifyEvidence (_grounding_guard) — sem nenhuma fonte de
  fundamentação (RAG / anexo / output de tool / contexto de pipeline) a resposta
  só viria do modelo → recusa controlada em vez de alucinação.
- Layer C (PR B, separado): roteamento ciente de anexo.
- Toggle global: settings.grounding_strict (default True). Escape hatch por
  agente: allow_general_knowledge (default False).

Convenção do projeto (cf. test_runtime_llm_fallback.py / test_conversation_memory.py):
NÃO chamamos execute_interaction inteiro (pesado, depende de DB+LLM) — testamos
as peças isoladas (funções puras + repos mockáveis). A integração da guarda no
FSM é exercida no smoke manual / homolog.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.agents import engine
from app.agents.engine import (
    GROUNDING_REFUSAL_REASON,
    _build_grounding_closing,
    _build_grounding_directive,
    _grounding_guard,
    _has_tool_grounding,
)


# ═══════════════════════════════════════════════════════════════════
# _grounding_guard — coração da decisão (função pura)
# ═══════════════════════════════════════════════════════════════════
class TestGroundingGuard:
    def test_recusa_sem_nenhuma_fonte(self):
        """strict + sem escape hatch + nenhuma evidência → RECUSA."""
        refuse, reason = _grounding_guard(
            strict=True, allow_general_knowledge=False,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
        )
        assert refuse is True
        assert reason == GROUNDING_REFUSAL_REASON

    @pytest.mark.parametrize("source", [
        "has_evidences", "has_attachments", "has_pipeline_context", "has_tool_output",
    ])
    def test_qualquer_fonte_libera(self, source):
        """UMA fonte de fundamentação já basta para NÃO recusar."""
        kwargs = dict(
            strict=True, allow_general_knowledge=False,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
        )
        kwargs[source] = True
        refuse, reason = _grounding_guard(**kwargs)
        assert refuse is False
        assert reason == ""

    def test_strict_off_nunca_recusa(self):
        """grounding_strict desligado → comportamento legado (não recusa)."""
        refuse, reason = _grounding_guard(
            strict=False, allow_general_knowledge=False,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
        )
        assert refuse is False
        assert reason == ""

    def test_escape_hatch_libera(self):
        """allow_general_knowledge (solicitado CLARAMENTE) → não recusa mesmo strict."""
        refuse, reason = _grounding_guard(
            strict=True, allow_general_knowledge=True,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
        )
        assert refuse is False
        assert reason == ""

    def test_draft_de_erro_e_poupado(self):
        """Draft que já é mensagem de erro do sistema (⚠) não é recusado de novo."""
        refuse, _ = _grounding_guard(
            strict=True, allow_general_knowledge=False,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
            draft="⚠ Erro ao chamar LLM (gpt-4o): Connection error.",
        )
        assert refuse is False

    def test_draft_de_erro_com_espaco_inicial(self):
        """lstrip antes do match do ⚠ (draft pode vir com whitespace)."""
        refuse, _ = _grounding_guard(
            strict=True, allow_general_knowledge=False,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
            draft="   ⚠ algo deu errado",
        )
        assert refuse is False

    def test_draft_normal_sem_evidencia_recusa(self):
        """Draft de resposta normal (sem ⚠) + sem fonte → ainda recusa."""
        refuse, _ = _grounding_guard(
            strict=True, allow_general_knowledge=False,
            has_evidences=False, has_attachments=False,
            has_pipeline_context=False, has_tool_output=False,
            draft="A capital da França é Paris.",  # conhecimento paramétrico clássico
        )
        assert refuse is True


# ═══════════════════════════════════════════════════════════════════
# Layer A — builders de diretiva (funções puras)
# ═══════════════════════════════════════════════════════════════════
class TestGroundingDirective:
    def test_directive_proibe_conhecimento_parametrico(self):
        d = _build_grounding_directive()
        assert "EXCLUSIVAMENTE" in d
        assert "PROIBIDO" in d
        assert "conhecimento geral ou paramétrico" in d
        # instrui a declarar ausência em vez de inventar
        assert "NÃO invente" in d

    def test_directive_cita_as_tres_fontes(self):
        d = _build_grounding_directive()
        assert "anexados" in d                 # documentos anexados
        assert "RAG" in d                       # base de conhecimento
        assert "ferramentas" in d               # MCP/APIs

    def test_closing_reforca_fundamentacao(self):
        c = _build_grounding_closing()
        assert "LEMBRETE FINAL" in c
        assert "cada afirmação deve derivar de uma evidência" in c

    def test_refusal_reason_e_acionavel(self):
        """A recusa precisa dizer COMO destravar (anexo / RAG / tool / escape hatch)."""
        r = GROUNDING_REFUSAL_REASON
        assert "Não há evidências" in r
        assert "Anexe um documento" in r
        assert "base de conhecimento" in r
        assert "ferramenta" in r
        assert "Permitir conhecimento geral" in r


# ═══════════════════════════════════════════════════════════════════
# _has_tool_grounding — reconhece fundamentação só-por-tool (async, repos mockados)
# ═══════════════════════════════════════════════════════════════════
def _patch_repo(monkeypatch, repo: str, rows, *, raise_exc=None):
    async def fake_find_all(interaction_id=None, limit=1, **_):
        if raise_exc is not None:
            raise raise_exc
        return rows
    monkeypatch.setattr(f"app.core.database.{repo}.find_all", fake_find_all)


class TestHasToolGrounding:
    @pytest.mark.asyncio
    async def test_sem_interaction_id_e_false(self):
        assert await _has_tool_grounding(None) is False
        assert await _has_tool_grounding("") is False

    @pytest.mark.asyncio
    async def test_tool_call_presente_e_true(self, monkeypatch):
        _patch_repo(monkeypatch, "tool_calls_repo", [{"id": "tc1"}])
        _patch_repo(monkeypatch, "binding_executions_repo", [])
        assert await _has_tool_grounding("int-1") is True

    @pytest.mark.asyncio
    async def test_binding_execution_presente_e_true(self, monkeypatch):
        """Sem tool_call MCP, mas com execução de binding (API declarativa) → True."""
        _patch_repo(monkeypatch, "tool_calls_repo", [])
        _patch_repo(monkeypatch, "binding_executions_repo", [{"id": "be1"}])
        assert await _has_tool_grounding("int-1") is True

    @pytest.mark.asyncio
    async def test_nenhuma_invocacao_e_false(self, monkeypatch):
        _patch_repo(monkeypatch, "tool_calls_repo", [])
        _patch_repo(monkeypatch, "binding_executions_repo", [])
        assert await _has_tool_grounding("int-1") is False

    @pytest.mark.asyncio
    async def test_erro_de_query_e_fail_safe_false(self, monkeypatch):
        """Erro nos dois repos → False (a guarda erra para o lado SEGURO: recusar)."""
        _patch_repo(monkeypatch, "tool_calls_repo", None, raise_exc=RuntimeError("db down"))
        _patch_repo(monkeypatch, "binding_executions_repo", None, raise_exc=RuntimeError("db down"))
        assert await _has_tool_grounding("int-1") is False


# ═══════════════════════════════════════════════════════════════════
# Setting global grounding_strict (config)
# ═══════════════════════════════════════════════════════════════════
@pytest.fixture
def fresh_settings():
    from app.core import config as _config
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


class TestGroundingStrictSetting:
    def test_default_declarado_e_true(self):
        """Default da CLASSE é True (grounded-by-default), imune a env do ambiente."""
        from app.core.config import Settings
        assert Settings.model_fields["grounding_strict"].default is True

    def test_ui_to_env_map_tem_a_chave(self):
        from app.core import config as _config
        assert _config._UI_TO_ENV_MAP.get("grounding_strict") == "GROUNDING_STRICT"

    def test_env_false_desliga(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("GROUNDING_STRICT", "false")
        from app.core.config import get_settings
        assert get_settings().grounding_strict is False

    def test_env_true_liga(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("GROUNDING_STRICT", "true")
        from app.core.config import get_settings
        assert get_settings().grounding_strict is True


# ═══════════════════════════════════════════════════════════════════
# Escape hatch por agente — allow_general_knowledge (schema + persistência)
# ═══════════════════════════════════════════════════════════════════
class TestAllowGeneralKnowledgeSchema:
    def test_agent_create_default_false(self):
        from app.models.schemas import AgentCreate
        a = AgentCreate(name="Agente Teste", kind="subagent")
        assert a.allow_general_knowledge is False

    def test_agent_create_aceita_true(self):
        from app.models.schemas import AgentCreate
        a = AgentCreate(name="Agente Teste", kind="subagent", allow_general_knowledge=True)
        assert a.allow_general_knowledge is True

    def test_agent_update_default_none(self):
        """Update parcial: None = "não mexe" (preserva valor atual)."""
        from app.models.schemas import AgentUpdate
        assert AgentUpdate().allow_general_knowledge is None

    def test_agent_update_aceita_bool(self):
        from app.models.schemas import AgentUpdate
        assert AgentUpdate(allow_general_knowledge=True).allow_general_knowledge is True
        assert AgentUpdate(allow_general_knowledge=False).allow_general_knowledge is False

    def test_bool_fields_inclui_allow_general_knowledge(self):
        """Coerção bool→int(0/1) na coluna INTEGER legada (create + update)."""
        from app.routes.agents import _BOOL_FIELDS
        assert "allow_general_knowledge" in _BOOL_FIELDS


class TestSettingsSaveSchema:
    def test_settings_save_aceita_grounding_strict(self):
        from app.routes.dashboard import SettingsSave
        assert SettingsSave(grounding_strict=False).grounding_strict is False

    def test_settings_save_default_true(self):
        from app.routes.dashboard import SettingsSave
        assert SettingsSave().grounding_strict is True


# ═══════════════════════════════════════════════════════════════════
# DB — coluna + migração idempotente
# ═══════════════════════════════════════════════════════════════════
class TestDatabaseColumn:
    def test_create_table_tem_coluna(self):
        src = Path("app/core/database.py").read_text(encoding="utf-8")
        assert "allow_general_knowledge INTEGER DEFAULT 0" in src

    def test_migracao_alter_idempotente(self):
        src = Path("app/core/database.py").read_text(encoding="utf-8")
        assert (
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
            "allow_general_knowledge INTEGER DEFAULT 0" in src
        )


# ═══════════════════════════════════════════════════════════════════
# Wiring no engine — assinatura, observabilidade e pins de reprodutibilidade
# ═══════════════════════════════════════════════════════════════════
class TestEngineWiring:
    def test_execute_interaction_tem_param_grounding_strict(self):
        sig = inspect.signature(engine.execute_interaction)
        assert "grounding_strict" in sig.parameters
        # default None = lê settings.grounding_strict
        assert sig.parameters["grounding_strict"].default is None

    def test_trace_expoe_grounding(self):
        """_build_result publica metadata['grounding'] na rastreabilidade (audit/UI)."""
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert '"grounding": ctx.metadata.get("grounding")' in src

    def test_layer_a_injeta_diretiva_no_prompt(self):
        """O system prompt builder chama o builder da diretiva (Layer A)."""
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert "_build_grounding_directive()" in src
        assert "_build_grounding_closing()" in src

    def test_evaluator_fixa_grounding_strict_false(self):
        """Golden dataset = reprodutibilidade: guarda NÃO se aplica (calibrado antes)."""
        src = Path("app/harness/evaluator.py").read_text(encoding="utf-8")
        assert "grounding_strict=False" in src

    def test_executor_fixa_grounding_strict_false(self):
        """Replay de recipe = determinístico: guarda NÃO se aplica."""
        src = Path("app/catalog/executor.py").read_text(encoding="utf-8")
        assert "grounding_strict=False" in src


# ═══════════════════════════════════════════════════════════════════
# Smoke de template — checkbox global (settings) + por-agente (agent_form)
# ═══════════════════════════════════════════════════════════════════
class TestTemplateSmoke:
    def test_settings_tem_checkbox_grounding_strict(self):
        content = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert 'x-model="config.grounding_strict"' in content
        assert "Grounded by Default" in content
        # estado inicial + coerção de bool no load (string "false" → False)
        assert "grounding_strict: true" in content
        assert "_bools=['grounding_strict']" in content

    def test_agent_form_tem_checkbox_allow_general_knowledge(self):
        content = Path("app/templates/pages/agent_form.html").read_text(encoding="utf-8")
        assert 'x-model="form.allow_general_knowledge"' in content
        # default no estado + load do GET + payload do submit
        assert "allow_general_knowledge: false" in content
        assert "this.form.allow_general_knowledge = !!a.allow_general_knowledge" in content
        assert "allow_general_knowledge: !!this.form.allow_general_knowledge" in content

    def test_areas_novas_sem_roxo(self):
        """Guard local de paleta: minhas adições não usam violet/fuchsia/purple."""
        pat = re.compile(r"\b(violet|fuchsia|purple)-\d")
        for f in (
            "app/templates/pages/settings.html",
            "app/templates/pages/agent_form.html",
        ):
            assert not pat.search(Path(f).read_text(encoding="utf-8")), f"roxo em {f}"
