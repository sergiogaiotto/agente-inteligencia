# Onda 3 — Catálogo / Marketplace Corporativo

Resumo executável dos 4 PRs entregues. Constrói sobre Onda 1 (loop básico)
e Onda 2 (governança madura) e adiciona: **stewardship descentralizado**,
**cost & consumption**, **recipes publicáveis (manifest)**.

## O que entrega

```
ONDA 1:                       ONDA 2:                       ONDA 3:
loop básico                   governança madura             maturidade operacional
publisher → Root → owner      + External Platforms          + Stewardship aberto
                              + Inventário Regulatório        a stewards de área
                              + Stewardship Dashboard       + Cost & Consumption
                              + Bulk decide                   + chargeback futuro
                                                            + Recipes publicáveis
                                                              (manifest)
```

## Mapa de PRs

| PR | Branch | Tema |
|---|---|---|
| #63 | catalog-stewardship-open | Stewardship aberto a stewards de área |
| #64 | catalog-cost-consumption | Cost & Consumption + CSV export |
| #65 | catalog-recipes | Recipes publicáveis (manifest declarativo) |
| #66 | catalog-onda3-regression | Fechamento (este) |

## Schema novo

```sql
-- Onda 3 / PR 3: composição declarativa de entries
catalog_recipes (
    entry_id PK FK → catalog_entries CASCADE,
    steps JSONB,    -- [{"order":1, "target_entry_id":"...", "notes":"..."}, ...]
    created_at, updated_at
);
```

Total de tabelas catalog: **6** (entries, submissions, capability_disclosure,
costs, external_metadata, recipes).

## Endpoints novos (27 total)

| Método | Rota | PR | Quem |
|---|---|---|---|
| POST | `/entries/{id}/invocation-cost` | #64 | Qualquer user que vê a entry |
| GET | `/cost` | #64 | Auto: Root → all, demais → mine |
| GET | `/cost/export.csv` | #64 | Auto: Root → all, demais → mine |
| GET | `/entries/{id}/recipe` | #65 | Qualquer user que vê a entry |
| PUT | `/entries/{id}/recipe` | #65 | owner/root, draft, kind=recipe |
| DELETE | `/entries/{id}/recipe` | #65 | owner/root, draft |

(Endpoint stewardship mudou comportamento — não é endpoint novo.)

## UI novas

| Página | Rota | Quem | PR |
|---|---|---|---|
| Custo & Consumo | `/catalog/cost` | qualquer user (scope auto) | #64 |

Páginas modificadas (Onda 3):
- `catalog_detail.html` (#65): tab "Recipe Steps" condicional + modal com reorder
- `catalog_publish.html` (#65): card "Construir Recipe" no Step 1
- `catalog_stewardship.html` (#63): banner contextual + botão Realocar condicional
- `layouts/base.html` (#63 + #64): nav "Stewardship" aberto a stewards + nav "Custo & Consumo"

## Novos pré-checks

- `recipe_has_steps` (**error**, só para kind=recipe) — recipe sem steps é ininteligível
- `a2a_has_artifact` (ajustado) — skip para kind=recipe (recipe é a2a mas não tem artifact)

## Novas actions de audit_log

- `recipe_defined` (com `step_count` + `target_entry_ids`)
- `recipe_cleared`

> Cost endpoint NÃO gera audit por invocação (volume alto, inflaria audit_log).
> Future onda pode auditar anomalias/limites.

## Mudanças de comportamento (não-breaking)

### Stewardship aberto (PR #63)

**Antes (Onda 2)**: `/catalog/stewardship` 403 para não-Root.
**Depois (Onda 3)**: Root vê tudo; não-Root vê entries cujo `steward_team` está em `user.domains`. Sem domains = vê nada (página orienta a pedir associação).

Reaproveita campo existente `users.domains` — sem nova role no schema.

### Recipe é kind sem artifact

**Antes**: `kind in (agent, skill, recipe)` exigia artifact_type+id.
**Depois**: só agent/skill exigem. Recipe é composição declarativa (steps no `catalog_recipes`).

## Métricas de entrega

| Indicador | Onda 1 | Onda 2 | Onda 3 (delta) | Total |
|---|---|---|---|---|
| PRs entregues | 10 | 6 | +4 | **20** |
| Endpoints REST | 14 | 7 | +6 | **27** |
| Páginas UI novas | 4 | 2 | +1 | **7** |
| Páginas UI alteradas | 4 | 4 | +4 | 12 |
| Tabelas PostgreSQL | 4 | 1 | +1 | **6** |
| Testes unitários | 171 | 50 | +36 | **257** |
| Pré-checks | 7 | 1 | +1 | **9** |
| Audit actions distintas | 6 | 3 | +2 | **11** |
| Breaking changes | 0 | 0 | **0** | — |

## O que fica para Onda 4

- **A2A bidirecional** (consumir Maestros externos, expor agentes como MCP server)
- **Verificação por execução** do capability disclosure (capability fingerprint)
- **Auto-wire do cost** no engine (instrumentação automática)
- **Execução real de recipes** (chain sequencial via engine)
- **Federation URN** entre instâncias Maestro
- **OPA tiered approval** (community auto, verified Root, official auditor)
- **Trust score erosion** por drift
- **Revenue-share em recipes**
- **Sandbox** de invocação com dados mock
- **Audit de anomalias de cost** (alertas de pico/limite)

Detalhes em [README.md](README.md).
