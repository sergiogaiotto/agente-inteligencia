# Onda — Comparação side-by-side de eval runs

## Goal

Implementar a feature mencionada na §9.5 da spec: **"Comparação side-by-side entre versões"**. Operador escolhe dois `eval_runs` rodados contra o mesmo Golden Dataset e vê deltas de accuracy, dimensões do judge, contract compliance, alucinação, latência — agregado e por categoria — mais a lista de casos onde o resultado divergiu (regressão ou melhoria).

## Why

Os dados já existem em `eval_runs` desde a Onda 2 (acurácia ponderada, 4 dimensões do Verifier, hallucination_rate, contract_compliance_rate, breakdown por categoria, lista de 100 cases com `passed/failure_reasons`). Falta a **view**.

Sem isso o operador precisa abrir cada run separadamente, decorar números, e fazer a comparação na cabeça. Pra release decision ("o novo modelo regrediu em compliance?"), isso é insustentável. Spec menciona explicitamente como funcionalidade do harness.

## Scope

2 plans, 2 waves. Backend isolado primeiro; UI consome depois.

| Wave | Plan | Entrega |
|------|------|---------|
| 1 | `01-PLAN-comparison-endpoint.md` | `GET /api/v1/eval-runs/compare?a=<id>&b=<id>` retorna `{run_a, run_b, comparable, deltas, by_category_deltas, divergent_cases}`. Valida `gold_version` e `status='completed'` |
| 2 | `02-PLAN-comparison-ui-section.md` | Seção "Comparar Execuções" no `/harness` (full-width abaixo da grid 2-col). Selects A e B, tabela com deltas coloridos, breakdown por categoria, lista de divergent cases (collapse) |

## Out of scope

- **N-way (3+) comparação**: 95% do uso real é 2-way ("baseline vs novo modelo"). N-way fragmenta UI sem ganho proporcional.
- **Cost computation real** (token × per-model price): exige catálogo de preços por modelo + tracking correto. Vira onda dedicada.
- **Time-series view** (trend de runs ao longo de N releases). Fora de escopo.
- **Saved comparison presets** ("sempre comparar baseline_v1 vs current"). Pequeno valor agora.

## Decisões assumidas

Confirmadas pelo usuário em diálogo prévio.

1. **2-way apenas** (a, b — duas runs).
2. **Validar `gold_version` igual**: se diferentes → `comparable=false`, UI mostra banner explicando. Não compara números (matematicamente meaningless).
3. **UI: seção dentro de `/harness`**, abaixo da grid 2-col existente. Full-width.
4. **Cost: deferir** pra onda própria. Mostrar só `avg_latency_ms`.
5. **Casos divergentes incluídos**: cruza `details[].case_id`, lista até 20 (regressões primeiro, depois melhorias). É o "ouro" da comparação.
6. **Breakdown por categoria incluído**: já está no `dimension_breakdown.by_category` JSON, só renderizar.

## Must-haves (goal-backward)

- [ ] `GET /eval-runs/compare?a=X&b=Y` com 2 IDs válidos, `gold_version` igual, `status=completed` em ambos → retorna dict com 6 chaves principais.
- [ ] `gold_version` diferente → `comparable=false`, `comparable_reason="datasets diferentes: a=v1, b=v2"`.
- [ ] Run não existente → 404.
- [ ] Run com `status=running` ou `no_cases` → `comparable=false`, reason explícito.
- [ ] Deltas calculados como `(b - a)` para todas métricas numéricas, com `null` quando alguma das duas é null (judge não rodou).
- [ ] Casos divergentes ordenados: regressões (passed_a=true, passed_b=false) antes de melhorias.
- [ ] UI: 2 selects populados pela lista de runs; botão "Comparar" só enabled quando A ≠ B.
- [ ] Banner rose quando `comparable=false`.
- [ ] Tabela de deltas com cores semânticas: verde quando b é melhor, rose quando b regrediu, surface-400 quando null.
- [ ] Direção do "melhor" depende da métrica: ↑ é bom para accuracy/factuality/contract; ↓ é bom para safety_violation_rate/hallucination_rate/latency.
- [ ] Casos divergentes em colapso, mostra ≤ 20 com case_id, category, expected_state, actual_a, actual_b, dim deltas.

## Risks

- **Truncamento de details**: hoje [evaluator.py](app/harness/evaluator.py) salva `details[:100]` no DB. Comparação cruza apenas o que está nos primeiros 100 cases de cada run. Mitigação: documentar limitação na UI ("mostrando até 100 cases dos N totais"). Onda futura pode aumentar o limite ou paginar.
- **judge_used=false em um lado**: dimensões NULL → deltas viram "—" gracefully. UI deve testar e não exibir NaN.
- **Cases ausentes em um dos dois lados** (mesmo gold_version mas alguém rodou parcialmente): cruzamento usa interseção dos case_ids. Documentar.
- **Performance**: 2 details JSON × 100 cases = 200 objetos em memória + cruzamento O(n). Trivial.

## Files touched

| Arquivo | Wave | Tipo |
|---------|------|------|
| `app/routes/dashboard.py` | 1 | edit (endpoint) |
| `app/templates/pages/harness.html` | 2 | edit (UI section) |

Sem novo módulo nem novo schema de DB. Onda pequena (~200 linhas total).
