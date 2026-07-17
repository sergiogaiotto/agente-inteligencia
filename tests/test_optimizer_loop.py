"""Loop reflexivo GEPA-style (49.0.0, PR4b — fecha o arco Otimização).

Cobre:
1. LÓGICA PURA (o coração): pareto_front (não-dominados, lições
   complementares), select_parent (determinístico rotacionado), should_stop
   (max_rounds / budget / early-stop por paciência), passes_from_details.
2. Orquestração run_optimization com TUDO mockado: semeia champion, propõe
   filhos, atualiza Pareto, confirma no holdout, salva melhor como revisão,
   report-only (nunca chama agents_repo.update).
3. Job durável: dispatch/cap, claim atômico, timeout, boot-resume + kill-switch.
4. Rotas: 403 com toggle OFF, 202 com ON, 404s, GET de status.

Mocks nos módulos — sem DB/LLM reais, convenção da suíte.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.optimizer.jobs as ojobs
import app.optimizer.loop as loop
import app.routes.optimizer as opt
from app.optimizer.loop import (
    pareto_front, passes_from_details, select_parent, should_stop,
)


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ═══ 1. Lógica pura ══════════════════════════════════════════════════════

class TestParetoFront:
    def test_domina_por_superconjunto_estrito(self):
        cands = [{"id": "A", "passes": {"c1", "c2", "c3"}},
                 {"id": "B", "passes": {"c1", "c2"}}]
        assert pareto_front(cands) == ["A"]  # B ⊂ A → dominado

    def test_preserva_licoes_complementares(self):
        # A bom nos casos de roteamento, B bom nos de extração — ambos ficam
        cands = [{"id": "A", "passes": {"r1", "r2"}},
                 {"id": "B", "passes": {"e1", "e2"}}]
        assert set(pareto_front(cands)) == {"A", "B"}

    def test_empate_de_passes_mantem_ambos(self):
        cands = [{"id": "A", "passes": {"c1"}}, {"id": "B", "passes": {"c1"}}]
        assert set(pareto_front(cands)) == {"A", "B"}

    def test_lista_de_passes_aceita(self):
        cands = [{"id": "A", "passes": ["c1", "c2"]},
                 {"id": "B", "passes": ["c1"]}]
        assert pareto_front(cands) == ["A"]


class TestSelectParent:
    def _cands(self):
        return [{"id": "A", "score": 0.9}, {"id": "B", "score": 0.7},
                {"id": "C", "score": 0.5}]

    def test_ordena_por_score_e_rotaciona(self):
        c = self._cands()
        front = ["A", "B", "C"]
        # round 0 → melhor (A); round 1 → B; round 2 → C; round 3 → A de novo
        assert select_parent(c, front, 0)["id"] == "A"
        assert select_parent(c, front, 1)["id"] == "B"
        assert select_parent(c, front, 2)["id"] == "C"
        assert select_parent(c, front, 3)["id"] == "A"

    def test_front_vazia_none(self):
        assert select_parent(self._cands(), [], 0) is None


class TestShouldStop:
    def _h(self, *v):
        return list(v)

    def test_max_rounds(self):
        stop, why = should_stop(rounds_done=4, max_rounds=4,
                                best_score_history=self._h(0.1, 0.2, 0.3),
                                patience=2, budget_usd=0, spent_usd=0)
        assert stop and "max_rounds" in why

    def test_budget(self):
        stop, why = should_stop(rounds_done=1, max_rounds=9,
                                best_score_history=self._h(0.1),
                                patience=2, budget_usd=5.0, spent_usd=5.0)
        assert stop and "budget" in why

    def test_early_stop_sem_melhora(self):
        # 3 rodadas sem melhora além da paciência (2)
        stop, why = should_stop(rounds_done=3, max_rounds=9,
                                best_score_history=self._h(0.8, 0.8, 0.8),
                                patience=2, budget_usd=0, spent_usd=0)
        assert stop and "early-stop" in why

    def test_continua_com_melhora(self):
        stop, _ = should_stop(rounds_done=2, max_rounds=9,
                              best_score_history=self._h(0.5, 0.6, 0.7),
                              patience=2, budget_usd=0, spent_usd=0)
        assert stop is False

    def test_passes_from_details(self):
        det = [{"case_id": "c1", "passed": True},
               {"case_id": "c2", "passed": False},
               {"case_id": "c3", "passed": True}]
        assert passes_from_details(det) == {"c1", "c3"}


# ═══ 2. Orquestração (mockada) ═══════════════════════════════════════════

def _wire_loop(monkeypatch, *, max_rounds=1, children=1, child_score=1.0,
               control_score=0.6, holdout_verdict="b_melhor",
               holdout_ids=("h1",)):
    """Wiring compartilhado da orquestração run_optimization (mocks)."""
    opt_row = {"id": "opt1", "agent_id": "a1", "release_id": "r1",
               "gold_version": "latest", "owner_user_id": "u1",
               "max_rounds": max_rounds, "children_per_round": children,
               "budget_usd": 0.0}
    state = {"updates": [], "cands": [], "rev": {}}

    monkeypatch.setattr(loop, "_load_run", lambda oid: _async(dict(opt_row))())
    monkeypatch.setattr(loop, "_update_run",
                        lambda oid, f: state["updates"].append(f) or _async(None)())

    async def _insert(opt_id, **kw):
        cid = f"oc{len(state['cands'])}"
        state["cands"].append({"id": cid, **kw})
        return cid

    monkeypatch.setattr(loop, "_insert_candidate", _insert)
    monkeypatch.setattr(loop, "_mark_pareto", _async(None))
    monkeypatch.setattr(loop, "_captured_failures", _async([]))

    calls = {"n": 0}

    async def _run_variant(*, opt, system_prompt, case_ids, caller_id):
        calls["n"] += 1
        if system_prompt is None:
            p, s = {"c1", "c2"}, 0.5
        elif system_prompt == "ctrl":
            p, s = {"c1", "c2", "c3"}, control_score
        else:
            p, s = {"c1", "c2", "c3", "c4"}, child_score
        return {"eval_id": f"ev{calls['n']}", "passes": p, "score": s, "cost": 0.1}

    monkeypatch.setattr(loop, "_run_variant_on_train", _run_variant)
    monkeypatch.setattr(loop, "_run_variant_on_train_holdout",
                        _async({"cost": 0.05, "details": []}))
    monkeypatch.setattr("app.core.database.agents_repo.find_by_id",
                        _async({"id": "a1", "name": "Ag",
                                "system_prompt": "champ prompt",
                                "llm_provider": "azure", "model": "gpt-4o"}))
    upd_agent = AsyncMock()
    monkeypatch.setattr("app.core.database.agents_repo.update", upd_agent)
    gold = [{"id": f"c{i}", "split": "train"} for i in range(1, 5)]
    gold += [{"id": h, "split": "holdout"} for h in holdout_ids]
    monkeypatch.setattr("app.core.database.gold_cases_repo.find_all", _async(gold))
    monkeypatch.setattr("app.routes.optimizer._agent_skill_sections", _async(None))
    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task",
                        _async(("azure", "gpt-opt")))
    monkeypatch.setattr("app.optimizer.proposer.summarize_gold",
                        lambda cs: {"total": len(cs), "exemplos_de_entrada": []})
    monkeypatch.setattr("app.optimizer.proposer.build_proposer_messages",
                        lambda **kw: [{"role": "user", "content": "x"}])
    monkeypatch.setattr(loop, "_propose",
                        _async(('{"system_prompt": "melhor prompt", '
                                '"rationale": "encurtei"}', "azure", "m", 0.02)))
    monkeypatch.setattr("app.optimizer.proposer.parse_proposer_response",
                        lambda c: {"system_prompt": "melhor prompt",
                                   "rationale": "encurtei"})
    monkeypatch.setattr("app.optimizer.proposer.variant_leaks_gold",
                        lambda *a, **k: False)
    monkeypatch.setattr("app.optimizer.proposer.build_control_variant",
                        lambda a, s: {"system_prompt": "ctrl", "rationale": "c"})
    import app.core.revisions as revisions

    async def _rec(**kw):
        state["rev"].update(kw)
        return "rev_opt"

    monkeypatch.setattr(revisions, "safe_record", _rec)
    monkeypatch.setattr("app.routes.dashboard._paired_comparison",
                        lambda a, b: {"verdict": holdout_verdict,
                                      "verdict_note": "n", "truncated": False})
    state["upd_agent"] = upd_agent
    return state


class TestRunOptimization:
    @pytest.mark.asyncio
    async def test_holdout_confirma_salva_e_reporta_melhor(self, monkeypatch):
        st = _wire_loop(monkeypatch, holdout_verdict="b_melhor")
        await loop.run_optimization("opt1", deadline_s=60.0)
        final = st["updates"][-1]
        res = json.loads(final["result"])
        assert final["status"] == "completed"
        assert res["improved"] is True and res["holdout_verdict"] == "b_melhor"
        assert res["model"] == "azure/gpt-4o"  # Model Drifting no selo
        assert st["rev"]["source"] == "optimizer_candidate"
        assert '"model"' in st["rev"]["note"]  # modelo registrado
        st["upd_agent"].assert_not_awaited()  # report-only

    @pytest.mark.asyncio
    async def test_holdout_desconfirma_nao_salva_revisao(self, monkeypatch):
        """Review [7]: ganho no treino mas holdout NÃO confirma → improved
        False e NENHUMA revisão gravada (mais rígido que aceitar treino)."""
        st = _wire_loop(monkeypatch, holdout_verdict="inconclusivo")
        await loop.run_optimization("opt1", deadline_s=60.0)
        final = st["updates"][-1]
        res = json.loads(final["result"])
        assert res["train_improved"] is True  # treino melhorou
        assert res["improved"] is False       # mas holdout não confirmou
        assert final["best_revision_id"] is None
        assert st["rev"] == {}  # nada salvo

    @pytest.mark.asyncio
    async def test_sem_ganho_no_treino_holdout_nao_roda(self, monkeypatch):
        """Review [4]: sem ganho no treino, holdout_verdict distingue de
        'sem_holdout' e o holdout NÃO é rodado."""
        # filho E controle PIORES que o champion (0.5) → nenhum ganho
        st = _wire_loop(monkeypatch, child_score=0.4, control_score=0.3)
        await loop.run_optimization("opt1", deadline_s=60.0)
        res = json.loads(st["updates"][-1]["result"])
        assert res["holdout_verdict"] == "nao_confirmado_sem_ganho"
        assert res["improved"] is False

    @pytest.mark.asyncio
    async def test_multi_rodada_early_stop(self, monkeypatch):
        """Review [16]/[17]: várias rodadas SEM melhora → early-stop por
        paciência (o filho sempre 0.7, nunca supera após a 1ª)."""
        st = _wire_loop(monkeypatch, max_rounds=9, children=1, child_score=0.7)
        monkeypatch.setattr("app.core.config.get_settings",
                            lambda: SimpleNamespace(optimizer_patience=2))
        await loop.run_optimization("opt1", deadline_s=60.0)
        res = json.loads(st["updates"][-1]["result"])
        # parou por early-stop MUITO antes das 9 rodadas
        assert res["rounds"] < 9 and "early-stop" in res["stop_reason"]

    @pytest.mark.asyncio
    async def test_loop_report_only_com_ganho(self, monkeypatch):
        opt_row = {
            "id": "opt1", "agent_id": "a1", "release_id": "r1",
            "gold_version": "latest", "owner_user_id": "u1",
            "max_rounds": 1, "children_per_round": 1, "budget_usd": 0.0,
        }
        updates = []

        async def _load_run(oid):
            return dict(opt_row)

        async def _update_run(oid, fields):
            updates.append(fields)

        monkeypatch.setattr(loop, "_load_run", _load_run)
        monkeypatch.setattr(loop, "_update_run", _update_run)
        cand_rows = []

        async def _insert(opt_id, **kw):
            cid = f"oc{len(cand_rows)}"
            cand_rows.append({"id": cid, **kw})
            return cid

        monkeypatch.setattr(loop, "_insert_candidate", _insert)
        monkeypatch.setattr(loop, "_mark_pareto", _async(None))
        monkeypatch.setattr(loop, "_captured_failures", _async([]))

        # champion 2/4; filho 4/4 (melhor); controle 3/4 (distinto → 'melhor
        # prompt' é inequivocamente o melhor).
        calls = {"n": 0}

        async def _run_variant(*, opt, system_prompt, case_ids, caller_id):
            if system_prompt is None:
                p, s = {"c1", "c2"}, 0.5
            elif system_prompt == "ctrl":
                p, s = {"c1", "c2", "c3"}, 0.75
            else:
                p, s = {"c1", "c2", "c3", "c4"}, 1.0
            calls["n"] += 1
            return {"eval_id": f"ev{calls['n']}", "passes": p, "score": s,
                    "cost": 0.1}

        monkeypatch.setattr(loop, "_run_variant_on_train", _run_variant)
        monkeypatch.setattr(loop, "_run_variant_on_train_holdout",
                            _async({"cost": 0.05, "details": []}))
        # loop.py importa os repos LAZY (dentro da função) → patch na FONTE
        upd_agent = AsyncMock()
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id",
                            _async({"id": "a1", "name": "Ag",
                                    "system_prompt": "champ prompt"}))
        monkeypatch.setattr("app.core.database.agents_repo.update", upd_agent)
        monkeypatch.setattr("app.core.database.gold_cases_repo.find_all", _async([
            {"id": "c1", "split": "train"}, {"id": "c2", "split": "train"},
            {"id": "c3", "split": "train"}, {"id": "c4", "split": "train"},
            {"id": "h1", "split": "holdout"}]))
        monkeypatch.setattr("app.routes.optimizer._agent_skill_sections",
                            _async(None))
        monkeypatch.setattr("app.llm_routing.resolve_llm_for_task",
                            _async(("azure", "gpt-opt")))
        monkeypatch.setattr("app.optimizer.proposer.summarize_gold",
                            lambda cs: {"total": len(cs)})
        monkeypatch.setattr("app.optimizer.proposer.build_proposer_messages",
                            lambda **kw: [{"role": "user", "content": "x"}])
        # _propose retorna 4-tupla (content, provider, model, cost) — o custo
        # volta ao acumulador do loop (review [2]).
        monkeypatch.setattr(loop, "_propose",
                            _async(('{"system_prompt": "melhor prompt", '
                                    '"rationale": "encurtei"}', "azure", "m", 0.02)))
        monkeypatch.setattr("app.optimizer.proposer.parse_proposer_response",
                            lambda c: {"system_prompt": "melhor prompt",
                                       "rationale": "encurtei"})
        monkeypatch.setattr("app.optimizer.proposer.variant_leaks_gold",
                            lambda *a, **k: False)
        monkeypatch.setattr("app.optimizer.proposer.build_control_variant",
                            lambda a, s: {"system_prompt": "ctrl",
                                          "rationale": "controle"})
        import app.core.revisions as revisions
        rev_saved = {}

        async def _rec(**kw):
            rev_saved.update(kw)
            return "rev_opt"

        monkeypatch.setattr(revisions, "safe_record", _rec)
        # paired: holdout confirma b_melhor
        monkeypatch.setattr("app.routes.dashboard._paired_comparison",
                            lambda a, b: {"verdict": "b_melhor",
                                          "verdict_note": "sig"})

        await loop.run_optimization("opt1", deadline_s=60.0)

        final = updates[-1]
        assert final["status"] == "completed"
        assert final["best_candidate_id"] and final["best_revision_id"] == "rev_opt"
        res = json.loads(final["result"])
        assert res["improved"] is True
        assert res["best_score"] == 1.0 and res["champion_score"] == 0.5
        assert res["holdout_verdict"] == "b_melhor"
        # a melhor variante virou revisão restaurável (report-only)
        assert rev_saved["source"] == "optimizer_candidate"
        assert rev_saved["content"] == "melhor prompt"
        upd_agent.assert_not_awaited()  # NUNCA aplica ao agente

    @pytest.mark.asyncio
    async def test_agente_sumido_marca_failed(self, monkeypatch):
        monkeypatch.setattr(loop, "_load_run",
                            _async({"id": "o", "agent_id": "a", "release_id": "r",
                                    "owner_user_id": "u"}))
        marks = []
        monkeypatch.setattr(loop, "_update_run",
                            lambda oid, f: marks.append(f) or _async(None)())
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id",
                            _async(None))
        monkeypatch.setattr("app.core.database.gold_cases_repo.find_all",
                            _async([]))
        monkeypatch.setattr("app.routes.optimizer._agent_skill_sections",
                            _async(None))
        await loop.run_optimization("o", deadline_s=10.0)
        assert any(m.get("status") == "failed" for m in marks)


# ═══ 3. Job durável ══════════════════════════════════════════════════════

class _Con:
    def __init__(self, row=None, rows=None, exec_result="UPDATE 1"):
        self.row = row
        self.rows = rows or []
        self.exec_result = exec_result
        self.fetch_sqls = []
        self.executed = []

    async def fetchrow(self, sql, *a):
        return self.row

    async def fetch(self, sql, *a):
        self.fetch_sqls.append(sql)
        return self.rows

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return self.exec_result


class _Pool:
    def __init__(self, con):
        self._con = con

    def acquire(self):
        con = self._con

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *e):
                return False
        return _Ctx()


class TestJob:
    @pytest.mark.asyncio
    async def test_dispatch_cap_e_dedup(self, monkeypatch):
        ojobs._reset_for_tests()
        monkeypatch.setattr(ojobs, "_max_concurrent", lambda: 1)
        import asyncio
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(oid):
            started.set()
            await release.wait()

        monkeypatch.setattr(ojobs, "_run_job", slow)
        assert ojobs.dispatch("o1") is True
        assert ojobs.dispatch("o1") is False
        assert ojobs.dispatch("o2") is False
        await started.wait()
        release.set()
        await asyncio.sleep(0)
        ojobs._reset_for_tests()

    @pytest.mark.asyncio
    async def test_claim_noop_sem_linha(self, monkeypatch):
        monkeypatch.setattr(ojobs, "_pool", lambda: _Pool(_Con(row=None)))
        ro = AsyncMock()
        monkeypatch.setattr("app.optimizer.loop.run_optimization", ro)
        await ojobs._run_job("ghost")
        ro.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_job_claima_e_roda(self, monkeypatch):
        monkeypatch.setattr(ojobs, "_pool", lambda: _Pool(_Con(row={"id": "o1"})))
        monkeypatch.setattr(ojobs, "_timeout_seconds", lambda: 60.0)
        ro = AsyncMock()
        monkeypatch.setattr("app.optimizer.loop.run_optimization", ro)
        await ojobs._run_job("o1")
        ro.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boot_resume_orfao_interrupted_killswitch(self, monkeypatch):
        con = _Con(rows=[], exec_result="UPDATE 2")
        monkeypatch.setattr(ojobs, "_pool", lambda: _Pool(con))
        monkeypatch.setattr(ojobs, "_enabled", lambda: False)
        out = await ojobs.resume_on_boot()
        assert out["interrupted"] == 2 and out["dispatched"] == 0
        assert con.fetch_sqls == []  # kill-switch: fila não consultada

    @pytest.mark.asyncio
    async def test_sweep_cura_zumbi(self, monkeypatch):
        ojobs._reset_for_tests()
        con = _Con(rows=[{"id": "z1"}])
        monkeypatch.setattr(ojobs, "_pool", lambda: _Pool(con))
        monkeypatch.setattr(ojobs, "_enabled", lambda: False)
        out = await ojobs.sweep_queued()
        assert out["interrupted"] == 1


# ═══ 4. Rotas ════════════════════════════════════════════════════════════

def _client():
    app = FastAPI()
    app.include_router(opt.router)
    return TestClient(app, raise_server_exceptions=False)


def _wire_route(monkeypatch, *, enabled=True, train=6):
    monkeypatch.setattr(opt, "require_role",
                        lambda *r: _async({"id": "u1", "role": "admin"}))
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: SimpleNamespace(
                            optimizer_loop_enabled=enabled,
                            optimizer_max_rounds=4,
                            optimizer_default_budget_usd=0.0))
    monkeypatch.setattr(opt.agents_repo, "find_by_id",
                        _async({"id": "a1", "name": "Ag", "skill_id": None}))
    monkeypatch.setattr(opt.releases_repo, "find_by_id", _async({"id": "r1"}))
    monkeypatch.setattr(opt, "_agent_skill_sections", _async(None))
    monkeypatch.setattr(opt.gold_cases_repo, "find_all", _async(
        [{"id": f"c{i}", "split": "train"} for i in range(train)]))


_BODY = {"agent_id": "a1", "release_id": "r1", "gold_version": "latest"}


class TestRotaOptimize:
    def test_403_toggle_off(self, monkeypatch):
        _wire_route(monkeypatch, enabled=False)
        r = _client().post("/api/v1/optimizer/optimize", json=_BODY)
        assert r.status_code == 403 and "desligado" in r.json()["detail"]

    def test_422_poucos_casos_treino(self, monkeypatch):
        _wire_route(monkeypatch, train=3)
        r = _client().post("/api/v1/optimizer/optimize", json=_BODY)
        assert r.status_code == 422 and "TREINO" in r.json()["detail"]

    def test_202_enfileira_e_dispatcha(self, monkeypatch):
        _wire_route(monkeypatch)
        con = _Con()
        monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
        dispatched = []
        monkeypatch.setattr("app.optimizer.jobs.dispatch",
                            lambda oid: dispatched.append(oid) or True)
        r = _client().post("/api/v1/optimizer/optimize", json=_BODY)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "queued" and body["optimization_id"]
        assert dispatched == [body["optimization_id"]]

    def test_404_agente(self, monkeypatch):
        _wire_route(monkeypatch)
        monkeypatch.setattr(opt.agents_repo, "find_by_id", _async(None))
        r = _client().post("/api/v1/optimizer/optimize", json=_BODY)
        assert r.status_code == 404

    def test_get_status_com_arvore(self, monkeypatch):
        # GET gated root/admin (review [14])
        monkeypatch.setattr(opt, "require_role",
                            lambda *r: _async({"id": "u1", "role": "admin"}))
        con = _Con(
            row={"id": "opt1", "status": "completed", "result": '{"best_score":1}'},
            rows=[{"id": "oc1", "round": 1, "parent_candidate_id": None,
                   "kind": "llm", "eval_id": "e1", "score": 0.9,
                   "on_pareto": True, "reflection": "r", "prompt_chars": 100}])
        monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
        r = _client().get("/api/v1/optimizer/optimize/opt1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run"]["result"]["best_score"] == 1
        assert body["candidates"][0]["id"] == "oc1"


def test_template_card_do_loop():
    from pathlib import Path
    src = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")
    assert 'data-testid="optimizer-loop-card"' in src
    assert 'data-testid="optimizer-loop-start"' in src
    assert "optimizerLoop()" in src
    assert "/api/v1/optimizer/optimize" in src
