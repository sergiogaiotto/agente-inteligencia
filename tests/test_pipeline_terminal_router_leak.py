"""Router TERMINAL não vaza a linha DECISAO crua ao usuário (#5, QA 2026-07-19).

REPRO do bug (VPS, domínio Balaio): quando o pipeline termina no ROUTER — fora
de escopo, `default→ancestral` é no-op porque o engine não re-visita nó já
executado —, a resposta apresentada ao usuário era o output CRU do classificador:
literalmente ``DECISAO: categoria=fora_de_escopo; risco=normal``.

Causa: `strip_decision_line` preserva a linha de propósito quando ela é o texto
INTEIRO (para nunca esvaziar a UI). Fix (camada de apresentação): quando o
produtor terminal emitiu SÓ o protocolo (`is_decision_only`), a superfície
final devolve uma recusa amigável — a decisão estruturada segue no envelope
`decision` (via de máquina).

Estes testes dirigem o `execute_pipeline` REAL (LLM mockado via
`execute_interaction`), não os helpers isolados.
"""
from __future__ import annotations

import json

import pytest

# Skill do router com contrato `## Decisions` (categoria + risco) — é o que faz
# `is_decision_only` reconhecer a linha como protocolo válido.
_SKILL_ROUTER = """# Triagem
## Purpose
Classifica e roteia.
## Decisions
```json
{ "categoria": ["comprador", "lojista", "antifraude", "fora_de_escopo"], "risco": ["normal", "alto"] }
```
"""

_RAW_DECISION = "DECISAO: categoria=fora_de_escopo; risco=normal"


def _agent(aid: str, name: str, *, kind: str = "router", skill_id: str = "skR") -> dict:
    return {
        "id": aid, "name": name, "status": "active", "kind": kind,
        "model": "gpt-4o", "skill_id": skill_id, "system_prompt": "prompt real",
    }


def _conn(src: str, tgt: str, *, ctype: str = "sequential", expr: str | None = None) -> dict:
    cfg = json.dumps({"expr": expr}) if expr is not None else "{}"
    return {
        "source_agent_id": src, "target_agent_id": tgt,
        "connection_type": ctype, "config": cfg,
    }


def _patch_topology(monkeypatch, conns_by_source: dict):
    async def fake_find_all(source_agent_id=None, limit=20, **_):
        return list(conns_by_source.get(source_agent_id, []))
    monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)


def _patch_agents(monkeypatch, agents: dict):
    async def fake_find_by_id(aid):
        return agents.get(aid)
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_find_by_id)


def _patch_skill(monkeypatch, raw_content: str):
    async def fake_skill(_id):
        return {"id": _id, "raw_content": raw_content}
    from app.agents import engine as eng
    monkeypatch.setattr(eng.skills_repo, "find_by_id", fake_skill)


def _patch_executions(monkeypatch, outputs_by_id: dict):
    async def fake_exec(*, agent_id, user_input, channel="api", attachments=None,
                        pipeline_context=None, session_id=None, **_):
        return {
            "output": outputs_by_id.get(agent_id, f"output-of-{agent_id}"),
            "final_state": "Recommend", "interaction_id": None,
            "duration_ms": 1, "evidence_score": 0, "transitions": [], "trace": {},
        }
    monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)


@pytest.mark.asyncio
async def test_router_terminal_nao_vaza_linha_decisao(monkeypatch):
    """O caso reproduzido: router terminal cujo output é SÓ a linha DECISAO.
    A resposta apresentada NÃO pode conter a linha crua — vira recusa amigável;
    a decisão estruturada segue no envelope `decision`; o step CRU (auditoria)
    preserva a linha."""
    from app.agents import engine as eng
    _patch_agents(monkeypatch, {"TR": _agent("TR", "Triagem Balaio")})
    _patch_topology(monkeypatch, {"TR": []})  # terminal: sem downstream
    _patch_skill(monkeypatch, _SKILL_ROUTER)
    _patch_executions(monkeypatch, {"TR": _RAW_DECISION})

    res = await eng.execute_pipeline(entry_agent_id="TR", user_input="qual a capital da França?")

    # 1. NÃO vaza a linha-protocolo crua ao usuário.
    assert "DECISAO" not in res["output"]
    assert res["output"] == eng._TERMINAL_DECISION_FALLBACK
    # 2. A decisão estruturada é preservada (via de máquina, envelope).
    assert res["decision"] == {"categoria": "fora_de_escopo", "risco": "normal"}
    # 3. O step CRU preserva a linha (trace/auditoria).
    step = res["pipeline_steps"][-1]
    assert step["output"] == _RAW_DECISION
    # 4. O balão do step terminal (UI de pipeline) é consistente com o topo.
    assert step.get("output_display") == eng._TERMINAL_DECISION_FALLBACK


@pytest.mark.asyncio
async def test_router_terminal_com_prosa_estripa_so_a_linha(monkeypatch):
    """Contraste: router que ALÉM da linha produz prosa real → a prosa é a
    resposta (linha estripada), sem substituição por recusa."""
    from app.agents import engine as eng
    _patch_agents(monkeypatch, {"TR": _agent("TR", "Triagem Balaio")})
    _patch_topology(monkeypatch, {"TR": []})
    _patch_skill(monkeypatch, _SKILL_ROUTER)
    prosa = "Posso te ajudar com a devolução do produto."
    _patch_executions(monkeypatch, {"TR": f"{prosa}\n{_RAW_DECISION}"})

    res = await eng.execute_pipeline(entry_agent_id="TR", user_input="quero devolver")

    assert res["output"] == prosa
    assert "DECISAO" not in res["output"]
    assert res["decision"] == {"categoria": "fora_de_escopo", "risco": "normal"}


@pytest.mark.asyncio
async def test_router_intermediario_nao_vaza_no_balao(monkeypatch):
    """#3/#6: router INTERMEDIÁRIO (roteou adiante ao especialista) cujo output é
    só a linha DECISAO. O especialista é o owner e responde; o balão do router
    NÃO pode mostrar a linha crua → output_display="" (a UI suprime). Caso comum
    e in-scope, não edge de fora-de-escopo."""
    from app.agents import engine as eng
    _patch_agents(monkeypatch, {
        "TR": _agent("TR", "Triagem", kind="router", skill_id="skR"),
        "SP": _agent("SP", "Especialista", kind="subagent", skill_id=""),  # sem contrato
    })
    _patch_topology(monkeypatch, {"TR": [_conn("TR", "SP", ctype="sequential")]})
    _patch_skill(monkeypatch, _SKILL_ROUTER)  # serve o skR do TR
    prosa_sp = "Claro! Vou processar a sua devolução agora mesmo."
    _patch_executions(monkeypatch, {"TR": _RAW_DECISION, "SP": prosa_sp})

    res = await eng.execute_pipeline(entry_agent_id="TR", user_input="quero devolver o tênis")

    # 1. A resposta é do especialista (owner), sem linha crua.
    assert res["output"] == prosa_sp
    assert "DECISAO" not in res["output"]
    # 2. O balão do router intermediário é suprimido (output_display="") — nunca
    #    a linha crua.
    steps_by_id = {s["agent_id"]: s for s in res["pipeline_steps"]}
    tr_step = steps_by_id["TR"]
    assert tr_step.get("output_display") == ""
    assert "DECISAO" not in (tr_step.get("output_display") or "")
    # 3. O output CRU do router segue preservado no step (auditoria/trace).
    assert tr_step["output"] == _RAW_DECISION


@pytest.mark.asyncio
async def test_display_preview_decision_only_vira_vazio(monkeypatch):
    """#5: o preview de stream (agent_done.output_preview) de um router
    decision-only NÃO pode vazar a linha crua a consumidores SSE externos."""
    from app.agents import engine as eng
    _patch_agents(monkeypatch, {"TR": _agent("TR", "Triagem")})
    _patch_skill(monkeypatch, _SKILL_ROUTER)
    got = await eng._display_preview(_RAW_DECISION, "TR")
    assert got == ""
    assert "DECISAO" not in got
