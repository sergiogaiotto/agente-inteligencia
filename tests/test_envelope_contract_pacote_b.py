"""Pacote B (35.0.0) — âncora do envelope do execute_pipeline + rename do parallel.

Decisões do dono (2026-07-13):
- B1+B2: `final_state`/`transitions` do envelope vinham de steps[-1] (que em
  fan-out 1-de-N pode ser um step PULADO) enquanto `output` vinha do último
  COMPLETADO — a resposta era de um agente e o "estado final" de outro. Agora
  tudo ancora no MESMO step que produziu o output (owner_step) e o envelope
  declara QUEM respondeu (`output_agent`, campo aditivo).
- B4: `evidence_score` deixou de ser o MAX da cadeia (inflava confiança quando
  quem respondeu não citou evidência) — agora é o do dono do output.
- B3: 'parallel' continua o VALOR da API (glossário: value em inglês), mas o
  rótulo pt-BR virou "Sempre dispara" com descrição honesta (execução é serial).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ─── helpers (mesmo padrão de test_mesh_skip_propagation) ───────────

def _agent(aid: str, name: str) -> dict:
    return {
        "id": aid, "name": name, "status": "active", "kind": "subagent",
        "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "prompt real",
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


def _patch_executions(monkeypatch, evidence_by_agent: dict | None = None):
    """Cada agente devolve output/estado próprios; evidence_score configurável
    por agente (p/ provar a semântica B4: dono ≠ MAX)."""
    ev = evidence_by_agent or {}

    async def fake_exec(*, agent_id, user_input, channel="api", attachments=None,
                        pipeline_context=None, session_id=None, **_):
        return {
            "output": f"output-of-{agent_id}",
            "final_state": f"State-{agent_id}",
            "interaction_id": None,
            "duration_ms": 1,
            "evidence_score": ev.get(agent_id, 0),
            "transitions": [{"from": "Start", "to": f"State-{agent_id}"}],
            "trace": {},
        }
    monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)


def _statuses(res: dict) -> dict:
    return {s["agent_id"]: s["status"] for s in res["pipeline_steps"]}


# ─── B1+B2+B4: âncora do envelope ────────────────────────────────────

class TestEnvelopeAncoradoNoDonoDoOutput:
    @pytest.mark.asyncio
    async def test_fanout_com_ultimo_no_pulado(self, monkeypatch):
        """O cenário do bug: T roteia 1-de-N; A casa, B (ÚLTIMO da cadeia) é
        pulado. Antes: output de A com final_state/transitions do step pulado
        B e evidence_score = MAX da cadeia (o do router T). Agora: tudo do A."""
        import app.agents.engine as eng
        _patch_agents(monkeypatch, {x: _agent(x, f"Nome-{x}") for x in ("T", "A", "B")})
        _patch_topology(monkeypatch, {
            "T": [
                _conn("T", "A", ctype="conditional", expr="'investir' in input_lower"),
                _conn("T", "B", ctype="conditional", expr="'suporte' in input_lower"),
            ],
        })
        # T (router) cita 5 evidências; A (quem responde) cita 0.2 → MAX mentia.
        _patch_executions(monkeypatch, evidence_by_agent={"T": 0.9, "A": 0.2})

        res = await eng.execute_pipeline(entry_agent_id="T", user_input="quero investir")

        assert _statuses(res) == {"T": "completed", "A": "completed", "B": "skipped_conditional"}
        assert res["pipeline_steps"][-1]["agent_id"] == "B"  # o último É o pulado
        # âncora: decisão/transições/evidência do MESMO agente do output
        assert res["output"] == "output-of-A"
        assert res["final_state"] == "State-A"
        assert res["transitions"] == [{"from": "Start", "to": "State-A"}]
        assert res["evidence_score"] == 0.2  # do dono (antes: 0.9 = MAX do router)
        assert res["output_agent"] == {"id": "A", "name": "Nome-A"}

    @pytest.mark.asyncio
    async def test_cadeia_linear_coerente(self, monkeypatch):
        import app.agents.engine as eng
        _patch_agents(monkeypatch, {x: _agent(x, f"Nome-{x}") for x in ("T", "A")})
        _patch_topology(monkeypatch, {"T": [_conn("T", "A")]})
        _patch_executions(monkeypatch)
        res = await eng.execute_pipeline(entry_agent_id="T", user_input="oi")
        assert res["output"] == "output-of-A"
        assert res["final_state"] == "State-A"
        assert res["output_agent"] == {"id": "A", "name": "Nome-A"}

    @pytest.mark.asyncio
    async def test_output_agent_sempre_aponta_o_step_do_output(self, monkeypatch):
        """Invariante do contrato novo: o step referido por output_agent tem
        EXATAMENTE o output do envelope."""
        import app.agents.engine as eng
        _patch_agents(monkeypatch, {x: _agent(x, f"Nome-{x}") for x in ("T", "A", "B")})
        _patch_topology(monkeypatch, {
            "T": [
                _conn("T", "A", ctype="conditional", expr="'x' in input_lower"),
                _conn("T", "B", ctype="default"),
            ],
        })
        _patch_executions(monkeypatch)
        res = await eng.execute_pipeline(entry_agent_id="T", user_input="pergunta sem a letra proibida")
        owner = res["output_agent"]["id"]
        owner_step = next(s for s in res["pipeline_steps"] if s["agent_id"] == owner)
        assert owner_step["output"] == res["output"]
        assert owner_step["final_state"] == res["final_state"]


class TestOutputAgentChegaNaAPI:
    """Review (major): o campo aditivo não pode morrer na fronteira rota/projeção
    — os payloads do invoke são ALLOWLISTS que rebuildam o envelope."""

    def test_payloads_das_rotas_propagam(self):
        rotas = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        # sync + stream (2 payloads allowlistados)
        assert rotas.count('"output_agent": result.get("output_agent")') == 1
        assert rotas.count('"output_agent": res.get("output_agent")') == 1
        jobs = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert '"output_agent": r.get("output_agent")' in jobs  # worker do 202

    def test_projecao_summary_expoe_e_minimal_omite(self):
        from app.agents.result_view import project_pipeline_result
        payload = {"pipeline_id": "p1", "status": "completed", "output": "resp",
                   "output_agent": {"id": "A", "name": "Nome-A"},
                   "final_state": "Done", "interaction_id": "i1",
                   "total_agents": 2, "completed_agents": 2,
                   "pipeline_steps": [], "duration_ms": 10}
        full = project_pipeline_result(payload, "full")
        assert full["output_agent"] == {"id": "A", "name": "Nome-A"}
        summary = project_pipeline_result(payload, "summary")
        assert summary["output_agent"] == {"id": "A", "name": "Nome-A"}
        minimal = project_pipeline_result(payload, "minimal")
        assert "output_agent" not in minimal  # o contrato mínimo segue mínimo

    @pytest.mark.asyncio
    async def test_fallback_sem_produtor_nao_inventa_autoria(self, monkeypatch):
        """Review: cadeia SEM produtor real (entry falha) não pode atribuir a
        resposta-fallback a um agente que nunca respondeu."""
        import app.agents.engine as eng
        _patch_agents(monkeypatch, {"T": _agent("T", "Nome-T")})
        _patch_topology(monkeypatch, {})

        async def boom(**_):
            raise RuntimeError("provider indisponível")
        monkeypatch.setattr("app.agents.engine.execute_interaction", boom)
        res = await eng.execute_pipeline(entry_agent_id="T", user_input="oi")
        assert res["output_agent"] is None
        assert res["output"] == "Pipeline sem resultado"


# ─── B3: rename do parallel (rótulo pt-BR; valor da API intacto) ─────

class TestParallelRename:
    def test_mesh_flow_sem_rotulo_paralela(self):
        html = Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")
        # case-insensitive (review: 'Paralela' capitalizada fora de aspas escapava)
        assert "paralela" not in html.lower()
        assert "Sempre dispara" in html
        # o VALOR da API/JS continua 'parallel' (glossário: value em inglês)
        assert "id: 'parallel'" in html
        # a descrição não promete simultaneidade
        assert "ao mesmo tempo" not in html

    def test_mesh_api_help_e_tour_renomeados(self):
        """Review: o rename tinha escapado em GET /api/v1/mesh/connection-types
        (fonte do popover '?' do wizard) e no passo do Tour."""
        mesh_src = Path("app/routes/mesh.py").read_text(encoding="utf-8")
        assert '"label": "Paralelo"' not in mesh_src
        assert '"label": "Sempre dispara"' in mesh_src
        base_src = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")
        assert "paralelos/fan-out" not in base_src

    def test_help_e_guide_honestos(self):
        help_src = Path("app/static/js/help-content.js").read_text(encoding="utf-8")
        guide_src = Path("app/static/js/module-guide.js").read_text(encoding="utf-8")
        assert "Sempre dispara (fan-out)" in help_src
        assert "Paralela (fan-out)" not in help_src
        assert "<b>Sempre dispara</b>" in guide_src
        assert "<b>Paralelo</b>" not in guide_src

    def test_runtime_intocado(self):
        """B3 é SÓ rótulo: o engine segue tratando parallel == sequential
        (decisão: gather real fica p/ feature futura opt-in)."""
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert 'in ("sequential", "parallel")' in src


# ─── B5: override por citação de nome documentado ────────────────────

class TestB5NamingOverrideDocumentado:
    def test_help_explica_reativacao_por_nome(self):
        help_src = Path("app/static/js/help-content.js").read_text(encoding="utf-8")
        assert "Citar o NOME do agente na resposta roteia" in help_src
        assert "mesh.conditional" in help_src
