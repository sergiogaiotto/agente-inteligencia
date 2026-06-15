"""Harness §9.5 — casamento de estado por DECISÃO, não por terminal.

Bug pego em smoke (2026-06-15): o FSM clássico colapsa a decisão
(Recommend/Refuse/Escalate) no estado terminal LogAndClose, então
`result["final_state"]` vem SEMPRE 'LogAndClose'. O harness comparava isso
contra o expected_state da UI (Recommend/Refuse/Escalate) e reprovava TODO caso
correto — além de zerar correct_refusal_rate/false_positive_rate, que checam
actual_state in (Refuse, Escalate).

`_decision_state` recupera a decisão real a partir do transition_log (o `from`
da transição que entrou em LogAndClose) e cai no final_state cru quando não há
transição de decisão (skills declarativas reportam 'completed', transitions=[]).

Convenção do projeto (cf. test_grounding_by_default.py): função pura, sem DB/LLM.
"""
from __future__ import annotations

from app.harness.evaluator import _decision_state


def _fsm_result(decision: str) -> dict:
    """Resultado típico do FSM clássico: decisão colapsada em LogAndClose."""
    return {
        "final_state": "LogAndClose",
        "transitions": [
            {"from": "Intake", "to": "PolicyCheck", "condition": ""},
            {"from": "PolicyCheck", "to": "RetrieveEvidence", "condition": ""},
            {"from": "RetrieveEvidence", "to": "DraftAnswer", "condition": ""},
            {"from": "DraftAnswer", "to": "VerifyEvidence", "condition": ""},
            {"from": "VerifyEvidence", "to": decision, "condition": "x"},
            {"from": decision, "to": "LogAndClose", "condition": ""},
        ],
    }


def test_recommend_recuperado_do_transition_log():
    assert _decision_state(_fsm_result("Recommend")) == "Recommend"


def test_refuse_recuperado_do_transition_log():
    # Antes do fix isso devolvia 'LogAndClose' → correct_refusal_rate morto.
    assert _decision_state(_fsm_result("Refuse")) == "Refuse"


def test_escalate_recuperado_do_transition_log():
    assert _decision_state(_fsm_result("Escalate")) == "Escalate"


def test_final_state_ja_eh_decisao_passa_direto():
    # Robustez: se algum caminho já reportar a decisão crua, não mexe.
    assert _decision_state({"final_state": "Recommend", "transitions": []}) == "Recommend"


def test_declarativo_completed_cai_no_fallback():
    # Skill declarativa: final_state='completed', sem transições → devolve cru.
    assert _decision_state({"final_state": "completed", "transitions": []}) == "completed"


def test_logandclose_sem_transicoes_devolve_terminal():
    # Sem transição de decisão não há o que recuperar — preserva o terminal.
    assert _decision_state({"final_state": "LogAndClose", "transitions": []}) == "LogAndClose"


def test_campos_ausentes_nao_quebram():
    assert _decision_state({}) == ""
    assert _decision_state({"final_state": None}) == ""


def test_pega_ultima_transicao_para_logandclose():
    # Multi-turn defensivo: havendo várias entradas em LogAndClose, usa a última.
    result = {
        "final_state": "LogAndClose",
        "transitions": [
            {"from": "Recommend", "to": "LogAndClose"},
            {"from": "VerifyEvidence", "to": "Refuse"},
            {"from": "Refuse", "to": "LogAndClose"},
        ],
    }
    assert _decision_state(result) == "Refuse"
