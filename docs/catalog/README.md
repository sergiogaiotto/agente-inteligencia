# Catálogo / Marketplace Corporativo — Ondas 1 + 2

Visão geral do módulo de catálogo entregue em **16 PRs incrementais**
(10 da Onda 1 + 6 da Onda 2). Etiqueta nutricional R6.3 obrigatória,
governança Root, lifecycle explícito, External Platforms catalogadas,
inventário regulatório com CSV export, stewardship dashboard,
bulk decide para Root.

> **Onda 2 já entregue** — veja [ONDA2.md](ONDA2.md) para o delta específico.

## O que entrega

Loop completo de governança de IA dentro da empresa, pela UI:

```
publisher (qualquer user autenticado)
    │
    ├─ /agents ou /skills  ──► botão "Publicar no Catálogo"
    │                          (pre-fill via query string)
    │
    └─ /catalog/publish    ──► wizard 4 passos (artefato → metadata →
                               disclosure → revisão+submit)
                                │
                                ▼
                          entry em status='draft'
                                │
                                ▼
                          submission criada (pending)
                                │
                                ▼
            ┌───────────────────┴──────────────────┐
            │                                      │
    Root vê em /catalog/queue             Owner vê em /catalog/{id}
    (lista filtrável + pré-checks)        (4 tabs + ações contextuais)
            │
            ├─► Aprovar  ──► status='approved'  ──► owner publica
            ├─► Mudanças ──► status='draft'     ──► owner itera + re-submete
            └─► Rejeitar ──► status='draft'     ──► (mesma coisa)
                                                       │
                                                       ▼
                                                  published
                                                       │
                                                       ▼
                                                  deprecated → archived
```

## Estrutura de arquivos

```
app/
├── catalog/                          # módulo Python
│   ├── __init__.py
│   ├── lifecycle.py                  # state machine declarativa
│   ├── models.py                     # Pydantic (Create/Update/Output)
│   ├── prechecks.py                  # 8 checks executados no submit
│   ├── queries.py                    # SQL especializado (visibility, disclosure)
│   └── urn.py                        # urn:maestro:<ws>:<kind>:<slug>:<version>
├── core/
│   └── database.py                   # +4 tabelas + 9 índices + 4 repos
├── routes/
│   └── catalog.py                    # 14 endpoints REST
├── templates/
│   ├── pages/
│   │   ├── catalog.html              # A1 — browse + search
│   │   ├── catalog_detail.html       # A2 — detalhe + ações
│   │   ├── catalog_publish.html      # B1 — wizard de publish
│   │   └── catalog_queue.html        # C1 — fila Root
│   └── layouts/base.html             # nav item "Catálogo" + "Fila Root" (gated)
docs/
└── catalog/
    ├── README.md                     # este arquivo
    ├── REGRESSION.md                 # checklist consolidado
    └── SMOKE_TEST.md                 # roteiros manuais por PR (1-10)
tests/                                # primeiro tests/ do projeto
├── conftest.py
├── test_catalog_api.py               # 60+ testes de API (TestClient + mocks)
├── test_catalog_lifecycle.py
├── test_catalog_models.py
├── test_catalog_prechecks.py
├── test_catalog_queries.py
└── test_catalog_urn.py
pytest.ini                            # config mínima
```

## Modelo de dados (PostgreSQL)

| Tabela | Função |
|---|---|
| `catalog_entries` | Artefato publicável (PK: id; URN único) |
| `catalog_submissions` | Workflow de aprovação (1 row por submit; aceita re-submissão) |
| `catalog_capability_disclosure` | Etiqueta nutricional 1:1 com entry (PK: entry_id) |
| `catalog_costs` | Tracking insert-only por invocação (base para chargeback futuro) |

Lifecycle: `draft → submitted → approved → published → deprecated → archived`.
Review: `pending → approved | rejected | changes_requested`.

URN futuro-proof: `urn:maestro:<workspace>:<kind>:<slug>:<version>`.
Onda 1 fixa `workspace='default'`; preparado para multi-workspace e federação.

## API (14 endpoints sob `/api/v1/catalog`)

**Entries**:
- `GET /entries` — list paginado, visibility-aware (SQL com WHERE complexo)
- `GET /entries/{id}` — detalhe (404 anti-vazamento se invisível)
- `POST /entries` — cria draft + URN auto-gerado
- `PUT /entries/{id}` — update (só draft, só owner/root)
- `DELETE /entries/{id}` — remove (só draft|archived)

**Workflow**:
- `POST /entries/{id}/submit` — draft → submitted (roda pré-checks)
- `POST /entries/{id}/publish` — approved → published
- `POST /entries/{id}/deprecate` — published → deprecated
- `POST /submissions/{id}/decide` — Root decide (approved/rejected/changes_requested)
- `GET /submissions/queue` — Root vê fila
- `GET /entries/{id}/submissions` — histórico (owner/root)

**Capability Disclosure**:
- `GET /entries/{id}/capability` — transparente (qualquer user que veja a entry)
- `PUT /entries/{id}/capability` — declara (só draft, só owner/root)
- `DELETE /entries/{id}/capability` — remove (só draft, só owner/root)

## Regras de visibilidade (`can_user_see`)

1. **Root** vê tudo
2. **Owner** vê próprias entries em qualquer status/visibility
3. Demais veem apenas `published` ou `deprecated` com:
   - `visibility='company'` OR
   - `visibility='department'` E `visibility_scope ∈ user.domains`

Filtro aplicado em SQL (paginação correta).

## Pré-checks (`run_prechecks`)

8 verificações no submit. Severidade `error` derruba `passed=False`;
`warning` é informativo. Submit não bloqueia — Root decide com o relatório.

| Check | Severidade | Falha se… |
|---|---|---|
| `name_length` | error | nome < 3 chars |
| `description_length` | warning | descrição < 20 chars |
| `version_semver` | error | não-semver |
| `owner_exists` | error | owner_user_id sumiu |
| `owner_active` | warning | user.status != 'active' |
| `capability_disclosure_present` | error | sem disclosure |
| `visibility_scope_for_department` | error | dept sem scope |
| `a2a_has_artifact` | error | a2a sem artifact_id |

## Capability Disclosure (R6.3)

Etiqueta nutricional inspirada em iOS App Privacy Labels. 12 flags + soberania + notas.
4 categorias na UI:

- 🔐 **Dados do consumer**: reads_user_kb, writes_user_kb, stores_input (+retention)
- 🌐 **Integrações externas**: calls_external_apis (+lista obrigatória), accesses_internet
- ⚖️ **Dados regulados**: processes_pii, processes_financial, processes_health
- 🧠 **Modelo**: trains_on_input, output_is_deterministic

Soberania: BR | EU | US | global | NULL.

## Visibilidade por role

| Tela | comum | root |
|---|---|---|
| `/catalog` | ✅ visibility-filtered | ✅ tudo |
| `/catalog/{id}` | ✅ se visível | ✅ tudo |
| `/catalog/{id}/capability` | ✅ se visível à entry | ✅ tudo |
| `/catalog/publish` | ✅ | ✅ |
| `/catalog/queue` | ❌ "Acesso restrito" | ✅ |
| Nav "Fila Root" | ❌ oculto | ✅ visível |

## O que entrou nas Ondas (status atual)

### ✅ Onda 1 entregue (PRs #47-#56)
Loop básico de governança: schema + API CRUD + workflow + capability disclosure + UI completa.

### ✅ Onda 2 entregue (PRs #57-#62)
- **External Platforms** como kind separado (R10) — ChatGPT/Cursor/Copilot/etc.
- **Inventário Regulatório** com CSV export (R13)
- **Stewardship Dashboard** com flags is_orphan/is_stale/has_low_reliability (R11)
- **Bulk decide** + filtros avançados na fila Root

### Reservado para Onda 3+

- **Recipes publicáveis** (mesh + bindings como artefato — R8.1)
- **Sandbox** de invocação com dados mock (R14)
- **Adapter A2A bidirecional** (consumir Maestros externos; expor agentes como MCP server)
- **OPA tiered approval** (community auto, verified Root, official auditor — R3.1)
- **Verificação por execução** do capability disclosure (capability fingerprint)
- **Federation de URN** entre instâncias Maestro (R5.3 — schema já prevê)
- **Cost & Consumption page** dedicada (dados já coletados em `catalog_costs`)
- **Stewardship aberto a stewards de área** (não só Root)
- **Trust score erosion** por drift (R5.2)
- **Revenue-share em recipes**

## Métricas de entrega

| Indicador | Onda 1 | Onda 2 (delta) | Total |
|---|---|---|---|
| PRs entregues | 10 | +6 | **16** |
| Endpoints REST | 14 | +7 | **21** |
| Páginas UI novas | 4 | +2 | **6** |
| Páginas UI alteradas | 4 | +4 | 8 |
| Tabelas PostgreSQL | 4 | +1 | **5** |
| Testes unitários | 171 | +50 | **221** |
| Pré-checks | 7 | +1 | **8** |
| Audit actions distintas | 6 | +3 | **9** |
| Breaking changes | 0 | 0 | **0** |

## Referências

- [ONDA2.md](ONDA2.md) — resumo da Onda 2
- [REGRESSION.md](REGRESSION.md) — checklist consolidado de regressão
- [SMOKE_TEST.md](SMOKE_TEST.md) — roteiros manuais por PR
- PRs Onda 1: #47 (schema), #48 (CRUD), #49 (workflow), #50 (disclosure),
  #51 (browse), #52 (detail), #53 (publish wizard), #54 (queue),
  #55 (integrations), #56 (fechamento)
- PRs Onda 2: #57 (ext backend), #58 (ext UI), #59 (inventário),
  #60 (stewardship), #61 (bulk decide), #62 (fechamento — este)
