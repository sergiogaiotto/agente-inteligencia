# Onda 4 — Catálogo / Marketplace Corporativo

Resumo executável dos **5 PRs entregues**. Constrói sobre Onda 3 (recipes
publicáveis como manifest) e adiciona: **execução real**, **UI de execução**,
**cost pleno por provider/model**, **sandbox**, **anomalias de cost**.

## O que entrega

```
ONDA 1:                ONDA 2:                ONDA 3:                ONDA 4:
loop básico            governança madura      maturidade             recipes executáveis
publisher → Root       + External Platforms   operacional            + execução real
   → owner             + Inventário           + Stewardship aberto     (chain + async)
                       + Stewardship          + Cost & Consumption   + UI de polling
                         Dashboard              (chargeback futuro)  + cost pleno
                       + Bulk decide          + Recipes (manifest)     (pricing real)
                                                                     + sandbox
                                                                       (testar antes
                                                                        de publicar)
                                                                     + anomalias
                                                                       (alertas de cost)
```

## Mapa de PRs

| PR | Branch | Tema |
|---|---|---|
| #67 | feat/catalog-recipes-execution | Execução real de recipes (chain + async) |
| #68 | feat/catalog-recipes-execution-ui | UI de execução (tab Execuções + polling) |
| #69 | feat/catalog-cost-pricing | Cost pleno por provider/model |
| #70 | feat/catalog-sandbox | Sandbox de invocação |
| #71 | feat/catalog-cost-anomalies | Anomalias de cost (picos + limite global) |
| #72 | chore/catalog-onda4-regression | Fechamento (este) |

## Schema novo

```sql
-- Onda 4 / PR #67: runs reais de recipes
catalog_recipe_executions (
    id TEXT PK,
    recipe_entry_id TEXT FK → catalog_entries CASCADE,
    consumer_user_id TEXT NOT NULL,
    input TEXT NOT NULL,
    steps_results JSONB,        -- [{order, target, status, output, cost_usd, ...}]
    status TEXT CHECK(IN ('running','completed','partial','failed')),
    total_cost_usd REAL,
    total_latency_ms INTEGER,
    error_message TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    -- Onda 4 / PR #70: flag de sandbox (ALTER TABLE idempotente)
    is_sandbox BOOLEAN DEFAULT FALSE
);
-- 3 índices: (recipe_entry_id, started_at DESC), (consumer_user_id, ...), (status)
```

Total de tabelas catalog: **7** (entries, submissions, capability_disclosure,
costs, external_metadata, recipes, **recipe_executions**).

## Módulos Python novos

| Módulo | PR | Função |
|---|---|---|
| `app/catalog/executor.py` | #67 | Roda chain (output[N-1] → input[N]) com skip-after-failure |
| `app/core/llm_pricing.py` | #69 | Dict PRICING + `compute_cost(provider, model, in, out)` |
| `app/catalog/anomalies.py` | #71 | `detect_anomalies()` + thresholds hardcoded |

## Endpoints novos (5 — total **32**)

| Método | Rota | PR | Quem |
|---|---|---|---|
| POST | `/entries/{id}/execute` | #67 | Qualquer user que vê a entry (recipe published) |
| GET | `/executions/{id}` | #67 | root \| consumer \| owner do recipe |
| GET | `/entries/{id}/executions` | #67 | Qualquer user que vê a entry |
| POST | `/entries/{id}/sandbox` | #70 | Owner \| Root (qualquer status, incl. draft) |
| GET | `/cost/anomalies` | #71 | Auto-scope: Root → all, demais → mine |

## UI novas e alteradas

| Página | Status | PR |
|---|---|---|
| `/catalog/{id}` — tab **Execuções** | NOVO (template existente) | #68 |
| `/catalog/{id}` — botão **🧪 Sandbox** + badge no histórico | NOVO | #70 |
| `/catalog/cost` — banner de anomalias | NOVO | #71 |
| `/catalog/{id}` — modal de disparo + modal de polling | NOVO | #68 |

## Pricing (snapshot 2026-05, hardcoded em `app/core/llm_pricing.py`)

| Provider/Model | Input USD/1k | Output USD/1k |
|---|---|---|
| azure/gpt-4o | 0.0025 | 0.01 |
| azure/gpt-4o-mini | 0.00015 | 0.0006 |
| azure/gpt-4-turbo | 0.01 | 0.03 |
| anthropic/claude-opus-4-7 | 0.015 | 0.075 |
| anthropic/claude-sonnet-4-6 | 0.003 | 0.015 |
| anthropic/claude-haiku-4-5 | 0.0008 | 0.004 |
| maritaca/sabia-4 | 0.0005 | 0.0015 |
| ollama/* | 0 | 0 |

Modelo desconhecido → cost_usd=0 + WARNING log (não derruba o fluxo).

## Thresholds de anomalia (PR #71, `app/catalog/anomalies.py`)

| Constante | Valor | Significado |
|---|---|---|
| `PICO_MULTIPLIER` | 3.0 | Hoje ≥ 3× média 7d → pico |
| `PICO_MIN_BASELINE_USD` | 1.0 | Ignora pico se baseline < $1 |
| `LIMITE_GLOBAL_USD` | 100.0 | Hoje > $100 → limite_global |
| `BASELINE_WINDOW_DAYS` | 7 | Janela do baseline (exclui hoje) |

## Mudanças de comportamento (não-breaking)

### Execução de recipes deixa de ser placeholder

**Antes (Onda 3)**: manifest declarativo apenas (não rodava).
**Depois (Onda 4)**: chain sequencial via engine; cada step invoca um agent
publicado; output[N-1] vira input[N]. Falha de step quebra chain (demais
ficam `skipped`); execution finaliza como `partial`.

### Cost passa a refletir uso real

**Antes (Onda 4 PR #67)**: executor gravava `cost_usd=0` (placeholder).
**Depois (Onda 4 PR #69)**: `compute_cost(provider, model, in_tokens, out_tokens)`
calcula USD real usando pricing table. Trace do engine traz tokens.input/output
separados; provider/model pegados de `trace.agent_provider`/`trace.agent_model`.

### Sandbox como free tier de dev

**Antes**: para testar um recipe, owner publicava e usava o /execute regular
(o custo ia para o chargeback dele mesmo).
**Depois (PR #70)**: `POST /sandbox` aceita qualquer status (incl. draft);
runs marcadas `is_sandbox=true` NÃO gravam em `catalog_costs`. LLM real
ainda é chamado (testa qualidade/latência), mas o custo fica fora dos
dashboards de chargeback.

### Anomalias de cost visíveis no dashboard

**Antes**: dashboard de cost mostrava só agregados — pico só pego se alguém
estivesse olhando.
**Depois (PR #71)**: banner vermelho em `/catalog/cost` aparece automaticamente
quando há pico (≥ 3× média 7d) ou limite global ($100/dia). Sandbox runs
não inflam baseline (porque não gravam em catalog_costs).

## Audit actions novas (4)

- `recipe_execution_started` (PR #67) — input_length, step_count
- `recipe_execution_finished` (PR #67) — status, total_cost_usd, total_latency_ms
- `recipe_sandbox_started` (PR #70) — input_length, step_count, entry_status
- `cost_anomaly_detected` (PR #71) — scope, anomaly_count, anomaly_types, today_usd

Total de actions distintas no catálogo: **15**.

## Métricas de entrega

| Indicador | Onda 1 | Onda 2 | Onda 3 | Onda 4 (delta) | Total |
|---|---|---|---|---|---|
| PRs entregues | 10 | 6 | 4 | +5 | **25** |
| Endpoints REST | 14 | 7 | 6 | +5 | **32** |
| Páginas UI novas | 4 | 2 | 1 | 0 | **7** |
| Páginas UI alteradas | 4 | 4 | 4 | +2 | 14 |
| Tabelas PostgreSQL | 4 | 1 | 1 | +1 | **7** |
| Testes unitários | 171 | 50 | 36 | +65 | **322** |
| Pré-checks | 7 | 1 | 1 | 0 | **9** |
| Audit actions distintas | 6 | 3 | 2 | +4 | **15** |
| Breaking changes | 0 | 0 | 0 | **0** | — |

## Reservado para Onda 5+

Itens do roadmap original que ficaram fora desta Onda — exigem fase de
design dedicada ou maior maturidade do produto antes de codar:

- **A2A bidirecional** — consumir Maestros externos + expor agentes como MCP server
  (toca `app/mcp/runtime.py` + `app/a2a/protocol.py`; design pesado)
- **Capability fingerprint** — verificação por execução do disclosure
  (compara o que foi declarado vs o que o runtime detecta)
- **OPA tiered approval** — community auto, verified Root, official auditor (R3.1)
- **Federation URN** entre instâncias Maestro (R5.3 — schema já prevê)
- **Trust score erosion** por drift (R5.2)
- **Revenue-share em recipes** (chargeback interno entre áreas)
- **Pricing editável via UI** — migrar `llm_pricing.py` para `platform_settings`
  ou tabela dedicada (hoje requer commit + deploy para ajustar)

## Referências

- [ONDA3.md](ONDA3.md) — resumo da Onda 3
- [ONDA2.md](ONDA2.md) — resumo da Onda 2
- [README.md](README.md) — visão geral consolidada
- [REGRESSION.md](REGRESSION.md) — checklist consolidado (Fase 8 = Onda 4)
- [SMOKE_TEST.md](SMOKE_TEST.md) — roteiros manuais por PR
- PRs Onda 4: #67 (execução), #68 (UI), #69 (cost pleno), #70 (sandbox),
  #71 (anomalias), #72 (fechamento — este)
