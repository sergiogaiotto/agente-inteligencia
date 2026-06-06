"""Bug user 2026-06-06: pipeline "Pesquisador A → {Rentab, Retenção}" (fan-out
1-de-N por aresta CONDITIONAL). Perguntas de RETENÇÃO roteavam certo, mas as de
RENTABILIZAÇÃO caíam em `skipped_conditional` — o subagente Rentab NUNCA rodava
(execução 1/3, só o roteador; ver anexo execution-log).

Causa-raiz: o gerador `_deriveConditionalExpr` (agent_form.html) montava a regra
condicional casando PALAVRA INTEIRA contra `input_lower`:
    'rentabilidade' in input_lower
A morfologia do PT derruba isso — o usuário digita 'rentabiliza**ção**',
'rentabili**zar**', 'rentab', e a substring 'rentabilidade' não casa NENHUMA.
A Retenção só funcionava por sorte: a keyword 'retenção' é idêntica ao que se
digita. (Confirmado: expr e input ambos NFC, sem erro de avaliação nos logs —
o gate pulava de propósito.)

Fix (gerador, agent_form.html):
  1) STEMMING leve (`_stemPt`) unifica a família morfológica
     (rentabilidade/rentabilização/rentabilizar → 'rentabil');
  2) SEMEIA o radical do NOME do agente-alvo ("Rentab" → 'rentab') como keyword
     prioritária — casa as variações do domínio E aparece no output do roteador;
  3) stopwords expandidas (fala/trata/questão/…) pra não virar ímã ao stemizar.

Este teste trava o CONTRATO no motor real (`_eval_conditional` +
`_build_conditional_context`): exprs com RADICAL roteiam as flexões que o usuário
digita; a expr antiga (palavra inteira) é provada falha. As exprs abaixo são
exatamente as que o gerador corrigido produz (e com que a pipeline foi reparada).
"""
from __future__ import annotations

import pytest

from app.agents.engine import _eval_conditional, _build_conditional_context


def _runs(expr: str, user_input: str) -> bool:
    """True se a aresta condicional EXECUTA o alvo para esse input (expr=True).
    (No engine, skip = `not _eval_conditional(...)`; aqui medimos o positivo.)"""
    ctx = _build_conditional_context(output="", final_state="", user_input=user_input)
    return _eval_conditional(expr, ctx)


# Exprs do gerador CORRIGIDO — idênticas às gravadas na pipeline ao reparar.
RENTAB_EXPR = (
    "'rentab' in input_lower or 'rentabil' in input_lower "
    "or 'result' in input_lower or 'financ' in input_lower"
)
RETEN_EXPR = (
    "'reten' in input_lower or 'client' in input_lower "
    "or 'estratégi' in input_lower or 'fidel' in input_lower"
)

# A regra ANTIGA (quebrada) que estava no banco para a aresta → Rentab.
OLD_RENTAB_EXPR = (
    "'questão' in input_lower or 'trata' in input_lower "
    "or 'rentabilidade' in input_lower or 'resultados' in input_lower "
    "or 'financeiros' in input_lower"
)

# Inputs REAIS digitados pelo usuário (do banco, turns desta sessão).
RENTAB_INPUTS = [
    "me fale sobre rentabilização",
    "como rentabilizar?",
    "e o que temos sobre rentab",
    "qual a melhor forma de rentabilização",
]
RETEN_INPUTS = [
    "me fale sobre retenção",
    "o que temos sobre retenção",
    "o que temos sobre retenção de clientes",
]


class TestOldExprWasBroken:
    """Documenta a regressão: a expr antiga (palavra inteira) NÃO casa as
    flexões/derivações que o usuário digita — por isso Rentab nunca rodava."""

    @pytest.mark.parametrize("q", RENTAB_INPUTS)
    def test_old_full_word_expr_misses_inflections(self, q):
        assert _runs(OLD_RENTAB_EXPR, q) is False, (
            f"esperado FALHA da regra antiga para {q!r} (era o bug)"
        )

    def test_old_expr_only_matched_the_exact_noun(self):
        # Só casava se o usuário digitasse a PALAVRA exata 'rentabilidade'.
        assert _runs(OLD_RENTAB_EXPR, "fale sobre rentabilidade") is True


class TestFixedExprRoutesRentabilizacao:
    @pytest.mark.parametrize("q", RENTAB_INPUTS)
    def test_rentab_runs_for_profit_queries(self, q):
        assert _runs(RENTAB_EXPR, q) is True

    @pytest.mark.parametrize("q", RENTAB_INPUTS)
    def test_reten_skipped_for_profit_queries(self, q):
        assert _runs(RETEN_EXPR, q) is False


class TestFixedExprRoutesRetencao:
    @pytest.mark.parametrize("q", RETEN_INPUTS)
    def test_reten_runs_for_retention_queries(self, q):
        assert _runs(RETEN_EXPR, q) is True

    @pytest.mark.parametrize("q", RETEN_INPUTS)
    def test_rentab_skipped_for_retention_queries(self, q):
        assert _runs(RENTAB_EXPR, q) is False


class TestNoCrossMatching:
    """Garante separação limpa dos ramos (sem dois SAs rodando à toa)."""

    def test_each_real_input_routes_to_exactly_one_branch(self):
        for q in RENTAB_INPUTS:
            assert (_runs(RENTAB_EXPR, q), _runs(RETEN_EXPR, q)) == (True, False), q
        for q in RETEN_INPUTS:
            assert (_runs(RENTAB_EXPR, q), _runs(RETEN_EXPR, q)) == (False, True), q

    def test_name_stem_is_the_robust_signal(self):
        # O radical do NOME do agente ('rentab'/'reten') casa toda a família,
        # inclusive quando o usuário digita só o nome do agente.
        assert _runs("'rentab' in input_lower", "e o que temos sobre rentab") is True
        assert _runs("'reten' in input_lower", "preciso de retenção agora") is True
