"""Fluxograma de agentes (PR3) — FSM canônica resolvida por agente.

"Abrir um nó" no Fluxograma mostra a FSM canônica (state_machine.py) ADAPTADA ao
agente: declarativo (sem LLM) vs LLM (fast/standard/rigorous), e o efeito de
require_evidence=0 (pula RetrieveEvidence/VerifyEvidence). A regra exec_mode→fases
está hardcoded no engine; aqui é a fonte ÚNICA consultável (`_build_fsm_profile`),
e o JS apenas renderiza — sem duplicar a regra (evita drift client×engine).

Cobertura:
- sem skill / modo inválido → unresolved (phases vazias)
- declarativo → trilha sem LLM, sem folhas terminais
- fast/standard/rigorous → 6 fases canônicas + 3 folhas; notas por perfil
- require_evidence=0 → Retrieve/VerifyEvidence marcadas 'skipped'
- invariantes: Intake/PolicyCheck/LogAndClose sempre 'always'; LogAndClose terminal;
  ramo policy_denied em PolicyCheck
- endpoint: 404 e caminho sem skill
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.routes.mesh import _build_fsm_profile


def _ids(prof):
    return [p["id"] for p in prof["phases"]]


def _by_id(prof, pid):
    return next(p for p in prof["phases"] if p["id"] == pid)


def test_unresolved_when_no_mode():
    for mode in (None, "", "weird"):
        prof = _build_fsm_profile(mode, 1)
        assert prof["execution_mode"] is None
        assert prof["phases"] == [] and prof["leaves"] == []


def test_declarative_lane_has_no_llm_and_no_leaves():
    prof = _build_fsm_profile("declarative", None)
    assert prof["execution_mode"] == "declarative"
    assert _ids(prof) == ["Intake", "PolicyCheck", "Declarative", "LogAndClose"]
    assert prof["leaves"] == []
    assert "sem LLM" in _by_id(prof, "Declarative")["desc"]


@pytest.mark.parametrize("mode", ["fast", "standard", "rigorous"])
def test_llm_modes_have_canonical_seven_path(mode):
    prof = _build_fsm_profile(mode, 1)
    assert _ids(prof) == ["Intake", "PolicyCheck", "RetrieveEvidence", "DraftAnswer", "VerifyEvidence", "LogAndClose"]
    # 3 folhas terminais mutuamente exclusivas
    assert [l["label"] for l in prof["leaves"]] == ["Recommend", "Refuse", "Escalate"]
    # invariantes de sempre-executadas + terminal
    assert _by_id(prof, "Intake")["state"] == "always"
    assert _by_id(prof, "PolicyCheck")["state"] == "always"
    lac = _by_id(prof, "LogAndClose")
    assert lac["state"] == "always" and lac.get("terminal") is True
    # ramo policy_denied
    assert "policy_denied" in _by_id(prof, "PolicyCheck")["branch"]


def test_reflection_and_verify_notes_per_profile():
    fast = _build_fsm_profile("fast", 1)
    assert _by_id(fast, "DraftAnswer")["note"] == ""               # fast: sem reflexão
    assert _by_id(fast, "VerifyEvidence")["note"] == "heurística"
    std = _build_fsm_profile("standard", 1)
    assert "reflexão" in _by_id(std, "DraftAnswer")["note"]
    assert _by_id(std, "VerifyEvidence")["note"] == "heurística"
    rig = _build_fsm_profile("rigorous", 1)
    assert "reflexão" in _by_id(rig, "DraftAnswer")["note"]
    assert "LLM" in _by_id(rig, "VerifyEvidence")["note"]


def test_require_evidence_zero_skips_evidence_phases():
    prof = _build_fsm_profile("standard", 0)
    assert _by_id(prof, "RetrieveEvidence")["state"] == "skipped"
    assert _by_id(prof, "VerifyEvidence")["state"] == "skipped"
    assert "require_evidence=0" in _by_id(prof, "RetrieveEvidence")["note"]
    # fases sempre-executadas seguem normais
    assert _by_id(prof, "Intake")["state"] == "always"


@pytest.mark.asyncio
async def test_endpoint_404(monkeypatch):
    from app.routes import mesh
    repo = AsyncMock()
    repo.find_by_id = AsyncMock(return_value=None)
    monkeypatch.setattr(mesh, "agents_repo", repo)
    with pytest.raises(HTTPException) as ei:
        await mesh.get_agent_fsm("missing")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_agent_without_skill(monkeypatch):
    from app.routes import mesh
    repo = AsyncMock()
    repo.find_by_id = AsyncMock(return_value={"id": "a1", "skill_id": None, "require_evidence": 1})
    monkeypatch.setattr(mesh, "agents_repo", repo)
    res = await mesh.get_agent_fsm("a1")
    assert res["execution_mode"] is None
    assert res["phases"] == []
    assert res["agent_id"] == "a1"
    assert res["require_evidence"] == 1
