# Onda 2 — Catálogo / Marketplace Corporativo

Resumo executável dos 6 PRs entregues na Onda 2. Constrói sobre a Onda 1
(loop básico de governança) e adiciona: External Platforms, Inventário
Regulatório, Stewardship Dashboard, Bulk decide.

## O que entrega

```
ONDA 1 (loop básico):           ONDA 2 (governança madura):
  publisher → Root → owner       + External Platforms (kind novo)
                                 + Inventário regulatório + CSV export
                                 + Stewardship dashboard + reassign
                                 + Bulk decide + filtros avançados
```

## Mapa de PRs

| PR | Branch | Tema |
|---|---|---|
| #57 | external-platforms-backend | Schema + API de external metadata |
| #58 | external-platforms-ui | Wizard variant + tab no detail |
| #59 | catalog-inventory | Inventário regulatório + CSV export |
| #60 | catalog-stewardship | Stewardship dashboard + reassign |
| #61 | catalog-bulk-decide | Bulk decide + filtros avançados |
| #62 | catalog-onda2-regression | Fechamento (este) |

## Schema novo

```sql
-- 1:1 com catalog_entries quando kind='external_platform'
catalog_external_metadata (
    entry_id PK FK → catalog_entries,
    vendor NOT NULL,
    vendor_url,
    contract_status CHECK,    -- none|negotiating|active|expired|terminated
    contract_renewal_date DATE,
    monthly_cost_usd REAL,
    vendor_contact,
    approved_use_cases,
    restrictions,
    approved_by_user_id,
    approved_at,
    created_at, updated_at
);
```

CASCADE delete sincroniza com `catalog_entries`. Total de tabelas catalog: **5**.

## Endpoints novos (21 total)

| Método | Rota | PR | Quem |
|---|---|---|---|
| GET | `/entries/{id}/external-metadata` | #57 | Qualquer user que vê a entry |
| PUT | `/entries/{id}/external-metadata` | #57 | owner/root, draft, external_platform |
| GET | `/inventory` | #59 | Root |
| GET | `/inventory/export.csv` | #59 | Root |
| GET | `/stewardship` | #60 | Root |
| POST | `/entries/{id}/reassign` | #60 | Root |
| POST | `/submissions/bulk-decide` | #61 | Root |

## UI novas

| Página | Rota | Quem | PR |
|---|---|---|---|
| Inventário Regulatório | `/catalog/inventory` | Root | #59 |
| Stewardship Dashboard | `/catalog/stewardship` | Root | #60 |

Páginas modificadas (Onda 2):
- `catalog_detail.html` (#58): tab "Metadata Externa" + modal de edição
- `catalog_publish.html` (#58): card de external platform no Step 1, bloco no Step 3, submit encadeia 4 chamadas
- `catalog_queue.html` (#61): checkbox, select-all, bulk action bar, modal de bulk + 4 filtros client-side
- `layouts/base.html` (#59 + #60): nav items "Inventário" e "Stewardship" gated por Root

## Novos pré-checks

`external_metadata_present` (warning, só para kind=external_platform):
exige metadata vendor declarada para Root aprovar plataforma externa.

## Novas actions de audit_log

- `external_metadata_declared` (#57)
- `stewardship_reassigned` (#60) — details inclui `{from, to}` para owner e steward
- `review_{decision}` com `details.bulk=true` (#61) — distingue de decisões individuais

## Visibilidade nova (gates Root)

Onda 2 adicionou 3 rotas Root-only:
- `/catalog/inventory` + endpoint API
- `/catalog/stewardship` + endpoint API
- `/api/v1/catalog/submissions/bulk-decide`

Nav items condicionais via `{% if user_role == 'root' %}` (continua o padrão de `/catalog/queue` da Onda 1).

## Métricas de entrega

| Indicador | Onda 1 | Onda 2 (delta) | Total |
|---|---|---|---|
| Endpoints REST | 14 | +7 | **21** |
| Páginas UI | 4 | +2 | **6** |
| Tabelas PostgreSQL | 4 | +1 | **5** |
| Testes unitários | 171 | +50 | **221** |
| Pré-checks | 7 | +1 | **8** |
| Audit actions distintas | 6 | +3 | **9** |
| Breaking changes | 0 | **0** | — |

## O que ainda fica para Onda 3+

Reservado:
- **Recipes publicáveis** (composição de entries como artefato — R8.1)
- **Adapter A2A bidirecional** (consumir Maestros externos, expor agentes como MCP server)
- **Verificação por execução** do capability disclosure (capability fingerprint)
- **Federation URN** entre instâncias Maestro
- **OPA tiered approval** (community auto, verified Root, official auditor)
- **Stewardship aberto a stewards de área** (não só Root)
- **Cost & Consumption page dedicada**
- **Trust score erosion por drift**
- **Revenue-share em recipes**

Detalhes em [README.md](README.md).
