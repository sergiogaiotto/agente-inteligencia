"""Promoção da variante vencedora (47.0.0, PR5 do arco Otimização).

Cobre POST /api/v1/optimizer/promote:
- Guards: gate root/admin; 404s; run_type/status/alvo; champion COM
  overrides (422); challenger SEM selo (422); skill_purpose ainda não
  promovível (422); gold_version/gold_hash divergentes (409).
- Veredito: 'sem_pareamento' bloqueia SEMPRE (nem force); veredito não-
  favorável → 409 sem force e promove COM force (flag 'forced' selada).
- Blast-radius: pipeline PUBLICADO com o agente → 409 sem ack_blast;
  promove com ack (rascunho não bloqueia).
- Apply: backfill + revisão source='promotion' com selo no note; version
  bump; audit; has_overrides no sumário do compare; markers do template.

Mocks nos módulos — sem DB/LLM reais, convenção da suíte.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.revisions as revisions
import app.routes.optimizer as opt


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(opt.router)
    return TestClient(app, raise_server_exceptions=False)


def _details(passes: dict) -> str:
    return json.dumps([{"case_id": k, "passed": v} for k, v in passes.items()])


# challenger vence 7-0 (p=2*0.5^7 ≈ 0.0156 < 0.05 → 'b_melhor')
_CHAMP = {
    "id": "A", "run_type": "experiment", "status": "completed",
    "agent_id": "a1", "pipeline_id": None, "gold_version": "v1",
    "gold_hash": "h1", "config_overrides": None, "total_cases": 10,
    "judge_model": "azure/gpt-4o",
    "details": _details({**{f"c{i}": True for i in range(3)},
                         **{f"d{i}": False for i in range(7)}}),
}
_CHALL = {
    "id": "B", "run_type": "experiment", "status": "completed",
    "agent_id": "a1", "pipeline_id": None, "gold_version": "v1",
    "gold_hash": "h1", "total_cases": 10,
    "judge_model": "azure/gpt-4o",
    "config_overrides": json.dumps({"system_prompt": "prompt VENCEDOR"}),
    "details": _details({**{f"c{i}": True for i in range(3)},
                         **{f"d{i}": True for i in range(7)}}),
}

_BODY = {"agent_id": "a1", "champion_eval_id": "A", "challenger_eval_id": "B"}


def _wire(monkeypatch, *, champ=None, chall=None, pipelines=None,
          subgraph_has_agent=True):
    monkeypatch.setattr(opt, "require_role",
                        lambda *r: _async({"id": "u1", "role": "admin",
                                           "username": "root"}))
    monkeypatch.setattr(opt.agents_repo, "find_by_id", _async({
        "id": "a1", "name": "Ag", "version": "1.0.0",
        "system_prompt": "prompt ATUAL", "llm_provider": "azure",
        "model": "gpt-4o"}))
    updated = {}

    async def _update(_id, d):
        updated.update(d)

    monkeypatch.setattr(opt.agents_repo, "update", _update)
    runs = {"A": dict(champ or _CHAMP), "B": dict(chall or _CHALL)}

    async def _find_run(rid):
        return runs.get(rid)

    monkeypatch.setattr(opt.eval_runs_repo, "find_by_id", _find_run)
    monkeypatch.setattr("app.core.database.pipelines_repo.find_all",
                        _async(pipelines or []))

    async def _sub(pid):
        return {"root_agent_id": "a1",
                "nodes": ([{"id": "a1"}] if subgraph_has_agent else
                          [{"id": "outro"}])}

    monkeypatch.setattr("app.catalog.pipeline_defs._build_subgraph", _sub)
    monkeypatch.setattr("app.core.database.audit_repo.create", _async(None))
    rev_calls = {"backfill": [], "record": []}

    async def _bf(**kw):
        rev_calls["backfill"].append(kw)

    async def _rec(**kw):
        rev_calls["record"].append(kw)
        return "rev_promo"

    monkeypatch.setattr(revisions, "safe_backfill", _bf)
    monkeypatch.setattr(revisions, "safe_record", _rec)
    return updated, rev_calls


class TestGuards:
    def test_404_runs(self, monkeypatch):
        _wire(monkeypatch)

        async def _none(rid):
            return None

        monkeypatch.setattr(opt.eval_runs_repo, "find_by_id", _none)
        assert _client().post("/api/v1/optimizer/promote",
                              json=_BODY).status_code == 404

    def test_422_champion_com_overrides(self, monkeypatch):
        champ = {**_CHAMP, "config_overrides": json.dumps({"system_prompt": "x"})}
        _wire(monkeypatch, champ=champ)
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 422 and "champion" in r.json()["detail"]

    def test_422_challenger_sem_selo(self, monkeypatch):
        chall = {**_CHALL, "config_overrides": None}
        _wire(monkeypatch, chall=chall)
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 422 and "selo" in r.json()["detail"]

    def test_422_skill_purpose_ainda_nao(self, monkeypatch):
        chall = {**_CHALL, "config_overrides": json.dumps(
            {"system_prompt": "x", "skill_purpose": "p"})}
        _wire(monkeypatch, chall=chall)
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 422 and "skill_purpose" in r.json()["detail"]

    def test_409_gold_hash_divergente(self, monkeypatch):
        chall = {**_CHALL, "gold_hash": "h2"}
        _wire(monkeypatch, chall=chall)
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 409 and "gold" in r.json()["detail"].lower()

    def test_422_run_de_outro_alvo(self, monkeypatch):
        chall = {**_CHALL, "agent_id": "OUTRO"}
        _wire(monkeypatch, chall=chall)
        assert _client().post("/api/v1/optimizer/promote",
                              json=_BODY).status_code == 422


class TestVeredito:
    def test_409_sem_pareamento_mesmo_com_force(self, monkeypatch):
        champ = {**_CHAMP, "details": "[]"}
        chall = {**_CHALL, "details": "[]"}
        _wire(monkeypatch, champ=champ, chall=chall)
        r = _client().post("/api/v1/optimizer/promote",
                           json={**_BODY, "force": True})
        assert r.status_code == 409 and "pareável" in r.json()["detail"]

    def test_409_inconclusivo_sem_force_e_promove_com_force(self, monkeypatch):
        # 1×1 discordantes → inconclusivo
        det_a = _details({"c1": True, "c2": False, "c3": True})
        det_b = _details({"c1": False, "c2": True, "c3": True})
        champ = {**_CHAMP, "details": det_a}
        chall = {**_CHALL, "details": det_b}
        updated, rev = _wire(monkeypatch, champ=champ, chall=chall)
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 409
        r2 = _client().post("/api/v1/optimizer/promote",
                            json={**_BODY, "force": True})
        assert r2.status_code == 200, r2.text
        assert r2.json()["sealed"]["forced"] is True
        assert updated["system_prompt"] == "prompt VENCEDOR"


class TestBlastRadius:
    def test_409_pipeline_publicado_sem_ack(self, monkeypatch):
        _wire(monkeypatch, pipelines=[
            {"id": "p1", "name": "Prod", "status": "publicado"}])
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 409
        assert "PUBLICADO" in r.json()["detail"]["message"]

    def test_promove_com_ack_e_lista_afetados(self, monkeypatch):
        updated, _ = _wire(monkeypatch, pipelines=[
            {"id": "p1", "name": "Prod", "status": "publicado"}])
        r = _client().post("/api/v1/optimizer/promote",
                           json={**_BODY, "ack_blast": True})
        assert r.status_code == 200, r.text
        assert r.json()["affected_pipelines"][0]["id"] == "p1"
        assert updated["system_prompt"] == "prompt VENCEDOR"

    def test_rascunho_nao_bloqueia(self, monkeypatch):
        updated, _ = _wire(monkeypatch, pipelines=[
            {"id": "p1", "name": "Draft", "status": "rascunho"}])
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 200, r.text
        assert updated["system_prompt"] == "prompt VENCEDOR"


class TestApply:
    def test_happy_path_aplica_com_historico_e_selo(self, monkeypatch):
        updated, rev = _wire(monkeypatch)
        r = _client().post("/api/v1/optimizer/promote", json=_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        # apply + bump
        assert updated["system_prompt"] == "prompt VENCEDOR"
        assert updated["version"] == "1.0.1" and body["version"] == "1.0.1"
        # histórico: backfill do ATUAL + revisão source='promotion' com selo
        assert rev["backfill"][0]["old_content"] == "prompt ATUAL"
        rec = rev["record"][0]
        assert rec["source"] == "promotion"
        assert rec["content"] == "prompt VENCEDOR"
        assert "challenger_eval_id" in (rec["note"] or "")
        # selo: modelo efetivo + par de runs + veredito honesto
        sealed = body["sealed"]
        assert sealed["provider"] == "azure" and sealed["model"] == "gpt-4o"
        assert sealed["verdict"] == "b_melhor" and sealed["forced"] is False
        assert body["revision_id"] == "rev_promo"


def test_summary_do_compare_expoe_has_overrides():
    from app.routes.dashboard import _summary_of_run
    assert _summary_of_run(dict(_CHALL))["has_overrides"] is True
    assert _summary_of_run(dict(_CHAMP))["has_overrides"] is False


def test_template_botao_de_promocao():
    from pathlib import Path
    src = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")
    assert 'data-testid="promote-challenger"' in src
    assert "promoteChallenger()" in src
    assert "ack_blast:true" in src.replace(" ", "")
