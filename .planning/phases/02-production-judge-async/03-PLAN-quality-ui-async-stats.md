---
wave: 2
depends_on: [02-PLAN-engine-integration.md]
files_modified:
  - app/routes/dashboard.py
  - app/templates/pages/quality.html
autonomous: true
estimated_diff_lines: ~90
---

# Plan 03 — Stats endpoint + card no /quality

## Objective

Expor os contadores do dispatcher async via `GET /api/v1/dashboard/verifier/async-stats` e mostrar no `/quality` um card "Production Sample" com sample rate corrente, pending, sampled, completed, failed, dropped — alimentado por polling leve (a cada 10s).

## Why

Operador precisa saber:
1. Se o async está ligado e em qual rate (sanity check pra catch config errada).
2. Se as tasks estão completando (judge funcionando) ou falhando (LLM provider problema).
3. Se há drops (cap pequeno demais ou pico de tráfego).
4. Quantas tasks estão na fila agora (saúde do sistema).

Sem isso, o async é uma caixa-preta — ninguém sabe se está funcionando ou silenciosamente falhando.

## Tasks

<task id="1" type="edit">
<file>app/routes/dashboard.py</file>
<location>seção "Harness §9.5" (perto da linha onde estão os endpoints de eval-runs)</location>
<change>
Adicionar endpoint:

```python
@router.get("/verifier/async-stats")
async def verifier_async_stats():
    """Snapshot dos counters do dispatcher async + config corrente.
    
    Cross-worker: cada worker tem contadores próprios (in-process).
    Dashboard mostra os do worker que respondeu o GET.
    """
    from app.verifier.async_dispatcher import stats_snapshot
    from app.core.config import get_settings
    s = get_settings()
    return {
        "stats": stats_snapshot(),
        "config": {
            "enabled": bool(s.verifier_v2_enabled and s.verifier_production_async),
            "sample_rate": s.verifier_production_sample_rate,
            "max_concurrent_jobs": s.verifier_max_concurrent_jobs,
        },
    }
```

Cuidado: import dentro da função para evitar trazer asyncio module-level state ao import time do router (que rodaria antes do lifespan estar pronto).
</change>
<acceptance>
- `GET /api/v1/dashboard/verifier/async-stats` retorna JSON com `stats` (5 chaves) e `config` (3 chaves).
- Funciona mesmo se nunca rodou nenhuma task (todos zero).
- Funciona mesmo se a flag está off (zeros + enabled=false).
</acceptance>
</task>

<task id="2" type="edit">
<file>app/templates/pages/quality.html</file>
<location>após o bloco de stats top cards (depois da linha ~45, antes do "Distribuição por modelo")</location>
<change>
Adicionar novo card antes do `<div class="grid ... mb-4">` da distribuição por modelo:

```html
<!-- ─── Production Sample (async dispatcher) ─── -->
<div x-show="asyncStats.config?.enabled" x-cloak class="rounded-xl border border-surface-200 bg-white p-3 mb-4">
    <div class="flex items-center justify-between mb-2">
        <h3 class="text-[12px] font-semibold text-brand-900">Production Sample</h3>
        <div class="flex items-center gap-3 text-[10px] text-surface-500">
            <span>rate: <span class="font-mono font-bold" :class="asyncStats.config?.sample_rate > 0.5 ? 'text-amber-600' : 'text-brand-700'"
                x-text="((asyncStats.config?.sample_rate||0)*100).toFixed(1)+'%'"></span></span>
            <span>cap: <span class="font-mono" x-text="asyncStats.config?.max_concurrent_jobs"></span></span>
            <button @click="loadAsyncStats()" class="text-brand-500 hover:text-brand-700">⟳ refresh</button>
        </div>
    </div>
    <div class="grid grid-cols-2 lg:grid-cols-5 gap-2">
        <div class="rounded-lg border border-surface-200 px-3 py-2">
            <div class="text-[9px] font-semibold uppercase tracking-wider text-surface-400">Pending</div>
            <div class="text-[16px] font-bold" :class="(asyncStats.stats?.pending||0) > 0 ? 'text-amber-600' : 'text-surface-400'"
                x-text="asyncStats.stats?.pending ?? 0"></div>
        </div>
        <div class="rounded-lg border border-surface-200 px-3 py-2">
            <div class="text-[9px] font-semibold uppercase tracking-wider text-surface-400">Sampled</div>
            <div class="text-[16px] font-bold text-brand-700" x-text="asyncStats.stats?.sampled ?? 0"></div>
        </div>
        <div class="rounded-lg border border-surface-200 px-3 py-2">
            <div class="text-[9px] font-semibold uppercase tracking-wider text-surface-400">Completed</div>
            <div class="text-[16px] font-bold text-emerald-600" x-text="asyncStats.stats?.completed ?? 0"></div>
        </div>
        <div class="rounded-lg border border-surface-200 px-3 py-2">
            <div class="text-[9px] font-semibold uppercase tracking-wider text-surface-400">Failed</div>
            <div class="text-[16px] font-bold" :class="(asyncStats.stats?.failed||0) > 0 ? 'text-rose-600' : 'text-surface-400'"
                x-text="asyncStats.stats?.failed ?? 0"></div>
        </div>
        <div class="rounded-lg border border-surface-200 px-3 py-2" title="Samples descartados por backpressure (set de tasks no cap)">
            <div class="text-[9px] font-semibold uppercase tracking-wider text-surface-400">Dropped</div>
            <div class="text-[16px] font-bold" :class="(asyncStats.stats?.dropped||0) > 0 ? 'text-amber-600' : 'text-surface-400'"
                x-text="asyncStats.stats?.dropped ?? 0"></div>
        </div>
    </div>
    <div class="mt-2 text-[9px] text-surface-400 font-mono">
        Contadores in-process. Cross-worker requer Prometheus/Redis (futuro).
    </div>
</div>

<!-- Mostra hint quando async está OFF — operador percebe que pode habilitar -->
<template x-if="!asyncStats.config?.enabled">
    <div class="rounded-xl border border-surface-100 bg-surface-50/50 p-3 mb-4 text-[11px] text-surface-500">
        <strong>Production sample async</strong> está desabilitado.
        Habilite com <code class="text-[10px] bg-surface-100 px-1 rounded">VERIFIER_PRODUCTION_ASYNC=true</code>
        para amostrar interações reais sem bloquear a resposta.
    </div>
</template>
```
</change>
<acceptance>
- Card mostra todos os 5 contadores quando enabled.
- Hint aparece quando disabled.
- Sample rate em amber se > 50% (visual de alerta).
- Pending em amber quando > 0.
- Failed/Dropped em amber/rose quando > 0; surface-400 quando zero.
- Botão refresh manual funciona.
- Cross-worker disclaimer visível.
</acceptance>
</task>

<task id="3" type="edit">
<file>app/templates/pages/quality.html</file>
<location>Alpine state e métodos do `qualityPage()`</location>
<change>
Adicionar:
- Estado `asyncStats: { stats: {}, config: {} }`.
- Método `loadAsyncStats()` que faz GET no endpoint novo.
- Polling a cada 10s enquanto a página estiver visível, usando `setInterval` registrado em `init()` e limpo (não estritamente necessário em SPA simples, mas sane).
- Chamar `loadAsyncStats()` em `load()` para inicializar.

```js
// dentro do retornado por qualityPage():
asyncStats: { stats: {}, config: {} },
async loadAsyncStats() {
    try {
        const r = await api.get('/api/v1/dashboard/verifier/async-stats');
        this.asyncStats = r || { stats: {}, config: {} };
    } catch {
        this.asyncStats = { stats: {}, config: {} };
    }
},
// Poll a cada 10s
_pollHandle: null,
startPolling() {
    if (this._pollHandle) return;
    this._pollHandle = setInterval(() => this.loadAsyncStats(), 10000);
},
// Adicionar startPolling() no init e chamada em load.
```

E no `x-init` do template, adicionar `startPolling()` após `load()`:
```html
<div x-data="qualityPage()" x-init="load(); startPolling();">
```
</change>
<acceptance>
- Carregamento inicial popula o card.
- A cada 10s, contadores atualizam.
- Sem erros JS no console.
</acceptance>
</task>

## Verification

- [ ] Smoke: `curl http://localhost:8000/api/v1/dashboard/verifier/async-stats` retorna JSON válido com 5 stats + 3 config keys.
- [ ] Manual: com `VERIFIER_PRODUCTION_ASYNC=false`, abrir `/quality` → ver hint de "habilitado=false". Card de stats não aparece.
- [ ] Manual: com `VERIFIER_PRODUCTION_ASYNC=true VERIFIER_PRODUCTION_SAMPLE_RATE=1.0`, rodar 5 interações no `/workspace`, voltar a `/quality` → `sampled=5`, eventualmente `completed=5`, `pending=0`.
- [ ] Manual: com `VERIFIER_PRODUCTION_SAMPLE_RATE=0.6`, ver rate em amber no card.
- [ ] Manual: forçar carga (50 interações em loop) — confirmar que `pending` sobe e desce, `dropped` vira > 0 só quando > cap.

## must_haves

- Operator não precisa abrir DevTools nem JSON cru pra ver saúde do dispatcher.
- Visual consistente com o resto do `/quality` (stat cards no mesmo padrão visual).
- Polling não congestiona — 10s é gentil. Sem websocket.

## Notes

- Considerei websocket pra updates ao vivo. Overkill — polling de 10s é fino para cardview.
- Se operador quiser zerar contadores manualmente, hoje só restartando o worker. Considerar endpoint `POST /verifier/async-stats/reset` numa futura iteração; não escopo desta onda.
- O hint quando disabled é UX adicional — muitas vezes a primeira pergunta do operador é "como ligo isso?". Resposta visível resolve.
