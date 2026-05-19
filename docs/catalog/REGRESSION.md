# Regressão da Onda 1 — Catálogo

Checklist consolidado executável após o merge dos 10 PRs.
Foco: garantir que o catálogo funciona ponta-a-ponta E que nada existente regrediu.

## Fase 1 — Verificação automática (sem dependências externas)

### 1.1 Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Critério de sucesso**: **171 passed** em <2s. Categorias:

| Suite | Testes | Cobre |
|---|---|---|
| test_catalog_urn.py | 19 | slugify, make_urn, parse_urn, is_valid_urn |
| test_catalog_lifecycle.py | 15 | state machine entry + review |
| test_catalog_models.py | 22 | Pydantic Create/Update/Output, CapabilityDisclosure, SubmissionDecision |
| test_catalog_queries.py | 23 | is_root, _user_domains, can_user_see (12 cenários), db_row_to_entry_dict |
| test_catalog_prechecks.py | 12 | 8 checks de `run_prechecks` + agregação |
| test_catalog_api.py | 80 | TestCreate, GetOne, Update, Delete, List, Submit, Decide, Publish, Deprecate, Queue, EntrySubmissions, CapabilityPut/Get/Delete |

### 1.2 Sanity de import

```powershell
.venv\Scripts\python.exe -c "from app.main import app; print('routes catalog:', len([r for r in app.routes if hasattr(r,'path') and '/catalog' in r.path]))"
```

**Critério**: ≥ 18 (14 API + 4 frontend = `/catalog`, `/catalog/publish`, `/catalog/queue`, `/catalog/{entry_id}`).

### 1.3 Parse dos templates novos

```powershell
.venv\Scripts\python.exe -c "from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader('app/templates')); [e.get_template(t) for t in ['pages/catalog.html','pages/catalog_detail.html','pages/catalog_publish.html','pages/catalog_queue.html']]; print('OK')"
```

**Critério**: imprime `OK` sem traceback.

## Fase 2 — Regressão das 14 telas existentes

Subir o app (`uvicorn app.main:app --reload --port 7000`), logar e abrir cada URL.
**Critério**: zero erro 500, página renderiza, funcionalidades atuais intactas.

| URL | Página | Esperado |
|---|---|---|
| /login | Login | Form renderiza |
| / | Dashboard | Cards de topologia OK + **nova seção Catálogo aparece** |
| /agents | Agentes | Lista existe + **novo botão "Publicar no Catálogo" no hover** |
| /agents/new | Novo Agente | Wizard 4 passos OK |
| /agents/{id}/edit | Editar | Form preenchido |
| /agents/{id}/invocations | Invocações | Histórico OK |
| /skills | Skills | Lista existe + **novo botão "Publicar no Catálogo" no hover** |
| /skills/new | Nova Skill | Editor + parser |
| /workspace | Workspace | Chat funcional |
| /mesh | AI Mesh | Topologia SVG |
| /mcp | Ferramentas | CRUD tools |
| /rag | Bases de Conhecimento | KNOWLEDGE_SOURCEs |
| /api-connectors | API Connectors | Lista |
| /harness | Avaliação | Dataset gold + eval runs |
| /releases | Releases | Cards |
| /quality | Qualidade | Verifier scores |
| /observability | Observabilidade | Métricas |
| /infra | Infraestrutura | Status |
| /history | Histórico | Tabs |
| /settings | Configurações | Plataforma + prompts |

## Fase 3 — Funcionalidade nova (catálogo)

### 3.1 Navegação

| Verificação | Esperado |
|---|---|
| Sidebar mostra item "Catálogo" abaixo de Skills | ✅ |
| Sidebar mostra "Fila Root" SÓ para `user_role='root'` | ✅ |
| Dashboard mostra seção "Catálogo Corporativo" | ✅ |

### 3.2 Lista (/catalog)

| Verificação | Esperado |
|---|---|
| Sem entries: empty state com link para /agents | ✅ |
| Com entries: grid responsivo (1/2/3 colunas) | ✅ |
| Card mostra: nome, URN, kind/status badges, version, trust metrics, tags | ✅ |
| Busca filtra em tempo real (client-side) | ✅ |
| Filtros tipo/status/domínio recarregam (server-side) | ✅ |
| Botão "Publicar no Catálogo" aponta para /catalog/publish | ✅ |
| User comum não vê entries privadas de outros | ✅ |

### 3.3 Detalhe (/catalog/{id})

| Verificação | Esperado |
|---|---|
| Entry inexistente / invisível: 404 + link voltar | ✅ |
| Header: nome + URN + 3 badges + action menu | ✅ |
| Tab "Visão Geral": trust metrics, adapter, tags | ✅ |
| Tab "Capability Disclosure": etiqueta nutricional 4 categorias | ✅ |
| Tab "Histórico": submissões com pré-checks expansíveis (só owner/root) | ✅ |
| Tab "Manifest": JSON cru | ✅ |
| Ações por status + role corretas | ✅ |
| Modal capability: validação client-side + PUT funciona | ✅ |
| Modal decide (Root): POST decide + recarrega | ✅ |

### 3.4 Wizard de publicação (/catalog/publish)

| Verificação | Esperado |
|---|---|
| Stepper visual marca step ativo e concluído | ✅ |
| Step 1 lista agents + skills com busca/filtro | ✅ |
| Pick pré-preenche Step 2 | ✅ |
| Query string `?kind=&artifact_id=` pula direto para Step 2 | ✅ |
| Step 2: URN preview atualiza ao digitar | ✅ |
| Step 3: campos condicionais (external_apis_list, retention) | ✅ |
| Step 4: resumo + cartão amarelo explicativo | ✅ |
| Submit final: 3 chamadas em sequência | ✅ |
| Sucesso: redireciona para /catalog/{id} | ✅ |
| Erro intermediário: mensagem orienta usuário | ✅ |

### 3.5 Fila Root (/catalog/queue)

| Verificação | Esperado |
|---|---|
| Não-Root acessando vê "Acesso restrito" | ✅ |
| Root vê fila paginada + filtro por status | ✅ |
| Cada item: nome, kind, version, submitter, data | ✅ |
| Pré-checks resumidos + `<details>` expansível | ✅ |
| Capability declarada exibe flags coloreadas | ✅ |
| Botões inline + modal funcionam | ✅ |
| Tabs decididas mostram reviewer/data/notes | ✅ |

## Fase 4 — Fluxo end-to-end completo

Pré: 2 users (`u1` comum, `root1` root), 1 agente criado em `/agents/new`.

```
1. [u1] /agents → click "Publicar no Catálogo" no agente
2. [u1] /catalog/publish?kind=agent&artifact_id=...  → Step 2
3. [u1] Avança steps, declara disclosure mínima, submete
4. [u1] redirect /catalog/{id} → status=submitted
5. [root1] /catalog/queue → vê submission com pré-checks
6. [root1] Click "Aprovar" → entry vai para approved
7. [u1] /catalog/{id} → status=approved → click "Publicar"
8. [u1] status=published → entry aparece para outros users
9. [u2] /catalog → vê entry no grid
10. [u2] /catalog/{id} → vê capability disclosure (transparência)
11. [u1] click "Depreciar" → status=deprecated
12. [auditoria] SELECT em audit_log mostra todas as transições
```

**Critério**: cada passo executa sem erro, badges/contagens atualizam,
audit_log popula corretamente.

## Fase 5 — Banco e infra

### 5.1 Tabelas criadas

```sql
\dt catalog_*
```

Esperado: 4 tabelas (entries, submissions, capability_disclosure, costs).

### 5.2 Constraint enforcement

```sql
INSERT INTO catalog_entries (id, urn, name, kind, owner_user_id) VALUES (
  '...', 'urn:test:default:agent:x:1.0.0', 'X', 'INVALID_KIND', 'u1'
);
```

Esperado: rejeitado por `CHECK(kind IN ...)`.

### 5.3 Audit trail

```sql
SELECT action, COUNT(*) FROM audit_log
WHERE entity_type = 'catalog_entry'
GROUP BY action;
```

Após fluxo end-to-end: linhas para `created`, `updated` (se houve PUT),
`capability_declared`, `submitted`, `review_approved`, `published`, `deprecated`.

## Sign-off Onda 1

Quando todos os critérios das 5 fases passarem:

- [ ] Fase 1 — automático verde (171 passed + imports OK + templates OK)
- [ ] Fase 2 — 14 telas existentes não regrediram
- [ ] Fase 3 — funcionalidade nova validada
- [ ] Fase 4 — fluxo end-to-end completo OK
- [ ] Fase 5 — banco e auditoria OK

**Onda 1 do Catálogo está pronta para produção.**

---

## Fase 6 — Regressão Onda 2

Adicional: External Platforms + Inventário + Stewardship + Bulk decide.
Total agora: **221 testes** (171 Onda 1 + 50 Onda 2).

### 6.1 Automático

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

Esperado: **221 passed**. Suites Onda 2 adicionadas:
- `TestExternalMetadataCheck` (4) + `TestExternalPlatformMetadata` (8) + `TestExternalMetadataPut/Get` (13)
- `TestInventory` (6)
- `TestStewardship` (3) + `TestReassign` (8)
- `TestBulkDecide` (8)

### 6.2 Schema novo

```sql
\d catalog_external_metadata
```

Esperado: tabela com 13 colunas, PK=entry_id, FK→catalog_entries CASCADE, CHECK em contract_status.

### 6.3 Endpoints novos (7 — total 21)

| Rota | Método | Auth |
|---|---|---|
| `/entries/{id}/external-metadata` | GET/PUT | qualquer/owner+root |
| `/inventory` | GET | Root |
| `/inventory/export.csv` | GET | Root |
| `/stewardship` | GET | Root |
| `/entries/{id}/reassign` | POST | Root |
| `/submissions/bulk-decide` | POST | Root |

### 6.4 Telas novas + atualizadas

| Página | Status |
|---|---|
| `/catalog/inventory` | NOVO — Root only |
| `/catalog/stewardship` | NOVO — Root only |
| `/catalog/detail` | tab "Metadata Externa" condicional + modal |
| `/catalog/publish` | card external + bloco Step 3 + submit 4 chamadas |
| `/catalog/queue` | checkbox + select-all + bulk bar + 4 filtros client-side |

### 6.5 Nav items condicionais (Root)

- "Inventário (regulatório)"
- "Stewardship"
- "Fila Root" (já existia da Onda 1)

### 6.6 Audit actions novas

- `external_metadata_declared`
- `stewardship_reassigned` (com `details.{owner,steward_team}.{from,to}`)
- `review_{decision}` com `details.bulk=true` (distingue de individual)

### 6.7 Fluxo end-to-end Onda 2

```
[publisher] /catalog/publish → External Platform → vendor=OpenAI → submete
    ↓
[Root] /catalog/queue → vê + seleciona 3 pendentes → "Aprovar todas"
    ↓
[Root] /catalog/inventory → filtro PII=true → vê listagem → Export CSV
    ↓
[Root] /catalog/stewardship → identifica órfãs → Realocar owner
```

## Sign-off Onda 2

- [ ] Fase 6.1 — 221 testes passam
- [ ] Fase 6.2 — 5 tabelas catalog_* existem (4 Onda 1 + 1 nova)
- [ ] Fase 6.3 — 21 endpoints REST + 6 UI registrados
- [ ] Fase 6.4 — telas funcionais
- [ ] Fase 6.5 — nav items Root-only
- [ ] Fase 6.6 — audit_log popula com 9 actions distintas
- [ ] Fase 6.7 — fluxo end-to-end completo

**Onda 2 do Catálogo está pronta para sign-off e produção.**
