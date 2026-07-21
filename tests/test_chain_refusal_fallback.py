"""F-4 opção (b) — recusa de qualidade na cadeia não descarta resposta boa (66.5.0).

Decisão de produto (2026-07-21, aprovada pelo dono): numa cadeia A→B, quando o
DONO do output (B) recusa por QUALIDADE (evidência insuficiente), a resposta
final volta ao último ESPECIALISTA anterior aprovado + rodapé honesto — em vez
de descartar a resposta boa (caso real do E2E Farol: Comercial groundeado →
Financeiro recusou o complemento pró-rata → cliente perdeu os planos).

Contrato selado (fail-closed em tudo que não for o caso exato):
 1. qualidade ('evidence_insufficient') + especialista anterior aprovado
    ('evidence_ok', subagent, completed, output real) → fallback no ÚLTIMO;
 2. recusa de POLÍTICA ('policy_refusal') ou risco ('risk_or_fraud') → NUNCA
    cai — a recusa vence (mostrar output anterior poderia vazar o que ela
    conteve);
 3. routers/maestro nunca são candidatos (output = protocolo/orquestração);
 4. sem candidato → recusa fica (comportamento atual);
 5. owner sem recusa → no-op;
 6. wiring: execute_pipeline chama o helper e aplica o rodapé (guard por
    inspect, padrão test_eval_run_validation).
"""

import inspect

from app.agents.engine import (
    _CHAIN_FALLBACK_NOTE,
    _chain_quality_fallback,
    _is_grounded_answer_step,
    _is_quality_refusal,
    execute_pipeline,
)


def _step(kind="subagent", status="completed", output="resposta real",
          conditions=("evidence_ok",), name="X"):
    return {
        "agent_name": name, "agent_kind": kind, "status": status,
        "output": output,
        "transitions": [{"from": "VerifyEvidence", "to": "?", "condition": c}
                        for c in conditions],
    }


def _refusal(*conditions):
    return _step(output="Recusa controlada: ...", conditions=conditions,
                 name="Financeiro")


class TestPredicados:
    def test_qualidade_pura_e_recusa_de_qualidade(self):
        assert _is_quality_refusal(_refusal("evidence_insufficient")) is True

    def test_politica_vence_mesmo_com_qualidade_junto(self):
        assert _is_quality_refusal(
            _refusal("evidence_insufficient", "policy_refusal")) is False
        assert _is_quality_refusal(_refusal("policy_refusal")) is False

    def test_risco_fraude_vence(self):
        assert _is_quality_refusal(
            _refusal("evidence_insufficient", "risk_or_fraud")) is False

    def test_candidato_exige_subagent_completed_com_evidence_ok(self):
        assert _is_grounded_answer_step(_step()) is True
        assert _is_grounded_answer_step(_step(kind="router")) is False
        assert _is_grounded_answer_step(_step(kind="aobd")) is False
        assert _is_grounded_answer_step(_step(status="skipped_conditional")) is False
        assert _is_grounded_answer_step(_step(output="  ")) is False
        assert _is_grounded_answer_step(
            _step(conditions=("evidence_insufficient",))) is False


class TestSelecaoDoFallback:
    def test_caso_farol_comercial_para_financeiro(self):
        comercial = _step(name="Comercial")
        owner = _refusal("evidence_insufficient")
        steps = [_step(kind="aobd", name="Maestro"),
                 _step(kind="router", name="Triagem"), comercial, owner]
        assert _chain_quality_fallback(steps, owner) is comercial

    def test_pega_o_ULTIMO_especialista_aprovado(self):
        a = _step(name="A")
        b = _step(name="B")
        owner = _refusal("evidence_insufficient")
        assert _chain_quality_fallback([a, b, owner], owner) is b

    def test_recusa_de_politica_nunca_cai(self):
        comercial = _step(name="Comercial")
        owner = _refusal("policy_refusal")
        assert _chain_quality_fallback([comercial, owner], owner) is None

    def test_sem_candidato_recusa_fica(self):
        steps = [_step(kind="aobd", name="Maestro"),
                 _step(kind="router", name="Triagem")]
        owner = _refusal("evidence_insufficient")
        assert _chain_quality_fallback([*steps, owner], owner) is None

    def test_owner_sem_recusa_e_noop(self):
        comercial = _step(name="Comercial")
        owner = _step(name="Financeiro")  # evidence_ok
        assert _chain_quality_fallback([comercial, owner], owner) is None

    def test_owner_none_e_noop(self):
        assert _chain_quality_fallback([_step()], None) is None


def test_wiring_no_execute_pipeline():
    src = inspect.getsource(execute_pipeline)
    assert "_chain_quality_fallback(" in src, "fallback não está ligado ao pipeline"
    assert "_CHAIN_FALLBACK_NOTE" in src, "rodapé honesto não é aplicado"
    # O rodapé é texto de plataforma, não do agente — e sem emoji (strip_emoji
    # roda no output final; um ⚠️ aqui seria silenciosamente removido).
    assert "Atenção:" in _CHAIN_FALLBACK_NOTE
    assert "⚠" not in _CHAIN_FALLBACK_NOTE
