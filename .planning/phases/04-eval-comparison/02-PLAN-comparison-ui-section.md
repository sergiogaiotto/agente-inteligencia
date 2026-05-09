---
wave: 2
depends_on: [01-PLAN-comparison-endpoint.md]
files_modified:
  - app/templates/pages/harness.html
autonomous: true
estimated_diff_lines: ~180
---

# Plan 02 — UI: seção "Comparar Execuções" no /harness

## Objective

Adicionar seção full-width abaixo da grid 2-col existente do `/harness`. Conteúdo:

1. **Seletores**: 2 `<select>` populados pela lista de `evalRuns` (já carregada). Botão "Comparar" desabilita quando A==B ou algum vazio.
2. **Banner de validação**: card rose mostrando `comparable_reason` quando false.
3. **Header com 2 cards lado a lado**: id curto, run_type, gate_result, judge_model, total_cases, gold_version.
4. **Tabela de deltas agregados**: 11 métricas, valor A, valor B, delta com cor (verde/rose/surface por `is_improvement`).
5. **Breakdown por categoria**: tabela colapsável com accuracy + 3 dims, deltas coloridos.
6. **Divergent cases**: lista colapsável (≤20) com expected_state, actual A vs B, dim deltas, badge regressão/melhoria.

## Why

Operador hoje não tem ferramenta visual pra "novo modelo regrediu vs baseline?". Precisa abrir runs separados, decorar, comparar mentalmente. Esta seção transforma a pergunta de release decision numa tabela de uma olhada.

## Tasks

<task id="1" type="edit">
<file>app/templates/pages/harness.html</file>
<location>após o `</div>` da grid 2-col (`<div class="grid lg:grid-cols-2 gap-6">...</div>`), antes do `</div>` final do x-data wrapper</location>
<change>
Inserir seção full-width:

```html
<!-- ─── Comparar Execuções (Onda 5) ─── -->
<div class="mt-6 rounded-xl border border-surface-200 bg-white">
    <div class="flex items-center justify-between border-b border-surface-100 px-5 py-3.5">
        <h2 class="text-[13px] font-semibold text-brand-900">Comparar Execuções</h2>
        <span class="text-[10px] text-surface-400">A vs B — deltas coloridos por métrica</span>
    </div>
    <div class="p-4 space-y-4">
        <!-- Seletores -->
        <div class="flex items-end gap-2 flex-wrap">
            <div class="flex-1 min-w-[200px]">
                <label class="block text-[10px] font-semibold uppercase tracking-wider text-surface-400 mb-1">A (baseline)</label>
                <select x-model="compareForm.a" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-[12px] outline-none">
                    <option value="">— escolha um run —</option>
                    <template x-for="r in evalRuns" :key="r.id">
                        <option :value="r.id" x-text="(r.run_type || '') + ' · ' + (r.gate_result || r.status || '?') + ' · ' + (r.id||'').substring(0,8)"></option>
                    </template>
                </select>
            </div>
            <div class="flex-1 min-w-[200px]">
                <label class="block text-[10px] font-semibold uppercase tracking-wider text-surface-400 mb-1">B (novo)</label>
                <select x-model="compareForm.b" class="w-full rounded-lg border border-surface-200 bg-white px-3 py-2 text-[12px] outline-none">
                    <option value="">— escolha um run —</option>
                    <template x-for="r in evalRuns" :key="r.id">
                        <option :value="r.id" x-text="(r.run_type || '') + ' · ' + (r.gate_result || r.status || '?') + ' · ' + (r.id||'').substring(0,8)"></option>
                    </template>
                </select>
            </div>
            <button @click="runCompare()"
                :disabled="!compareForm.a || !compareForm.b || compareForm.a === compareForm.b || compareLoading"
                class="rounded-lg bg-brand-500 px-4 py-2 text-[12px] font-semibold text-white hover:bg-brand-600 disabled:opacity-40 disabled:cursor-not-allowed">
                <span x-text="compareLoading ? 'Comparando...' : 'Comparar'"></span>
            </button>
        </div>

        <!-- Estado: nenhum compare ainda -->
        <template x-if="!compareResult && !compareLoading">
            <p class="text-[11px] text-surface-400">Escolha 2 execuções diferentes para ver os deltas.</p>
        </template>

        <!-- Estado: incompatível -->
        <template x-if="compareResult && !compareResult.comparable">
            <div class="rounded-lg border border-rose-200 bg-rose-50 p-3 text-[12px] text-rose-800">
                <strong>Não é possível comparar.</strong>
                <span class="block mt-1 text-[11px]" x-text="compareResult.comparable_reason || ''"></span>
            </div>
        </template>

        <!-- Resultado -->
        <template x-if="compareResult && compareResult.comparable">
            <div class="space-y-4">
                <!-- Header com 2 cards lado a lado -->
                <div class="grid md:grid-cols-2 gap-3">
                    <template x-for="(side, i) in ['a','b']" :key="side">
                        <div class="rounded-lg border-2 p-3"
                             :class="side==='a' ? 'border-brand-200 bg-brand-50/30' : 'border-emerald-200 bg-emerald-50/30'">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[10px] font-bold uppercase tracking-wider"
                                      :class="side==='a' ? 'text-brand-700' : 'text-emerald-700'"
                                      x-text="side.toUpperCase() + (side==='a' ? ' — baseline' : ' — novo')"></span>
                                <span class="rounded-full px-2 py-0.5 text-[9px] font-bold"
                                      :class="compareResult['run_'+side].gate_result==='approved' ? 'bg-emerald-100 text-emerald-700' :
                                              compareResult['run_'+side].gate_result==='rejected' ? 'bg-rose-100 text-rose-700' :
                                              'bg-surface-100 text-surface-500'"
                                      x-text="compareResult['run_'+side].gate_result || compareResult['run_'+side].status"></span>
                            </div>
                            <div class="space-y-1 text-[11px]">
                                <div class="flex justify-between"><span class="text-surface-400">id</span><span class="font-mono text-brand-900" x-text="(compareResult['run_'+side].id || '').substring(0,8)"></span></div>
                                <div class="flex justify-between"><span class="text-surface-400">run_type</span><span class="text-brand-900" x-text="compareResult['run_'+side].run_type || '—'"></span></div>
                                <div class="flex justify-between"><span class="text-surface-400">gold</span><span class="font-mono text-brand-900" x-text="compareResult['run_'+side].gold_version || '—'"></span></div>
                                <div class="flex justify-between"><span class="text-surface-400">cases</span><span class="text-brand-900" x-text="(compareResult['run_'+side].passed||0)+'/'+(compareResult['run_'+side].total_cases||0)"></span></div>
                                <div class="flex justify-between"><span class="text-surface-400">judge</span><span class="font-mono text-brand-900 text-[10px]" x-text="compareResult['run_'+side].judge_used ? (compareResult['run_'+side].judge_model || 'usado') : 'no-judge'"></span></div>
                            </div>
                        </div>
                    </template>
                </div>

                <!-- Deltas agregados -->
                <div class="rounded-lg border border-surface-200">
                    <div class="border-b border-surface-100 px-3 py-2 text-[11px] font-semibold text-brand-900">Deltas agregados</div>
                    <table class="w-full text-[11px]">
                        <thead class="bg-surface-50 text-surface-400">
                            <tr>
                                <th class="text-left px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">Métrica</th>
                                <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">A</th>
                                <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">B</th>
                                <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">Δ</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-surface-50">
                            <template x-for="m in metricsOrder" :key="m">
                                <tr x-show="compareResult.deltas[m]">
                                    <td class="px-3 py-1.5 text-brand-900" x-text="metricLabel(m)"></td>
                                    <td class="px-3 py-1.5 text-right font-mono" x-text="fmtMetric(m, compareResult.deltas[m]?.a)"></td>
                                    <td class="px-3 py-1.5 text-right font-mono" x-text="fmtMetric(m, compareResult.deltas[m]?.b)"></td>
                                    <td class="px-3 py-1.5 text-right font-mono font-bold"
                                        :class="deltaColor(compareResult.deltas[m])"
                                        x-text="fmtDelta(m, compareResult.deltas[m]?.delta, compareResult.deltas[m]?.is_improvement)"></td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                </div>

                <!-- Breakdown por categoria -->
                <div class="rounded-lg border border-surface-200">
                    <button @click="showCategoryBreakdown = !showCategoryBreakdown"
                        class="w-full flex items-center justify-between border-b border-surface-100 px-3 py-2 hover:bg-surface-50">
                        <span class="text-[11px] font-semibold text-brand-900">Por categoria
                            <span class="text-surface-400 font-normal" x-text="'(' + Object.keys(compareResult.by_category_deltas || {}).length + ')'"></span>
                        </span>
                        <svg class="w-3 h-3 text-surface-400 transition-transform" :class="showCategoryBreakdown ? 'rotate-180' : ''" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19.5 8.25l-7.5 7.5-7.5-7.5"/></svg>
                    </button>
                    <div x-show="showCategoryBreakdown" x-transition.duration.150ms class="overflow-x-auto">
                        <table class="w-full text-[11px]">
                            <thead class="bg-surface-50 text-surface-400">
                                <tr>
                                    <th class="text-left px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">Categoria</th>
                                    <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">Cases A/B</th>
                                    <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">Acc Δ</th>
                                    <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">F Δ</th>
                                    <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">C Δ</th>
                                    <th class="text-right px-3 py-1.5 font-semibold uppercase tracking-wider text-[9px]">T Δ</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-surface-50">
                                <template x-for="[cat, b] in Object.entries(compareResult.by_category_deltas || {})" :key="cat">
                                    <tr>
                                        <td class="px-3 py-1.5 text-brand-900 truncate max-w-[200px]" x-text="cat"></td>
                                        <td class="px-3 py-1.5 text-right text-surface-500 font-mono text-[10px]"
                                            x-text="((b.passed_a||0)+'/'+(b.total_a||0)) + ' · ' + ((b.passed_b||0)+'/'+(b.total_b||0))"></td>
                                        <td class="px-3 py-1.5 text-right font-mono font-bold"
                                            :class="deltaColor(b.accuracy)"
                                            x-text="fmtDelta('accuracy', b.accuracy?.delta, b.accuracy?.is_improvement)"></td>
                                        <td class="px-3 py-1.5 text-right font-mono"
                                            :class="deltaColor(b.avg_factuality)"
                                            x-text="fmtDelta('avg_factuality', b.avg_factuality?.delta, b.avg_factuality?.is_improvement)"></td>
                                        <td class="px-3 py-1.5 text-right font-mono"
                                            :class="deltaColor(b.avg_completeness)"
                                            x-text="fmtDelta('avg_completeness', b.avg_completeness?.delta, b.avg_completeness?.is_improvement)"></td>
                                        <td class="px-3 py-1.5 text-right font-mono"
                                            :class="deltaColor(b.avg_tone)"
                                            x-text="fmtDelta('avg_tone', b.avg_tone?.delta, b.avg_tone?.is_improvement)"></td>
                                    </tr>
                                </template>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Divergent cases -->
                <div class="rounded-lg border border-surface-200">
                    <button @click="showDivergent = !showDivergent"
                        class="w-full flex items-center justify-between border-b border-surface-100 px-3 py-2 hover:bg-surface-50">
                        <span class="text-[11px] font-semibold text-brand-900">Casos divergentes
                            <span class="text-surface-400 font-normal" x-text="'(' + (compareResult.divergent_cases || []).length + ' até 20 mostrados; regressões primeiro)'"></span>
                        </span>
                        <svg class="w-3 h-3 text-surface-400 transition-transform" :class="showDivergent ? 'rotate-180' : ''" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19.5 8.25l-7.5 7.5-7.5-7.5"/></svg>
                    </button>
                    <div x-show="showDivergent" x-transition.duration.150ms class="divide-y divide-surface-50 max-h-[40vh] overflow-y-auto scrollbar-thin">
                        <template x-if="(compareResult.divergent_cases || []).length === 0">
                            <p class="px-3 py-3 text-[11px] text-surface-400">Nenhum caso divergente — A e B concordaram em todos os cases comuns.</p>
                        </template>
                        <template x-for="d in compareResult.divergent_cases || []" :key="d.case_id">
                            <div class="px-3 py-2 text-[11px]">
                                <div class="flex items-center gap-2 flex-wrap mb-1">
                                    <span class="rounded-full px-2 py-0.5 text-[9px] font-bold"
                                          :class="d.regression ? 'bg-rose-100 text-rose-700' : 'bg-emerald-100 text-emerald-700'"
                                          x-text="d.regression ? 'regressão' : 'melhoria'"></span>
                                    <span class="rounded-full bg-violet-50 px-2 py-0.5 text-[10px] font-medium text-violet-700" x-text="d.category"></span>
                                    <span class="rounded-full bg-brand-50 px-2 py-0.5 text-[10px] font-medium text-brand-700" x-text="'esperado: ' + (d.expected_state || '?')"></span>
                                    <span class="text-[9px] text-surface-400 font-mono ml-auto" x-text="(d.case_id||'').substring(0,12)"></span>
                                </div>
                                <div class="grid grid-cols-2 gap-2 text-[10px]">
                                    <div class="rounded border border-brand-100 bg-brand-50/30 p-1.5">
                                        <div class="font-bold text-brand-700 mb-0.5">A — <span x-text="d.a.passed ? '✓ passou' : '✗ falhou'"></span></div>
                                        <div class="text-surface-600">state: <span class="font-mono" x-text="d.a.actual_state || '—'"></span></div>
                                        <div class="text-surface-500" x-text="dimsLine(d.a)"></div>
                                        <template x-if="(d.a.failure_reasons||[]).length > 0">
                                            <div class="text-rose-700 text-[9px] mt-0.5" x-text="d.a.failure_reasons.join(', ')"></div>
                                        </template>
                                    </div>
                                    <div class="rounded border border-emerald-100 bg-emerald-50/30 p-1.5">
                                        <div class="font-bold text-emerald-700 mb-0.5">B — <span x-text="d.b.passed ? '✓ passou' : '✗ falhou'"></span></div>
                                        <div class="text-surface-600">state: <span class="font-mono" x-text="d.b.actual_state || '—'"></span></div>
                                        <div class="text-surface-500" x-text="dimsLine(d.b)"></div>
                                        <template x-if="(d.b.failure_reasons||[]).length > 0">
                                            <div class="text-rose-700 text-[9px] mt-0.5" x-text="d.b.failure_reasons.join(', ')"></div>
                                        </template>
                                    </div>
                                </div>
                            </div>
                        </template>
                    </div>
                </div>
            </div>
        </template>
    </div>
</div>
```
</change>
<acceptance>
- Seção aparece abaixo da grid 2-col existente.
- Selects mostram run_type · gate · id-curto pra cada run.
- Botão Comparar bloqueia em A==B, A vazio, B vazio, ou compareLoading.
- Banner rose quando comparable=false.
- 2 header cards lado a lado, esquerda brand, direita emerald.
- Tabela de deltas agregados com 11 métricas.
- Breakdown por categoria colapsável.
- Divergent cases colapsável, regressões com badge rose, melhorias emerald.
</acceptance>
</task>

<task id="2" type="edit">
<file>app/templates/pages/harness.html</file>
<location>Alpine state e métodos do `harnessPage()`</location>
<change>
Adicionar:

```js
// Onda 5: comparação side-by-side
compareForm: { a: '', b: '' },
compareResult: null,
compareLoading: false,
showCategoryBreakdown: false,
showDivergent: true,  // default open — é o "ouro" da comparação

// Ordem fixa das métricas na tabela de deltas (controla ordem na UI)
metricsOrder: [
    'accuracy', 'accuracy_unweighted',
    'avg_factuality', 'avg_completeness', 'avg_tone',
    'contract_compliance_rate', 'correct_refusal_rate',
    'safety_violation_rate', 'hallucination_rate', 'false_positive_rate',
    'avg_latency_ms',
],

metricLabel(m) {
    const labels = {
        accuracy: 'Acurácia ponderada',
        accuracy_unweighted: 'Acurácia bruta',
        avg_factuality: 'Factuality (média)',
        avg_completeness: 'Completeness (média)',
        avg_tone: 'Tone (média)',
        contract_compliance_rate: 'Contract compliance',
        correct_refusal_rate: 'Refusal correto',
        safety_violation_rate: 'Safety violations',
        hallucination_rate: 'Alucinação',
        false_positive_rate: 'Falso positivo',
        avg_latency_ms: 'Latência (ms)',
    };
    return labels[m] || m;
},

fmtMetric(m, v) {
    if (v == null) return '—';
    if (m === 'avg_latency_ms') return Number(v).toFixed(0) + 'ms';
    if (m.startsWith('avg_factuality') || m.startsWith('avg_completeness') || m.startsWith('avg_tone')) {
        return Number(v).toFixed(2);
    }
    if (m.endsWith('_rate') || m.startsWith('accuracy')) {
        return (Number(v) * 100).toFixed(1) + '%';
    }
    return Number(v).toFixed(2);
},

fmtDelta(m, delta, isImprovement) {
    if (delta == null) return '—';
    const sign = delta > 0 ? '+' : '';
    if (m === 'avg_latency_ms') return sign + Number(delta).toFixed(0) + 'ms';
    if (m.startsWith('avg_')) return sign + Number(delta).toFixed(2);
    if (m.endsWith('_rate') || m.startsWith('accuracy')) return sign + (Number(delta) * 100).toFixed(1) + 'pp';
    return sign + Number(delta).toFixed(2);
},

deltaColor(deltaObj) {
    if (!deltaObj || deltaObj.delta == null) return 'text-surface-400';
    if (deltaObj.is_improvement === true) return 'text-emerald-600';
    if (deltaObj.is_improvement === false) return 'text-rose-600';
    return 'text-surface-500'; // delta == 0 ou indeterminado
},

dimsLine(side) {
    const parts = [];
    if (side.factuality != null) parts.push('F' + Number(side.factuality).toFixed(1));
    if (side.completeness != null) parts.push('C' + Number(side.completeness).toFixed(1));
    if (side.tone != null) parts.push('T' + Number(side.tone).toFixed(1));
    if (side.safety != null) parts.push('S' + side.safety);
    return parts.length ? 'dims: ' + parts.join(' ') : 'sem dims';
},

async runCompare() {
    if (!this.compareForm.a || !this.compareForm.b) return;
    if (this.compareForm.a === this.compareForm.b) {
        showToast('Escolha dois runs diferentes', 'error');
        return;
    }
    this.compareLoading = true;
    try {
        const url = `/api/v1/eval-runs/compare?a=${encodeURIComponent(this.compareForm.a)}&b=${encodeURIComponent(this.compareForm.b)}`;
        this.compareResult = await api.get(url);
    } catch (e) {
        showToast('Erro ao comparar: ' + (e?.message || 'desconhecido'), 'error');
        this.compareResult = null;
    } finally {
        this.compareLoading = false;
    }
},
```
</change>
<acceptance>
- 8 propriedades novas no state (compareForm, compareResult, compareLoading, showCategoryBreakdown, showDivergent, metricsOrder, mais helpers como métodos).
- 6 helpers JS (metricLabel, fmtMetric, fmtDelta, deltaColor, dimsLine, runCompare).
- runCompare valida A≠B, mostra toast em erro, sem JS errors.
- fmtDelta sufixos corretos: pp pra rates, ms pra latency, raw pra dims.
</acceptance>
</task>

## Verification

- [ ] Manual: rodar 2 baseline runs (idealmente com modelos diferentes), abrir `/harness`, escolher A=run1, B=run2, clicar Comparar → ver header lado a lado + tabela de deltas + breakdown + divergent cases.
- [ ] Manual: escolher A==B → botão disabled.
- [ ] Manual: rodar 1 run com VERIFIER_PRODUCTION_ASYNC=false (sync judge) e outro com judge_used=false → comparable=true mas dimensões mostram "—" no lado sem judge.
- [ ] Manual: rodar 2 runs em datasets diferentes → banner rose com reason explicativo.
- [ ] Manual: confirmar que cores: melhor verde, pior rose, indeterminado/zero surface.

## must_haves

- Operador entende em < 30s qual run foi melhor e em quais dimensões.
- Cases divergentes mostram falha por DIM (qual dimensão fez B regredir).
- Sem JS errors em qualquer combinação.

## Notes

- `showDivergent: true` por default — operador raramente quer fechar isso, é o "ouro" da feature.
- `showCategoryBreakdown: false` — muitas categorias bagunçam a vista; abre quando quer drill-down.
- `metricsOrder` controla ordem na tabela; é fixo e não vem do backend pra desacoplar UI da estrutura de `_METRIC_DIRECTIONS`.
