"""Simetria draft×juiz nas evidências (66.4.3) — achado E2E 2026-07-21 (F-2).

Falsa recusa reproduzida 3× na VPS: o DRAFT via cada snippet INTEGRAL, mas o
bloco de evidências do juiz cortava em 500 chars POR evidência
(multi_dim_judge._format_evidences). Com o chunk default (~500 tokens ≈ 2000
chars), o juiz via ~25% do que o draft viu: no caso real, os fatos da resposta
("90 dias" / "5 dias úteis" / "estorno em dobro") estavam nas posições
607/841/1108 do chunk recuperado — TODOS além do corte → o juiz marcou tudo
unsupported → factuality=0 < threshold → ok=False → "Recusa controlada:
Evidência insuficiente" para um draft CORRETO e groundeado.

Contratos selados aqui:
 1. fato além do char 500 do snippet APARECE no bloco do juiz (morre no
    código antigo);
 2. o teto agora é orçamento TOTAL (proteção contra chunk patológico) com
    corte AVISADO no bloco — e as evidências seguintes continuam listadas;
 3. guarda anti-desobediência da rubrica: SEM evidências, factuality=0 do
    juiz vira None (a rubrica manda null; _compute_ok ignora None mas 0
    reprovava o run sozinho). COM evidências, 0 é veredito válido e fica.
"""

from types import SimpleNamespace

from app.verifier.multi_dim_judge import MultiDimJudge
from app.verifier.runtime import Verifier


def _ev(text, source="Política de Cobrança", score=0.03):
    return SimpleNamespace(relevance_score=score, source_name=source,
                           snippet_text=text)


def test_fato_apos_o_char_500_e_visivel_ao_juiz():
    # Reproduz o caso real: fato decisivo na posição 600+ do chunk.
    snippet = ("x" * 600) + " fatura duplicada comprovada tem estorno em dobro"
    out = MultiDimJudge._format_evidences([_ev(snippet)])
    assert "estorno em dobro" in out, (
        "o juiz não vê o que o draft viu — assimetria draft×juiz reintroduzida"
    )


def test_snippet_do_tamanho_de_um_chunk_default_passa_integral():
    snippet = ("a" * 1990) + " prazo de 90 dias"  # ~500 tokens
    out = MultiDimJudge._format_evidences([_ev(snippet)])
    assert "prazo de 90 dias" in out
    assert "corte pelo orçamento" not in out


def test_orcamento_total_trunca_com_aviso_e_nao_come_as_seguintes():
    gigante = "g" * (MultiDimJudge._EVIDENCE_BUDGET_CHARS + 5000)
    out = MultiDimJudge._format_evidences([_ev(gigante), _ev("fato final")])
    assert "corte pelo orçamento total do juiz" in out
    # A 2ª evidência continua listada (rotulada), mesmo sem orçamento restante.
    assert "[E2]" in out


def test_sem_evidencia_factuality_zero_do_juiz_vira_none():
    scores = {"factuality": 0.0, "completeness": 4.0,
              "tone_adherence": 5.0, "safety": 1.0}
    out = Verifier._null_unverifiable_factuality(scores, [])
    assert out["factuality"] is None
    assert out["completeness"] == 4.0


def test_com_evidencia_factuality_zero_e_veredito_valido_e_fica():
    scores = {"factuality": 0.0, "completeness": 4.0,
              "tone_adherence": 5.0, "safety": 1.0}
    out = Verifier._null_unverifiable_factuality(scores, [_ev("qualquer")])
    assert out["factuality"] == 0.0
