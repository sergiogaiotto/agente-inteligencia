"""#683: hallucination_rate honesto — taxa sobre os casos com factualidade AVALIADA.

Sintoma (E2E de QA 2026-07-18): pipelines fundamentados por RAG eram reprovados
no gate por `hallucination_rate` 71–100%, embora o grounding funcionasse ao vivo.
Causa: em modo pipeline, steps 'standard' não têm snapshot de verification
reancorado → o caso cai no re-judge SEM evidência → `factuality=null`, mas
`unsupported_claims` era contado mesmo assim, inflando a taxa (dividida pelo TOTAL).

Fix: `_hallucination_rate` mede a taxa sobre os casos com factualidade avaliada
(espelha `safety_violation_rate`/`contract_compliance_rate`) e devolve None quando
nenhum foi avaliado — "não medido" != "0.0 alucinação" — e o gate não reprova por
uma métrica que o harness não conseguiu medir.
"""
from app.harness.evaluator import _hallucination_rate


def test_none_when_no_case_had_factuality_evaluated():
    # pipeline só com steps 'standard' → factualidade nunca avaliada
    assert _hallucination_rate(hallucination_count=0, factuality_evaluated=0) is None


def test_rate_is_over_evaluated_cases_not_total():
    # 1 alucinação em 4 casos AVALIADOS = 0.25 (não 1/7 diluído no total)
    assert _hallucination_rate(hallucination_count=1, factuality_evaluated=4) == 0.25


def test_zero_when_evaluated_but_clean():
    assert _hallucination_rate(hallucination_count=0, factuality_evaluated=5) == 0.0


def test_full_rate_when_all_evaluated_hallucinate():
    assert _hallucination_rate(hallucination_count=3, factuality_evaluated=3) == 1.0
