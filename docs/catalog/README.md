# Catálogo / Marketplace Corporativo — Ondas 1 + 2 + 3

Visão geral do módulo de catálogo entregue em **25 PRs incrementais**
(10 Onda 1 + 6 Onda 2 + 4 Onda 3 + 5 Onda 4). Etiqueta nutricional R6.3
com warning não-bloqueante na aprovação, governança Root, lifecycle explícito, External Platforms
catalogadas, inventário regulatório com CSV export, stewardship
descentralizado a stewards de área, cost & consumption com chargeback
real, recipes **executáveis** (chain sequencial), sandbox, e detecção
de anomalias de cost.

> **Onda 4 já entregue** — veja [ONDA4.md](ONDA4.md), [ONDA3.md](ONDA3.md) e [ONDA2.md](ONDA2.md) para o delta específico de cada uma.

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

**Política (achado A2A-1, 29.1.15)**: a disclosure é *warning não-bloqueante* —
precheck `capability_disclosure_present` marca error no submit, mas o Root PODE
aprovar sem ela. Nesse caso o `POST /submissions/{id}/decide` devolve
`disclosure_warning`, o audit grava `disclosure_missing=true`, a fila e o modal
de aprovação exibem aviso âmbar e a entry mostra banner permanente. Edição é
draft-only (PUT /capability responde 409 fora de rascunho — a UI explica em vez
de quebrar).

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

### ✅ Onda 3 entregue (PRs #63-#66)
- **Stewardship descentralizado** — aberto a stewards de área (via `users.domains`)
- **Cost & Consumption** — endpoint de registro + page com agregados + CSV export
- **Recipes publicáveis** — kind=recipe como composição declarativa (manifest; execução fica para Onda 4)

### ✅ Onda 4 entregue (PRs #67-#72)

- **Execução real de recipes** (PR #67) — chain sequencial via engine, async com polling, skip-after-failure, cost auto-wire básico por step. Nova tabela `catalog_recipe_executions` + 3 endpoints sob `/api/v1/catalog`.
- **UI de execução** (PR #68) — tab "Execuções" em `/catalog/{id}`, modal de disparo + modal de polling em tempo real (1.5s), histórico paginado, drill-down de execuções passadas.
- **Cost pleno** (PR #69) — `app/core/llm_pricing.py` com tabela de preços por provider/model; executor calcula `cost_usd` real (substituindo placeholder=0) usando `tokens.input × in_per_1k + tokens.output × out_per_1k`. Cobre azure, openai, anthropic, maritaca, ollama. Modelo desconhecido → 0 + warning.
- **Sandbox de invocação** (PR #70) — owner/Root rodam recipe em qualquer status (incl. draft) com LLM real, mas sem gravar em `catalog_costs`. Coluna `is_sandbox` na tabela de executions + endpoint `POST /sandbox` + botão 🧪 Sandbox + badge laranja no histórico/polling. Audit `recipe_sandbox_started`.
- **Anomalias de cost** (PR #71) — detecção de picos (hoje ≥ 3× média 7d) e limite global ($100/dia) sobre `catalog_costs`. Endpoint `GET /cost/anomalies` (auto-scope) + banner vermelho em `/catalog/cost` + audit `cost_anomaly_detected`. Thresholds hardcoded em `app/catalog/anomalies.py`.

### Reservado para Onda 5+

Itens do roadmap original entregues na Onda 4 saíram desta lista (execução
real, UI, cost pleno, sandbox, anomalias). Restam para fases futuras:

- **A2A bidirecional** (consumir Maestros externos; expor agentes como MCP server — design pesado)
- **Verificação por execução** do capability disclosure (capability fingerprint)
- **OPA tiered approval** (community auto, verified Root, official auditor — R3.1)
- **Federation de URN** entre instâncias Maestro (R5.3 — schema já prevê)
- **Trust score erosion** por drift (R5.2)
- **Revenue-share em recipes** (chargeback interno)
- **Pricing editável via UI** (migrar `llm_pricing.py` para `platform_settings`)

## Métricas de entrega

| Indicador | Onda 1 | Onda 2 | Onda 3 | Onda 4 (PR #67-71) | Total |
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

## Referências

- [ROADMAP.md](ROADMAP.md) — backlog priorizado da Onda 5+ com contexto para retomar
- [ONDA4.md](ONDA4.md) — resumo da Onda 4
- [ONDA3.md](ONDA3.md) — resumo da Onda 3
- [ONDA2.md](ONDA2.md) — resumo da Onda 2
- [REGRESSION.md](REGRESSION.md) — checklist consolidado de regressão (Fase 1-8)
- [SMOKE_TEST.md](SMOKE_TEST.md) — roteiros manuais por PR
- PRs Onda 1: #47 (schema), #48 (CRUD), #49 (workflow), #50 (disclosure),
  #51 (browse), #52 (detail), #53 (publish wizard), #54 (queue),
  #55 (integrations), #56 (fechamento)
- PRs Onda 2: #57 (ext backend), #58 (ext UI), #59 (inventário),
  #60 (stewardship), #61 (bulk decide), #62 (fechamento)
- PRs Onda 3: #63 (stewardship aberto), #64 (cost), #65 (recipes),
  #66 (fechamento)
- PRs Onda 4: #67 (execução), #68 (UI), #69 (cost pleno), #70 (sandbox),
  #71 (anomalias), #72 (fechamento — este)
