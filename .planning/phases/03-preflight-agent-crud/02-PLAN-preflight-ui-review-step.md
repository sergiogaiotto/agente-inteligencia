---
wave: 2
depends_on: [01-PLAN-preflight-checks-backend.md]
files_modified:
  - app/templates/pages/agent_form.html
autonomous: true
estimated_diff_lines: ~110
---

# Plan 02 — UI: lista de checks na step "Revisão"

## Objective

Adicionar bloco de pre-flight na step "Revisão" do `agent_form.html`. Bloco mostra lista de checks por severidade, fix hint clicável quando aplicável, ícone visual, contadores no topo. Auto-roda ao entrar na step. Botão "Salvar" desabilita quando há `blocked=true`. Botão "Verificar novamente" para re-rodar manualmente.

## Why

Step "Revisão" hoje é cosmético — só mostra sumário do form. Pre-flight transforma a step no real "are you sure?": operador vê todos os problemas de configuração antes de salvar, com hint pra cada um.

## Tasks

<task id="1" type="edit">
<file>app/templates/pages/agent_form.html</file>
<location>step 4 (Revisão), antes do bloco "Pass-through banner" (linha ~265)</location>
<change>
Inserir bloco de pre-flight no topo da step Revisão:

```html
<!-- ─── Pre-flight checks ─── -->
<div x-show="step === 3" class="mb-4">
    <div class="rounded-xl border-2 p-4"
         :class="preflight.loading ? 'border-surface-200 bg-surface-50' :
                 preflight.report?.blocked ? 'border-rose-300 bg-rose-50/50' :
                 (preflight.report?.has_warnings ? 'border-amber-300 bg-amber-50/50' :
                  'border-emerald-300 bg-emerald-50/50')">
        <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-2">
                <span class="text-[14px]" x-text="preflight.loading ? '⟳' :
                    preflight.report?.blocked ? '✗' :
                    (preflight.report?.has_warnings ? '⚠' : '✓')"></span>
                <h3 class="text-[13px] font-bold"
                    :class="preflight.report?.blocked ? 'text-rose-700' :
                            preflight.report?.has_warnings ? 'text-amber-700' :
                            'text-emerald-700'"
                    x-text="preflight.loading ? 'Verificando configuração...' :
                            preflight.report?.blocked ? 'Configuração com erros — corrija antes de salvar' :
                            preflight.report?.has_warnings ? 'Configuração com avisos — pode salvar' :
                            'Configuração OK'"></h3>
            </div>
            <button @click="runPreflight()" :disabled="preflight.loading"
                class="text-[11px] text-brand-500 hover:text-brand-700 disabled:opacity-30">
                ⟳ Verificar novamente
            </button>
        </div>
        <template x-if="!preflight.loading && (preflight.report?.checks || []).length > 0">
            <ul class="space-y-1.5 mt-3">
                <template x-for="c in preflight.report.checks" :key="c.id">
                    <li class="flex items-start gap-2 text-[12px]">
                        <span class="text-[14px] shrink-0"
                              :class="c.severity==='error' ? 'text-rose-600' :
                                      c.severity==='warning' ? 'text-amber-600' :
                                      'text-brand-500'"
                              x-text="c.severity==='error' ? '✗' : c.severity==='warning' ? '⚠' : 'ℹ'"></span>
                        <div class="flex-1">
                            <div class="font-semibold"
                                 :class="c.severity==='error' ? 'text-rose-900' :
                                         c.severity==='warning' ? 'text-amber-900' :
                                         'text-brand-900'"
                                 x-text="c.title"></div>
                            <div class="text-[11px] text-surface-600 mt-0.5" x-text="c.detail"></div>
                            <template x-if="c.fix_hint">
                                <a :href="c.fix_hint" target="_blank"
                                   class="inline-block mt-1 text-[10px] text-brand-500 hover:text-brand-700 font-mono"
                                   x-text="'→ ' + c.fix_hint"></a>
                            </template>
                        </div>
                        <span class="text-[9px] text-surface-400 font-mono shrink-0" x-text="c.id"></span>
                    </li>
                </template>
            </ul>
        </template>
        <template x-if="!preflight.loading && (preflight.report?.checks || []).length === 0">
            <p class="text-[11px] text-emerald-700 mt-2">9 checks executados, todos limpos.</p>
        </template>
    </div>
</div>
```
</change>
<acceptance>
- Visual coerente com brand (rounded-xl, border-2, padding consistente).
- Loading state aparece imediatamente ao entrar na step (antes do response chegar).
- Cores por severidade: error=rose, warning=amber, info=brand.
- Lista vazia → "9 checks executados, todos limpos" em emerald.
- Fix hint vira link clicável (target=_blank pra não perder o form).
</acceptance>
</task>

<task id="2" type="edit">
<file>app/templates/pages/agent_form.html</file>
<location>botão Salvar (linha ~314)</location>
<change>
Modificar pra desabilitar quando há blocked:

```html
<button x-show="step === steps.length - 1" @click="save()"
    :disabled="saving || preflight.report?.blocked"
    :title="preflight.report?.blocked ? 'Corrija os erros do pre-flight antes de salvar' : ''"
    class="rounded-lg bg-emerald-500 px-5 py-2 text-[13px] font-semibold text-white hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors">
    <span x-text="saving ? 'Salvando...' : (isEdit ? 'Atualizar' : 'Criar Agente')"></span>
</button>
```

`saving` continua bloqueando durante o request; `preflight.report?.blocked` adiciona o gate de configuração.
</change>
<acceptance>
- Botão fica disabled visual + clique ignorado quando blocked.
- Tooltip explica o motivo.
- Ainda funciona normalmente quando preflight é null (loading inicial não bloqueia o save).
</acceptance>
</task>

<task id="3" type="edit">
<file>app/templates/pages/agent_form.html</file>
<location>Alpine state e métodos do `agentForm()`</location>
<change>
Adicionar estado e watcher:

```js
// dentro do retornado por agentForm():
preflight: { loading: false, report: null },

async runPreflight() {
    this.preflight.loading = true;
    try {
        // Constrói payload exato do AgentCreate (drop campos não-aceitos)
        const payload = {
            name: this.form.name || '',
            description: this.form.description || null,
            kind: this.form.kind || 'subagent',
            domain: this.form.domain || null,
            skill_id: this.form.skill_id || null,
            llm_provider: this.form.llm_provider || 'openai',
            model: this.form.model || '',
            system_prompt: this.form.system_prompt || '',
            version: this.form.version || '1.0.0',
            require_evidence: !!this.form.require_evidence,
            temperature: Number(this.form.temperature) || 0.7,
            accepts_images: !!this.form.accepts_images,
            accepts_documents: !!this.form.accepts_documents,
        };
        const r = await api.post('/api/v1/agents/preflight', payload);
        this.preflight.report = r;
    } catch (e) {
        // 422 ou erro de rede — mostra um check sintético pro user perceber
        this.preflight.report = {
            checks: [{
                id: 'preflight_error', severity: 'warning',
                title: 'Não foi possível rodar o pre-flight',
                detail: (e?.message || 'Erro desconhecido') + ' — você pode salvar mesmo assim.',
                fix_hint: null, field: null,
            }],
            has_errors: false, has_warnings: true, blocked: false,
        };
    } finally {
        this.preflight.loading = false;
    }
},
```

E modificar transição pra step Revisão pra disparar runPreflight automaticamente. Adicionar `$watch` no `step`:

```js
init() {
    // dentro do init existente OU adicionar agora:
    this.$watch('step', (newStep) => {
        if (newStep === 3) this.runPreflight();
    });
},
```

NOTA: `agentForm()` já usa `x-init="load()"`. Adicionar uma chamada explícita lá em vez do `$watch` é alternativa. Recomendo `$watch` (limpo, declarativo). Se `init()` não existe no objeto Alpine atual, criá-lo e mover lógica relevante.
</change>
<acceptance>
- Entrar na step Revisão dispara runPreflight automaticamente.
- Botão "Verificar novamente" re-roda.
- Erros de rede não derrubam a UI — mostra check sintético.
</acceptance>
</task>

<task id="4" type="edit">
<file>app/templates/pages/agent_form.html</file>
<location>função `save()` (linha ~467)</location>
<change>
Antes de fazer o request real, re-rodar preflight uma última vez (defesa: usuário pode ter mudado algo após o último auto-run):

```js
async save() {
    if (!this.form.name.trim()) { showToast('Nome obrigatório', 'error'); this.step = 0; return; }
    // Re-roda preflight no momento do save
    await this.runPreflight();
    if (this.preflight.report?.blocked) {
        showToast('Pre-flight encontrou erros — corrija antes de salvar', 'error');
        return;
    }
    // ... resto da função existente
},
```
</change>
<acceptance>
- Save bloqueado mesmo se o user clicar antes do auto-run terminar (re-roda no save).
- Toast explica o motivo.
- Se preflight passou clean, save procede normal.
</acceptance>
</task>

## Verification

- [ ] Manual: criar agente novo, ir até Revisão → ver loading → ver checks. Sem erros e sem warnings = card emerald, "Configuração OK".
- [ ] Manual: setar provider sem API key → ver card rose, check C1, botão disabled.
- [ ] Manual: vincular skill_id inexistente → ver C2, botão disabled.
- [ ] Manual: skill com tools fora do registry → card amber, C3 listado, botão habilitado, save funciona.
- [ ] Manual: clicar "Verificar novamente" durante loading → debounce ou no-op (já bloqueado por `:disabled`).
- [ ] Manual: editar agente existente, Revisão → mesma lógica, alimentado pelo state do form.

## must_haves

- Operador entende a severidade SÓ pela cor do card (rose/amber/emerald) sem ler o título.
- Cada check tem fix_hint actionable quando possível.
- Save bloqueado é evidente (botão visual disabled + tooltip).
- Sem JS errors no DevTools console em qualquer estado.

## Notes

- Card de "Configuração OK" duplica visualmente com o pass-through warning existente (linha ~266). Tudo bem — pass-through é diferente: é uma intenção válida (router puro), só o card de pre-flight cobre o restante.
- Considere mover ícones (✓/✗/⚠/ℹ) pra utility do brand-md no futuro. Hoje, inline é suficiente.
- Polling do preflight não faz sentido: rodamos só nos eventos certos (entrar na step, save, botão manual). Sem `setInterval`.
