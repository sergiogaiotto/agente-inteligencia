# Onda — Harness usar o Verifier multi-dim

## Goal

Fazer o **Harness de Avaliação (§9.5)** consumir o **Verifier multi-dimensional (§14.2)** que já roda em runtime, transformando o gate de release de um proxy raso (state match + similarity/regex + red flags) num gate qualitativo com **factuality, completeness, tone_adherence, safety, contract compliance e taxa de alucinação**, com agregação por categoria e detecção de regressão por dimensão.

## Why

Hoje o Verifier v2 já roda em `execute_interaction` quando `VERIFIER_V2_ENABLED=true` e persiste em `verifications`. A página `/quality` mostra essas métricas para tráfego ad-hoc (workspace). **Mas o Harness, que é o canal canônico de gate offline contra Golden Dataset, ignora o Verifier** — decide release com `accuracy`, `correct_refusal_rate`, `false_positive_rate`. Métricas que distinguem "passou no shape" de "respondeu com factualidade" não pesam no gate. Resultado: regressão silenciosa em qualidade narrativa pode entrar em produção desde que o estado da FSM bata e o regex case.

## Scope

3 blocos. **Wave 1** é pré-requisito; **Wave 2** tem dois plans paralelos.

| Wave | Plan | Entrega |
|------|------|---------|
| 1 | `01-PLAN-engine-expose-verification.md` | `execute_interaction` retorna `verification` no resultado |
| 2 | `02-PLAN-harness-multidim-gate.md` | Harness lê dimensões, agrega, gate multi-dim, schema migrado |
| 2 | `03-PLAN-harness-ui-dimensions.md` | UI do `/harness` mostra dimensões e razão do gate |

## Out of scope

- Mudar o judge (`MultiDimJudge`) ou as 4 dimensões.
- Mudar o `ContractValidator`.
- Refatorar o fluxo de evidence (retrieve/rerank).
- Implementar comparação side-by-side entre baselines (mencionada na §9.5 como "futuro").

## Decisões assumidas (sem confirmação do usuário)

Documento aqui o que assumi para destravar o plano. Cada uma pode ser revertida em revisão.

1. **Verifier no harness roda separado, não via `execute_interaction`.** O harness chama `verifier.verify(draft=result.output, evidences=...)` depois de receber o `result`, garantindo `profile=rigorous` e ignorando o `_execution_mode` do skill. Razão: alguns skills usam `fast` e pulam o judge — sem isso a média de factuality vira NaN. Custo: o draft é re-julgado, mas o engine não precisa mudar de assinatura.
   - **Alternativa não escolhida**: aceitar `profile` como parâmetro em `execute_interaction`. Rejeitada porque polui a API pública só para satisfazer o harness.

2. **PR fatiado**: Wave 1 sai antes (PR pequeno, retrocompatível, valor imediato no `/workspace`); Wave 2 vai em PR único cobrindo backend + UI.

3. **Thresholds default no `Settings`**:
   - `harness_min_avg_factuality = 3.5` (de 0–5)
   - `harness_max_safety_violation_rate = 0.05`
   - `harness_min_contract_compliance = 0.95`
   - `harness_max_hallucination_rate = 0.10`
   - `harness_max_dim_regression_pct = 5.0` (qualquer dimensão caindo > 5% em regression-run reprova)
   - `harness_use_verifier = True` (toggle global; quando `False`, harness mantém comportamento atual sem chamar o judge — útil para runs rápidos/baratos).

4. **Compat com casos sem judge**: se `verification.dimensions[k].score is None` (ex: judge falhou ou skipped), o caso conta na contagem global mas é excluído da média daquela dimensão. Adicionar `dim_skipped` no entry.

## Must-haves (goal-backward)

A onda só está pronta quando, para um agente arbitrário e Golden Dataset com ≥ 5 casos:

- [ ] `execute_interaction` retorna `result["verification"]` com as 4 dimensões + ok + contract.
- [ ] `run_evaluation` retorna `avg_factuality`, `avg_completeness`, `avg_tone`, `safety_violation_rate`, `contract_compliance_rate`, `hallucination_rate`.
- [ ] Coluna `eval_runs.avg_factuality` (e demais) existe no banco e é populada em runs novos.
- [ ] Gate rejeita release quando `avg_factuality < 3.5` mesmo com `accuracy >= 0.80`.
- [ ] Razão da rejeição lista qual threshold quebrou (ex: `gate_rejected: avg_factuality=2.8 < 3.5`).
- [ ] Página `/harness` mostra mini-badges F/C/T/S por execução (mesmo padrão do `/quality`).
- [ ] Página `/harness` no detalhe expande breakdown por categoria + top-10 unsupported_claims agregadas.
- [ ] Toggle `HARNESS_USE_VERIFIER=false` desativa as chamadas extras do judge e o harness opera no modo legacy (acurácia + refusal + FP) — coluna `judge_used=false` em `eval_runs`.
- [ ] Run sem `VERIFIER_V2_ENABLED` não quebra (gracefully degrades com `judge_used=false`, dimensões NULL).

## Risks

- **Custo dobrado.** Em dataset de 500 casos, +500 chamadas LLM (judge). Toggle `HARNESS_USE_VERIFIER=false` é o escape. Mitigação extra: documentar no README e mostrar custo estimado no botão "Executar Harness" antes da confirmação (não é parte desta onda — anotar).
- **Latência do harness.** Hoje cada caso é uma `execute_interaction`; agora vira `execute_interaction` + `verifier.verify`. Se o judge está em `azure/gpt-4o`, +2-4s por caso. Para 500 casos: +20-30 min. Aceitável (harness é assíncrono); documentar.
- **Self-preference.** Se `verifier_judge_model` == modelo do agente, judge favorece o draft. `MultiDimJudge` já tem nota de "anti-self-preference" mas não impõe. Mitigação: log de warning quando `judge_model.startswith(agent.model)` no harness; não bloquear (decisão do operador).

## Files touched (resumo)

| Arquivo | Wave | Tipo |
|---------|------|------|
| `app/agents/engine.py` | 1 | edit |
| `app/harness/evaluator.py` | 2 | edit |
| `app/core/database.py` | 2 | edit (migrations) |
| `app/core/config.py` | 2 | edit (settings novos) |
| `app/templates/pages/harness.html` | 2 | edit |
| `app/routes/dashboard.py` | 2 | edit (response inclui novas colunas) |
| `tests/` (a criar se não existir) | 1, 2 | new |
