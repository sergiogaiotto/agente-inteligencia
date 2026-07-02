"""reasoning_effort — esforço de raciocínio por agente, ponta a ponta.

Auditoria (2026-06-27): o card "Reasoning" do wizard só ROTEAVA o modelo; não
existia nenhum reasoning_effort sendo setado/enviado ao LLM. Esta feature adiciona
o campo (low|medium|high) no agente e o repassa aos providers da família OpenAI
(gpt-oss, Azure, OpenAI público) via model_kwargs. Inclui guarda de temperature
p/ modelos reasoning-only (o1/o3) que rejeitam temperature != 1.

Cobre: helpers puros, threading no get_provider, validação de schema, fiação no
harness e a UI do wizard (varredura de template).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.llm_providers import (
    _is_reasoning_only_model,
    _openai_chat_kwargs,
    get_provider,
)
from app.models.schemas import AgentCreate, AgentUpdate


# ───────────────── helpers puros ─────────────────

class TestReasoningOnlyModel:
    @pytest.mark.parametrize("m", ["o1", "o1-preview", "o1-mini", "o3", "o3-mini", "o4-mini", "O1-Preview"])
    def test_reasoning_only(self, m):
        assert _is_reasoning_only_model(m) is True

    @pytest.mark.parametrize("m", ["gpt-oss-120b", "gpt-4o", "gpt-4o-mini", "", None, "maritaca"])
    def test_aceita_temperature(self, m):
        assert _is_reasoning_only_model(m) is False


class TestOpenAIChatKwargs:
    def test_modelo_normal_com_effort(self):
        kw = _openai_chat_kwargs(0.7, "gpt-oss-120b", "high")
        assert kw == {"temperature": 0.7, "model_kwargs": {"reasoning_effort": "high"}}

    def test_modelo_normal_sem_effort(self):
        kw = _openai_chat_kwargs(0.7, "gpt-oss-120b", None)
        assert kw == {"temperature": 0.7}
        assert "model_kwargs" not in kw

    def test_reasoning_only_forca_temperature_1(self):
        # o1/o3/o4 só aceitam temperature=1; OMITIR não basta (langchain manda 0.7
        # default no body de qualquer jeito) → forçamos 1.0.
        kw = _openai_chat_kwargs(0.5, "o1-preview", "high")
        assert kw["temperature"] == 1.0
        assert kw["model_kwargs"] == {"reasoning_effort": "high"}

    def test_reasoning_only_sem_effort(self):
        assert _openai_chat_kwargs(0.5, "o1-mini", None) == {"temperature": 1.0}


# ───────────────── threading no get_provider ─────────────────

class TestGetProviderThreading:
    def test_gpt_oss_recebe_effort(self):
        p = get_provider("gpt-oss-120b", model="x", temperature=0.7, reasoning_effort="high")
        assert p.reasoning_effort == "high"

    def test_azure_modelo_reasoning_recebe_effort(self):
        # Gate por MODELO (2026-07-02): na Azure/OpenAI só a família de
        # raciocínio (o1/o3/o4/gpt-5) aceita reasoning_effort. Mandar para
        # gpt-4o dava 400 "Unrecognized request argument" — e derrubava a
        # cadeia de resiliência justamente no fallback.
        p = get_provider("azure", model="o3-mini", reasoning_effort="medium")
        assert p.reasoning_effort == "medium"

    def test_azure_gpt4o_descarta_effort(self):
        p = get_provider("azure", model="gpt-4o", reasoning_effort="medium")
        assert p.reasoning_effort is None

    def test_ollama_nao_quebra_com_effort(self):
        # ollama NÃO é família OpenAI → get_provider faz pop e NÃO repassa o kwarg,
        # senão OllamaProvider.__init__ (que não conhece o param) daria TypeError.
        p = get_provider("ollama", reasoning_effort="high")
        assert not hasattr(p, "reasoning_effort")


# ───────────────── validação de schema ─────────────────

class TestSchemaValidation:
    def test_normaliza_e_valida(self):
        assert AgentCreate(name="xx", reasoning_effort="HIGH").reasoning_effort == "high"
        assert AgentCreate(name="xx", reasoning_effort="  low ").reasoning_effort == "low"
        assert AgentCreate(name="xx", reasoning_effort="").reasoning_effort is None
        assert AgentCreate(name="xx").reasoning_effort is None
        assert AgentUpdate(reasoning_effort="medium").reasoning_effort == "medium"

    def test_invalido_rejeitado(self):
        with pytest.raises(ValidationError):
            AgentCreate(name="xx", reasoning_effort="turbo")
        with pytest.raises(ValidationError):
            AgentUpdate(reasoning_effort="máximo")


# ───────────────── fiação no harness (engine → get_provider) ─────────────────

class TestHarnessWiring:
    def test_harness_passa_effort_ao_provider(self, monkeypatch):
        import app.agents.engine as engine
        captured = {}
        def fake_get_provider(name, **kw):
            captured["name"] = name
            captured.update(kw)
            return object()
        monkeypatch.setattr(engine, "get_provider", fake_get_provider)
        engine.DeepAgentHarness({
            "llm_provider": "gpt-oss-120b", "model": "m",
            "temperature": 0.5, "reasoning_effort": "high",
        })
        assert captured["reasoning_effort"] == "high"
        assert captured["temperature"] == 0.5

    def test_effort_vazio_vira_none(self, monkeypatch):
        import app.agents.engine as engine
        captured = {}
        monkeypatch.setattr(engine, "get_provider", lambda name, **kw: captured.update(kw) or object())
        engine.DeepAgentHarness({"llm_provider": "gpt-oss-120b", "model": "m", "reasoning_effort": ""})
        assert captured["reasoning_effort"] is None


# ───────────────── UI do wizard (varredura de template) ─────────────────

class TestUpdateClearToNull:
    """PUT com reasoning_effort=null (wizard "Padrão do modelo") DEVE limpar o valor
    salvo — senão o filtro `if v is not None` da rota dropa o null (footgun null-drop)."""

    def test_explicit_null_marca_field_set(self):
        assert "reasoning_effort" in AgentUpdate(reasoning_effort=None).model_fields_set
        assert "reasoning_effort" not in AgentUpdate(name="x").model_fields_set

    def test_rota_limpa_para_null(self, monkeypatch):
        import app.routes.agents as ar
        import app.agents.preflight as pf
        from types import SimpleNamespace
        captured = {}

        async def fake_find(aid):
            return {"id": aid, "name": "A", "reasoning_effort": "high", "task_type": None}
        async def fake_update(aid, upd):
            captured["upd"] = upd; return {}
        async def fake_audit(row):
            return {}
        async def fake_preflight(payload):
            return SimpleNamespace(blocked=False, model_dump=lambda: {})

        monkeypatch.setattr(ar.agents_repo, "find_by_id", fake_find)
        monkeypatch.setattr(ar.agents_repo, "update", fake_update)
        monkeypatch.setattr(ar.audit_repo, "create", fake_audit)
        monkeypatch.setattr(pf, "run_preflight", fake_preflight)

        app = FastAPI(); app.include_router(ar.router)
        r = TestClient(app, raise_server_exceptions=False).put(
            "/api/v1/agents/a1", json={"reasoning_effort": None})
        assert r.status_code == 200, r.text
        # a re-inclusão por model_fields_set fez o null chegar no UPDATE → limpa a coluna
        assert "reasoning_effort" in captured["upd"]
        assert captured["upd"]["reasoning_effort"] is None


def test_wizard_tem_controle_de_reasoning_effort():
    from pathlib import Path
    src = Path("app/templates/pages/agent_form.html").read_text(encoding="utf-8")
    # select ligado ao form, visível só p/ task_type=reasoning, com as 3 opções
    assert 'x-model="form.reasoning_effort"' in src
    assert "form.task_type === 'reasoning'" in src
    for opt in ('value="low"', 'value="medium"', 'value="high"'):
        assert opt in src
    # default no form + envio no payload de save (gated: só envia p/ task_type=reasoning,
    # senão null — evita valor órfão indo ao LLM se o usuário trocar de task_type)
    assert "reasoning_effort: ''" in src
    assert "this.form.task_type === 'reasoning' ? ((this.form.reasoning_effort" in src
