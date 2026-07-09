"""F3 — check de preflight 'grounding starvation'.

Um Especialista/Maestro SEM base (skill_id), com 'Permitir conhecimento geral do
LLM' desligado e 'Exigir evidências' (grounding_strict) global ligado, RECUSA
toda entrada no invoke direto. Nenhum dos 10 checks anteriores cobria isso.
Triagem (router) é ISENTA da recusa por design — não deve disparar.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.agents.preflight import check_grounding_starvation


def _settings(strict: bool = True):
    return SimpleNamespace(grounding_strict=strict)


def _payload(kind="subagent", skill_id=None, allow_general=False):
    return {"kind": kind, "skill_id": skill_id, "allow_general_knowledge": allow_general}


def test_warns_for_especialista_without_base():
    r = check_grounding_starvation(_payload("subagent"), _settings(True))
    assert r is not None
    assert r.severity == "warning"
    assert r.id == "C11_grounding_starvation"
    assert r.field == "allow_general_knowledge"


def test_warns_for_maestro_without_base():
    r = check_grounding_starvation(_payload("aobd"), _settings(True))
    assert r is not None and r.severity == "warning"


def test_router_triagem_is_exempt():
    assert check_grounding_starvation(_payload("router"), _settings(True)) is None


def test_no_warn_when_skill_linked():
    assert check_grounding_starvation(_payload("subagent", skill_id="sk-1"), _settings(True)) is None


def test_no_warn_when_general_knowledge_allowed():
    assert check_grounding_starvation(_payload("subagent", allow_general=True), _settings(True)) is None


def test_no_warn_when_grounding_not_strict():
    assert check_grounding_starvation(_payload("subagent"), _settings(strict=False)) is None
