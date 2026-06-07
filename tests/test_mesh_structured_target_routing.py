"""Roteamento por TARGET ESTRUTURADO no AI Mesh (Fase B — 2026-06-07).

CONTEXTO / bug que motivou (sessão CEP→Tavium, 2026-06-07 19:46):
O roteador (Fase B #316) passou a emitir um bloco estruturado
``{"target": "Busca endereço", "inputs": {"cep": "13211740"}}`` e o SA passou a
consumi-lo (#315 via `_extract_inputs_from_text`). MAS o GATE condicional
(`_should_skip_conditional`) — que decide se cada SA downstream roda, e roda
ANTES de o SA processar — só entendia roteamento em LINGUAGEM NATURAL
(`_output_routes_to_target`: verbo/seta/"agente" antes do nome). A chave JSON
``"target"`` não é um cue de NL → o override "o roteador mandou" não disparava;
caía na `expr` de keyword, que não casava o CEP nu ("13211740") → os 3 irmãos
(Busca endereço, Tavily, FAQ) eram TODOS pulados (1/4 executados, só o roteador).

Fix (caminho definitivo, não o patch de regex): o bloco ``{"target": X}`` é um
sinal de roteamento de PRIMEIRA CLASSE — DETERMINÍSTICO e EXCLUSIVO:
  • X casa este alvo  → roda (ignora a expr de keyword);
  • X nomeia OUTRO    → pula este (o roteador elegeu UM — 1-de-N real).
Inerte quando não há bloco → preserva 100% do roteamento por expr/NL-cue legado.

Estes testes provam:
1. parser `_extract_routed_target` (fenced / inline / sem target / não-JSON).
2. caso CEP: target estruturado faz o SA nomeado RODAR e os irmãos PULAREM,
   mesmo com a expr de keyword deles dando false (a repro do bug).
3. casamento de nome case/acento-insensível.
4. sem bloco estruturado, o roteamento por expr segue idêntico (override inerte).
"""
from __future__ import annotations

import json

import pytest


# ─── helpers de mock (espelham tests/test_mesh_fanout_routing.py) ────


def _agent(aid: str, name: str) -> dict:
    return {
        "id": aid, "name": name, "status": "active", "kind": "subagent",
        "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "prompt real",
    }


def _conn(src: str, tgt: str, *, ctype: str = "conditional", expr: str | None = None) -> dict:
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


def _patch_executions(monkeypatch, outputs_by_id: dict | None = None):
    """Captura cada execução. O roteador (entry) pode devolver um output
    CUSTOM (o bloco estruturado) via `outputs_by_id`; os demais devolvem
    'output-of-<id>'. interaction_id=None → pula consolidação de sessão."""
    outputs_by_id = outputs_by_id or {}
    invoked = []

    async def fake_exec(*, agent_id, user_input, channel="api", attachments=None,
                        pipeline_context=None, session_id=None, **_):
        invoked.append({"agent_id": agent_id, "user_input": user_input,
                        "pipeline_context": pipeline_context})
        return {
            "output": outputs_by_id.get(agent_id, f"output-of-{agent_id}"),
            "final_state": "Recommend", "interaction_id": None,
            "duration_ms": 1, "evidence_score": 0, "transitions": [], "trace": {},
        }
    monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)
    return invoked


def _statuses(res: dict) -> dict:
    return {s["agent_id"]: s["status"] for s in res["pipeline_steps"]}


# ─── parser _extract_routed_target / _norm_routing_name ──────────────


class TestExtractRoutedTarget:
    def test_inline_object(self):
        from app.agents import engine as eng
        out = '{"target": "Busca endereço", "inputs": {"cep": "13211740"}}'
        assert eng._extract_routed_target(out) == "Busca endereço"

    def test_fenced_json_block(self):
        from app.agents import engine as eng
        out = 'vou rotear:\n```json\n{"target": "Tavily", "inputs": {"q": "x"}}\n```\npronto'
        assert eng._extract_routed_target(out) == "Tavily"

    def test_no_target_key_returns_none(self):
        from app.agents import engine as eng
        # bloco só com inputs (sem target) → não é decisão de roteamento.
        assert eng._extract_routed_target('{"inputs": {"cep": "1"}}') is None

    def test_non_json_prose_returns_none(self):
        from app.agents import engine as eng
        assert eng._extract_routed_target("encaminhe ao agente Busca endereço") is None
        assert eng._extract_routed_target("") is None
        assert eng._extract_routed_target(None) is None

    def test_norm_routing_name_accent_case_insensitive(self):
        from app.agents import engine as eng
        assert eng._norm_routing_name("Busca endereço") == eng._norm_routing_name("busca endereco")
        assert eng._norm_routing_name("  Tavily ") == "tavily"


# ─── repro do bug CEP via execute_pipeline ───────────────────────────


class TestStructuredTargetRouting:
    @pytest.mark.asyncio
    async def test_cep_named_target_runs_siblings_skip(self, monkeypatch):
        """A REPRO. Roteador emite {"target":"Busca endereço",...}; os 3 irmãos
        têm expr de keyword que NÃO casa o CEP nu. Antes: todos pulados (bug).
        Agora: 'Busca endereço' roda (target estruturado), Tavily e FAQ pulam."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "BU": _agent("BU", "Buscador A"),
            "END": _agent("END", "Busca endereço"),
            "TAV": _agent("TAV", "Tavily"),
            "FAQ": _agent("FAQ", "FAQ Claro"),
        })
        _patch_topology(monkeypatch, {"BU": [
            _conn("BU", "END", expr="'cep' in input_lower"),
            _conn("BU", "TAV", expr="'turismo' in input_lower"),
            _conn("BU", "FAQ", expr="'loja' in input_lower"),
        ]})
        router_out = '{"target": "Busca endereço", "inputs": {"cep": "13211740"}}'
        invoked = _patch_executions(monkeypatch, {"BU": router_out})

        res = await eng.execute_pipeline(entry_agent_id="BU", user_input="13211740")

        ran = [x["agent_id"] for x in invoked]
        st = _statuses(res)
        assert "END" in ran, "roteador emitiu target=Busca endereço → deve rodar"
        assert "TAV" not in ran and st["TAV"] == "skipped_conditional"
        assert "FAQ" not in ran and st["FAQ"] == "skipped_conditional"

    @pytest.mark.asyncio
    async def test_target_match_is_accent_insensitive(self, monkeypatch):
        """Roteador emite o target SEM acento ('busca endereco'); ainda casa o
        SA 'Busca endereço'. Robustez do casamento de nome."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "BU": _agent("BU", "Buscador A"),
            "END": _agent("END", "Busca endereço"),
            "TAV": _agent("TAV", "Tavily"),
        })
        _patch_topology(monkeypatch, {"BU": [
            _conn("BU", "END", expr="'cep' in input_lower"),
            _conn("BU", "TAV", expr="'turismo' in input_lower"),
        ]})
        router_out = '{"target": "busca endereco", "inputs": {"cep": "13211740"}}'
        invoked = _patch_executions(monkeypatch, {"BU": router_out})

        res = await eng.execute_pipeline(entry_agent_id="BU", user_input="13211740")

        ran = [x["agent_id"] for x in invoked]
        assert "END" in ran
        assert "TAV" not in ran and _statuses(res)["TAV"] == "skipped_conditional"

    @pytest.mark.asyncio
    async def test_no_structured_block_falls_back_to_expr(self, monkeypatch):
        """Sem bloco {target} no output do roteador, o override é INERTE: o
        roteamento por expr de keyword segue idêntico (END casa 'cep', TAV não)."""
        from app.agents import engine as eng
        _patch_agents(monkeypatch, {
            "BU": _agent("BU", "Buscador A"),
            "END": _agent("END", "Busca endereço"),
            "TAV": _agent("TAV", "Tavily"),
        })
        _patch_topology(monkeypatch, {"BU": [
            _conn("BU", "END", expr="'cep' in input_lower"),
            _conn("BU", "TAV", expr="'turismo' in input_lower"),
        ]})
        # roteador devolve PROSA (sem bloco estruturado) → cai na expr.
        invoked = _patch_executions(monkeypatch, {"BU": "ok, encaminhando"})

        res = await eng.execute_pipeline(entry_agent_id="BU", user_input="meu cep é tal")

        ran = [x["agent_id"] for x in invoked]
        assert "END" in ran, "expr 'cep' casa o input → END roda (legado preservado)"
        assert "TAV" not in ran and _statuses(res)["TAV"] == "skipped_conditional"
