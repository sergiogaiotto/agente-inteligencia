"""'Conhecer o agente' — endpoint POST /api/v1/agents/{id}/explain.

Assistente que EXPLICA o agente a partir da sua definição (config + SKILL.md +
posição no mesh + diagnóstico agregado). Invariante central: **NUNCA executa** o
agente — sem `execute_interaction`, sem criar interação, sem gastar o orçamento
dele. Testado chamando a função da rota direto (sem HTTP), com deps injetadas.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routes.agents as agents_mod

FAKE_AGENT = {
    "id": "a1", "name": "Especialista Faturamento", "kind": "subagent",
    "domain": "cometa", "llm_provider": "gpt-oss-120b", "model": "openai/gpt-oss-120b",
    "task_type": "reasoning", "temperature": 0.4, "require_evidence": 1,
    "allow_general_knowledge": 0, "system_prompt": "Você é o especialista de faturamento.",
    "skill_id": "s1", "status": "active", "version": "1.0.0",
}
FAKE_SKILL = {"id": "s1", "raw_content": "# Faturamento\n## Purpose\nSegunda via da fatura.\n## Tool Bindings\nNenhuma."}


def _req(api_key_id=None):
    return SimpleNamespace(state=SimpleNamespace(api_key_id=api_key_id))


@pytest.fixture
def wired(monkeypatch):
    calls = {"wizard": 0, "exec": 0, "int_create": 0, "messages": None}

    async def find_agent(_id):
        return FAKE_AGENT if _id == "a1" else None

    async def find_skill(_id):
        return FAKE_SKILL if _id == "s1" else None

    async def mesh_find_all(**kw):
        return []

    async def fake_diag(aid):
        return {"performance": {"interactions_last_30d": 3}, "health": {"score": 90}}

    async def fake_resolve(task):
        return ("azure", "gpt-4o")

    async def fake_wizard(messages, provider, model, **kw):
        calls["wizard"] += 1
        calls["messages"] = messages
        return ("Este é um Especialista de faturamento — e ele NÃO é executado aqui.", provider, model)

    async def fake_exec(*a, **k):
        calls["exec"] += 1
        raise AssertionError("explain NUNCA deve executar o agente")

    async def fake_int_create(*a, **k):
        calls["int_create"] += 1
        raise AssertionError("explain NUNCA deve criar interação do agente")

    import app.core.database as db
    import app.llm_routing as lr
    import app.routes.wizard as wz
    import app.agents.engine as eng

    monkeypatch.setattr(agents_mod.agents_repo, "find_by_id", find_agent)
    monkeypatch.setattr(agents_mod.skills_repo, "find_by_id", find_skill)
    monkeypatch.setattr(db.mesh_repo, "find_all", mesh_find_all)
    monkeypatch.setattr(agents_mod, "agent_diagnostics", fake_diag)
    monkeypatch.setattr(lr, "resolve_llm_for_task", fake_resolve)
    monkeypatch.setattr(wz, "_wizard_llm_complete", fake_wizard)
    monkeypatch.setattr(eng, "execute_interaction", fake_exec)
    monkeypatch.setattr(agents_mod.interactions_repo, "create", fake_int_create)
    return calls


@pytest.mark.asyncio
async def test_responde_sem_executar_o_agente(wired):
    r = await agents_mod.explain_agent("a1", {"message": "o que você faz?"}, _req(), {"id": "u1"})
    assert r.get("answer")
    assert r["agent_id"] == "a1"
    assert wired["wizard"] == 1
    # INVARIANTE: nada de execução / interação / orçamento do agente
    assert wired["exec"] == 0
    assert wired["int_create"] == 0


@pytest.mark.asyncio
async def test_ficha_tem_skill_config_e_persona_anti_execucao(wired):
    await agents_mod.explain_agent("a1", {"message": "me explica"}, _req(), {"id": "u1"})
    sys_msg = wired["messages"][0]["content"]
    assert "## Purpose" in sys_msg                     # SKILL.md completa na ficha
    assert "Especialista" in sys_msg                    # papel traduzido (glossário)
    assert "Exigir evidência (RAG): sim" in sys_msg     # config
    assert "NÃO o executa" in sys_msg                   # persona proíbe agir como o agente


@pytest.mark.asyncio
async def test_multi_turn_usa_history(wired):
    await agents_mod.explain_agent(
        "a1",
        {"message": "e a diferença pro outro?",
         "history": [{"role": "user", "content": "o que você faz?"},
                     {"role": "assistant", "content": "Segunda via."}]},
        _req(), {"id": "u1"},
    )
    roles = [m["role"] for m in wired["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


@pytest.mark.asyncio
async def test_ui_only_bloqueia_x_api_key(wired):
    with pytest.raises(HTTPException) as ei:
        await agents_mod.explain_agent("a1", {"message": "oi"}, _req(api_key_id="k1"), {"id": "u1"})
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_mensagem_vazia_devolve_erro(wired):
    r = await agents_mod.explain_agent("a1", {"message": "   "}, _req(), {"id": "u1"})
    assert "error" in r
    assert wired["wizard"] == 0     # não chama LLM à toa


@pytest.mark.asyncio
async def test_agente_inexistente_404(wired):
    with pytest.raises(HTTPException) as ei:
        await agents_mod.explain_agent("nope", {"message": "oi"}, _req(), {"id": "u1"})
    assert ei.value.status_code == 404
