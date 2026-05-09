---
wave: 2
depends_on: [02-PLAN-harness-multidim-gate.md]
files_modified:
  - app/templates/pages/harness.html
autonomous: true
estimated_diff_lines: ~120
---

# Plan 03 — UI do Harness mostra dimensões

## Objective

A página `/harness` deve mostrar, para cada execução de avaliação, as dimensões agregadas (F/C/T/S) com mini-badges coloridas e, no detalhe expansível, breakdown por categoria + top-10 unsupported_claims agregadas + razão do gate. Mesmo padrão visual da página `/quality` (já estabelecido).

## Why

- Operador hoje vê só `accuracy + passed/total + latency` na lista de execuções. Não sabe se `accuracy=0.85` veio com `factuality=4.2` (ótimo) ou `factuality=2.1` (ruim).
- Quando o gate reprova, hoje aparece só `"rejected"` — sem motivo. Operador precisa abrir o JSON do `details` para investigar.
- Decisão de release acontece olhando essa página; precisa de toda a informação à vista.

## Tasks

<task id="1" type="edit">
<file>app/templates/pages/harness.html</file>
<location>card "Execuções de Avaliação", template `x-for="r in evalRuns"` (linha ~114-128)</location>
<change>
Estender cada item da lista para mostrar mini-badges de dimensões. Layout proposto (manter compacto):

```html
<div class="px-5 py-3" @click="toggleEval(r)">
  <!-- linha 1: tipo + gate -->
  <div class="flex items-center justify-between mb-1">
    <span class="text-[12px] font-medium text-brand-900" x-text="r.run_type + ' — ' + (r.gate_result||r.status)"></span>
    <span class="rounded-full px-2 py-0.5 text-[10px] font-bold"
          :class="r.gate_result==='approved'?'bg-emerald-50 text-emerald-700':r.gate_result==='rejected'?'bg-rose-50 text-rose-700':'bg-amber-50 text-amber-700'"
          x-text="r.gate_result||r.status"></span>
  </div>
  <!-- linha 2: métricas legacy + badges multi-dim -->
  <div class="flex flex-wrap gap-3 text-[11px] text-surface-400 items-center">
    <span x-text="'Acurácia: '+(r.accuracy*100).toFixed(1)+'%'"></span>
    <span x-text="r.passed+'/'+r.total_cases+' passed'"></span>
    <span x-text="(r.avg_latency_ms||0).toFixed(0)+'ms avg'"></span>
    <template x-if="r.judge_used">
      <span class="flex items-center gap-1">
        <span :class="dimColor(r.avg_factuality)" x-text="'F'+fmtDim(r.avg_factuality)" title="factuality média"></span>
        <span :class="dimColor(r.avg_completeness)" x-text="'C'+fmtDim(r.avg_completeness)" title="completeness"></span>
        <span :class="dimColor(r.avg_tone)" x-text="'T'+fmtDim(r.avg_tone)" title="tone"></span>
        <span :class="(r.safety_violation_rate||0) <= 0.05 ? 'text-emerald-600' : 'text-rose-600'"
              x-text="'S'+((1-(r.safety_violation_rate||0))*100).toFixed(0)+'%'" title="safety pass rate"></span>
        <template x-if="(r.hallucination_rate||0) > 0">
          <span class="rounded bg-rose-100 px-1.5 py-0.5 text-[9px] font-bold text-rose-700"
                :title="'casos com unsupported_claims'"
                x-text="'⚑'+((r.hallucination_rate||0)*100).toFixed(0)+'%'"></span>
        </template>
      </span>
    </template>
    <template x-if="!r.judge_used">
      <span class="rounded bg-surface-100 px-1.5 py-0.5 text-[9px] text-surface-500" title="harness rodou sem multi-dim verifier">no-judge</span>
    </template>
  </div>
  <!-- linha 3: gate_reason quando rejected -->
  <template x-if="r.gate_result==='rejected' && r.gate_reason">
    <div class="mt-1 text-[10px] text-rose-700 font-mono" x-text="'⚠ '+r.gate_reason"></div>
  </template>
</div>
```

Reutilizar `dimColor` e `fmtDim` do `/quality` — copiá-los para o script desta página.
</change>
<acceptance>
- Mini-badges aparecem só quando `judge_used=true`.
- Cores: ≥4 verde, ≥3 brand, ≥2 amber, <2 rose (igual /quality).
- Quando `gate_result=rejected`, `gate_reason` aparece embaixo em fonte mono pequena rose.
- Layout não quebra em telas estreitas (flex-wrap).
</acceptance>
</task>

<task id="2" type="edit">
<file>app/templates/pages/harness.html</file>
<location>após o template do item da lista (mesmo card)</location>
<change>
Adicionar área expansível por execução (mesmo padrão `expanded === r.id` do `/quality`):

```html
<div x-show="expandedEval === r.id" x-transition.duration.150ms class="border-t border-surface-100 bg-surface-50/50 px-5 py-3 text-[11px]">
  <!-- breakdown por categoria -->
  <template x-if="r.dimension_breakdown && r.dimension_breakdown.by_category">
    <div class="space-y-1.5 mb-3">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-surface-400">Por categoria</div>
      <template x-for="[cat, b] in Object.entries(r.dimension_breakdown.by_category||{})" :key="cat">
        <div class="grid grid-cols-6 gap-2 items-center">
          <span class="col-span-2 text-brand-900 truncate" x-text="cat"></span>
          <span class="text-surface-500" x-text="b.passed+'/'+b.total"></span>
          <span :class="dimColor(b.avg_factuality)" x-text="'F'+fmtDim(b.avg_factuality)"></span>
          <span :class="dimColor(b.avg_completeness)" x-text="'C'+fmtDim(b.avg_completeness)"></span>
          <span :class="dimColor(b.avg_tone)" x-text="'T'+fmtDim(b.avg_tone)"></span>
        </div>
      </template>
    </div>
  </template>
  <!-- top unsupported claims -->
  <template x-if="(r.dimension_breakdown?.top_unsupported_claims || []).length > 0">
    <div class="rounded border border-rose-200 bg-rose-50/50 p-2 mb-2">
      <div class="text-[10px] font-bold text-rose-700 mb-1">⚑ Top unsupported_claims (alucinação suspeita)</div>
      <ul class="list-disc pl-4 space-y-0.5 text-rose-900">
        <template x-for="c in r.dimension_breakdown.top_unsupported_claims" :key="c">
          <li x-text="c"></li>
        </template>
      </ul>
    </div>
  </template>
  <div class="text-[9px] text-surface-400 font-mono">
    judge_model=<span x-text="r.judge_model || '—'"></span>
    · skipped=<span x-text="r.dimension_breakdown?.skipped_cases ?? 0"></span>
    · created=<span x-text="r.created_at?.replace('T',' ').substring(0,16) || '—'"></span>
  </div>
</div>
```

Para isso funcionar: o `dimension_breakdown` precisa vir do backend já com `by_category[cat].avg_factuality/completeness/tone` e com `top_unsupported_claims` (lista deduplicada, top 10 por frequência). Se não vier, anotar follow-up no plan 02 (mas o plan 02 já prevê o JSON; o que falta é incluir `top_unsupported_claims` no agregado).
</change>
<acceptance>
- Área expansível abre/fecha ao clicar.
- Quando `dimension_breakdown` está vazio (run legacy), área mostra apenas o footer com `judge_model=—` e nada de breakdown.
- Layout sólido sem JS errors no console.
</acceptance>
</task>

<task id="3" type="edit">
<file>app/harness/evaluator.py</file>
<location>seção de agregação dentro de `run_evaluation`</location>
<change>
**Patch ao Plan 02**: incluir `top_unsupported_claims` no `dimension_breakdown`:

```python
from collections import Counter
all_claims = []
for d in details:
    all_claims.extend(d.get("unsupported_claims") or [])
top_unsupported = [claim for claim, _ in Counter(all_claims).most_common(10)]

dimension_breakdown = {
    "by_category": {
        cat: {
            "total": b["total"],
            "passed": b["passed"],
            "avg_factuality": _safe_mean(b.get("dim_factuality", [])),
            "avg_completeness": _safe_mean(b.get("dim_completeness", [])),
            "avg_tone": _safe_mean(b.get("dim_tone", [])),
        }
        for cat, b in by_category.items()
    },
    "top_unsupported_claims": top_unsupported,
    "skipped_cases": sum(1 for d in details if d.get("dim_skipped")),
}
```

Este patch deve ser feito **junto** com o Plan 02; cito aqui para referência. Se o Plan 02 já incluir, este task vira no-op.
</change>
<acceptance>
- `result["dimension_breakdown"]["top_unsupported_claims"]` existe e tem ≤ 10 itens distintos.
- Não quebra quando nenhum caso teve unsupported claim.
</acceptance>
</task>

<task id="4" type="edit">
<file>app/templates/pages/harness.html</file>
<location>bloco `<script>` no fim (linha ~136-198)</location>
<change>
Adicionar:

1. Estado `expandedEval: null` no objeto Alpine.
2. Método `toggleEval(r)` que faz `this.expandedEval = (this.expandedEval === r.id) ? null : r.id`.
3. Funções `fmtDim(v)` e `dimColor(v)` (copiar do `/quality`).
4. Em `load()`, parsing defensivo: se `r.dimension_breakdown` for string, `JSON.parse`. Idem para `details`.
</change>
<acceptance>
- Click no item da lista expande/colapsa.
- `dimension_breakdown` é objeto, não string, no momento de renderizar.
- Compatível com runs antigos (sem judge): não quebra, só mostra menos.
</acceptance>
</task>

## Verification

- [ ] Manual: rodar harness com judge ligado num release qualquer, abrir `/harness`, ver mini-badges F/C/T/S na lista.
- [ ] Manual: clicar num item → área expande mostrando breakdown por categoria.
- [ ] Manual: forçar gate rejection (baixar `harness_min_avg_factuality` para `4.5` em env), rodar, ver `gate_reason` em vermelho na linha.
- [ ] Manual: rodar com `HARNESS_USE_VERIFIER=false` → mini-badges não aparecem, badge `no-judge` em surface.
- [ ] Browser DevTools console limpo (zero JS errors em qualquer estado: empty, no-judge, judge-success, judge-rejection).
- [ ] Layout testado em viewport ≥ 1280px e ≤ 768px (flex-wrap não quebra).

## must_haves

- Operador consegue, sem abrir DevTools nem JSON cru, identificar:
  - Se uma execução usou judge ou não.
  - Qual dimensão puxou o gate para baixo.
  - Quais categorias do Golden Dataset estão regredindo.
  - Quais claims sem suporte aparecem com mais frequência.
- Visual consistente com `/quality` (cores, badges, padding).

## Notes

- Não criar componente Alpine compartilhado entre `/harness` e `/quality` para `dimColor/fmtDim` agora — duplicar é OK; refatorar quando aparecer um terceiro consumidor (regra de 3).
- Não adicionar export CSV de execução — fora de escopo.
- Botão "Executar Harness" hoje não mostra estimativa de custo. Anotar como melhoria de UX **fora desta onda**.
