"""#684 (Fatia F): sinais de decisão do Verifier alimentando a FSM (Refuse/Escalate).

A recusa/escala redigida pelo agente ficava invisível em `Recommend` (a FSM só
transicionava Refuse por evidência insuficiente e Escalate por risk/fraud). Com
`verifier_signals_drive_fsm` ON, o rascunho é classificado em `policy_refusal`
(recusa por política/dado de 3º/injection → Refuse) e `needs_escalation`
(escalonamento → Escalate). Flag OFF → sinais ausentes/False → mapeamento
IDÊNTICO ao histórico (zero mudança de produção).
"""
import pytest

from app.agents.state_machine import InteractionContext, InteractionStateMachine, State
from app.verifier.runtime import detect_decision_signals


# ─── detector puro ───────────────────────────────────────────────
def test_detect_refusal():
    d = "Desculpe, não posso fornecer dados de terceiros por questões de privacidade."
    assert detect_decision_signals(d) == (True, False)


def test_detect_escalation():
    d = "Identifiquei falha regional; vou encaminhar ao NOC e abrir chamado NOC com protocolo."
    assert detect_decision_signals(d) == (False, True)


def test_detect_normal_answer_no_signal():
    d = "A segunda via da sua fatura está disponível no app Cometa, na seção Faturas."
    assert detect_decision_signals(d) == (False, False)


def test_refusal_has_precedence_over_escalation():
    d = "Não posso compartilhar dados de terceiro; não é caso de escalar para a gerência."
    assert detect_decision_signals(d) == (True, False)


def test_detector_is_pure_on_empty():
    assert detect_decision_signals("") == (False, False)
    assert detect_decision_signals(None) == (False, False)


# ─── FSM (interaction_id="" → sem escrita no DB) ─────────────────
@pytest.mark.asyncio
async def test_fsm_policy_refusal_transitions_to_refuse():
    ctx = InteractionContext(current_state=State.VERIFY_EVIDENCE)
    sm = InteractionStateMachine(ctx)
    await sm.run_verify_evidence({"ok": True, "confidence": 1.0, "policy_refusal": True})
    assert ctx.current_state == State.REFUSE


@pytest.mark.asyncio
async def test_fsm_needs_escalation_transitions_to_escalate():
    ctx = InteractionContext(current_state=State.VERIFY_EVIDENCE)
    sm = InteractionStateMachine(ctx)
    await sm.run_verify_evidence({"ok": True, "confidence": 1.0, "needs_escalation": True})
    assert ctx.current_state == State.ESCALATE


@pytest.mark.asyncio
async def test_fsm_off_flag_absent_signals_keeps_recommend():
    # flag OFF → engine não passa os sinais → comportamento histórico (Recommend)
    ctx = InteractionContext(current_state=State.VERIFY_EVIDENCE)
    sm = InteractionStateMachine(ctx)
    await sm.run_verify_evidence({"ok": True, "confidence": 1.0})
    assert ctx.current_state == State.RECOMMEND


@pytest.mark.asyncio
async def test_fsm_refusal_wins_over_risk():
    # recusa por política tem precedência sobre risco (Refuse, não Escalate)
    ctx = InteractionContext(current_state=State.VERIFY_EVIDENCE)
    sm = InteractionStateMachine(ctx)
    await sm.run_verify_evidence(
        {"ok": True, "confidence": 1.0, "policy_refusal": True, "risk_high": True}
    )
    assert ctx.current_state == State.REFUSE


# ─── wrapper do engine (gate por flag) ───────────────────────────
def test_engine_wrapper_off_returns_empty(monkeypatch):
    from app.agents.engine import _decision_signals
    import app.core.config as config

    class _S:
        verifier_signals_drive_fsm = False

    monkeypatch.setattr(config, "get_settings", lambda: _S())
    assert _decision_signals("não posso fornecer dados de terceiros") == {}


def test_engine_wrapper_on_returns_signals(monkeypatch):
    from app.agents.engine import _decision_signals
    import app.core.config as config

    class _S:
        verifier_signals_drive_fsm = True

    monkeypatch.setattr(config, "get_settings", lambda: _S())
    assert _decision_signals("Desculpe, não posso fornecer dados de terceiros.") == {
        "policy_refusal": True,
        "needs_escalation": False,
    }
