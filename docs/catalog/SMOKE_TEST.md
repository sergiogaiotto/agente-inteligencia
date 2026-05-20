# Smoke Test — PR 1 (Catalog Schema + Repository)

Validação manual de que o schema do catálogo aplica corretamente e os repositories operam.

## Pré-requisitos
- PostgreSQL rodando e acessível via `DATABASE_URL`
- Variáveis de ambiente carregadas (`.env`)
- Dependências instaladas (`pip install -r requirements.txt`)

## 1 — Testes unitários (sem banco)

Lógica pura: URN, lifecycle, Pydantic models. Sem dependência de Postgres.

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 58 passed. Cobre slugify, make_urn, parse_urn, transições de
state machine (entry + review), validação de CatalogEntryCreate/Update/Entry,
CapabilityDisclosure (consistência APIs externas, retenção não-negativa),
SubmissionDecision.

## 2 — Migração do schema (com Postgres)

Subir a aplicação aplica `SCHEMA` + `_IDEMPOTENT_MIGRATIONS` em sequência. As
4 tabelas novas usam `CREATE TABLE IF NOT EXISTS` — seguro re-rodar.

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 7000
```

**Verificar log**: deve aparecer `PostgreSQL pool aberto: min=2 max=10` sem
tracebacks. Erros de SQL aparecem como WARNING `Migration ignorada:` (apenas
para `_IDEMPOTENT_MIGRATIONS`; falhas no `SCHEMA` quebram o startup).

## 3 — Validar tabelas criadas (psql)

```sql
\dt catalog_*
```

**Esperado**: 4 tabelas:
- `catalog_entries`
- `catalog_submissions`
- `catalog_capability_disclosure`
- `catalog_costs`

```sql
\d catalog_entries
```

**Esperado**: 26 colunas incluindo `urn` (UNIQUE), `kind` (CHECK), `status`
(CHECK com 6 valores), `visibility` (CHECK com 3 valores), `adapter_type`
(CHECK com 4 valores), índices em status/kind/owner/artifact.

## 4 — CRUD via Repository (Python REPL)

```python
import asyncio, uuid
from app.core.database import init_db, catalog_entries_repo, close_db
from app.catalog.urn import make_urn

async def smoke():
    await init_db()
    eid = str(uuid.uuid4())
    data = {
        "id": eid,
        "urn": make_urn("agent", "Smoke Test", "0.1.0"),
        "name": "Smoke Test",
        "description": "criação via smoke test",
        "kind": "agent",
        "artifact_type": "agent",
        "artifact_id": "fake-agent-id",
        "version": "0.1.0",
        "status": "draft",
        "visibility": "private",
        "owner_user_id": "test-user",
        "adapter_type": "a2a",
    }
    created = await catalog_entries_repo.create(data)
    print("created:", created["id"])

    found = await catalog_entries_repo.find_by_id(eid)
    assert found and found["urn"] == data["urn"]
    print("found URN:", found["urn"])

    updated = await catalog_entries_repo.update(eid, {"description": "atualizado"})
    assert updated and updated["description"] == "atualizado"
    print("updated description")

    rows = await catalog_entries_repo.find_all(limit=5)
    print(f"listed {len(rows)} entries")

    ok = await catalog_entries_repo.delete(eid)
    assert ok
    print("deleted")

    await close_db()

asyncio.run(smoke())
```

**Esperado**: 5 prints sem AssertionError. Entry criada → encontrada →
atualizada → listada → deletada.

## 5 — Regressão: telas e endpoints existentes

Carrega o navegador em http://localhost:7000 e abre cada página da nav
superior. Esperado: zero erros 500, todas as telas atuais carregam normal.

| Página | URL | Comportamento esperado |
|--------|-----|-----------------------|
| Login | `/login` | Formulário ainda renderiza |
| Dashboard | `/` | Cards carregam |
| Agentes | `/agents` | Lista existente intacta |
| Skills | `/skills` | Lista existente intacta |
| Workspace | `/workspace` | Chat funcional |
| AI Mesh | `/mesh` | Topologia carrega |
| Configurações | `/settings` | API keys/modelos editáveis |

**Health check**:
```powershell
curl http://localhost:7000/api/health
```
Esperado: JSON com `"status": "ok"`, `"app": "Maestro"`, fingerprint do código.

## Critérios de aceitação do PR 1

- [x] 58 testes unitários passam
- [ ] Schema aplica sem erro no startup
- [ ] 4 tabelas catalog_* existem em Postgres
- [ ] CRUD básico via Repository funciona (script seção 4)
- [ ] Regressão das telas existentes OK (sem 500)
- [ ] /api/health retorna 200

---

# Smoke Test — PR 2 (API CRUD de entries)

Adicionado: `app/routes/catalog.py` com 5 endpoints REST montados em `/api/v1/catalog`.

## 6 — Testes unitários (sem banco)

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 117 passed (PR 1 + PR 2). Cobre `is_root`, `_user_domains`,
`can_user_see` (12 cenários de visibility), `db_row_to_entry_dict` (parsing
JSON), e plumbing HTTP de todos os 5 endpoints com auth mockada.

## 7 — Endpoints REST (com app rodando)

Pré: login em `/login` para ter cookie `user_id`.

### POST — criar entry draft

```powershell
curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" `
  -b "user_id=<seu-user-id>" `
  -d '{"name":"Agente Smoke","kind":"agent","artifact_type":"agent","artifact_id":"<id-de-agent-existente>","version":"0.1.0"}'
```

**Esperado**: 201 com body contendo `id`, `urn=urn:maestro:default:agent:agente-smoke:0.1.0`, `status="draft"`, `owner_user_id=<seu-id>`.

### POST — rejeita kind=agent sem artifact link → 422

```powershell
curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" -b "user_id=<id>" `
  -d '{"name":"X","kind":"agent","version":"1.0.0"}'
```

**Esperado**: 422 com detail mencionando "vínculo".

### POST — external_platform sem artifact link → 201

```powershell
curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" -b "user_id=<id>" `
  -d '{"name":"ChatGPT Enterprise","kind":"external_platform"}'
```

**Esperado**: 201.

### GET — list paginado

```powershell
curl "http://localhost:7000/api/v1/catalog/entries?limit=10" -b "user_id=<id>"
```

**Esperado**: `{"entries": [...], "total": N, "limit": 10, "offset": 0}`. Só entries visíveis ao user (regras `can_user_see`).

### GET single → 404 se não visível

Logue como user B e tente acessar entry criada por user A em status='draft'. Esperado: 404 (não vaza existência).

### PUT — só draft, só owner/root

```powershell
curl -X PUT http://localhost:7000/api/v1/catalog/entries/<id> `
  -H "Content-Type: application/json" -b "user_id=<id>" `
  -d '{"description":"editado"}'
```

**Esperado**: 200 se draft e owner/root. 409 se status != draft. 403 se outro user.

### DELETE — só draft/archived, só owner/root

```powershell
curl -X DELETE http://localhost:7000/api/v1/catalog/entries/<id> -b "user_id=<id>"
```

**Esperado**: 200 se draft/archived e owner/root. 409 se published/etc. 403 se outro user.

## 8 — Validar audit_log

```sql
SELECT entity_type, action, actor, details, created_at
FROM audit_log
WHERE entity_type = 'catalog_entry'
ORDER BY created_at DESC LIMIT 10;
```

**Esperado**: 1 row por created/updated/deleted, com `actor=<user_id>` e `details` JSON com URN/changed_keys.

## Critérios de aceitação do PR 2

- [x] 117 testes unitários passam (58 do PR 1 + 59 novos)
- [x] 5 rotas montadas em `/api/v1/catalog/entries`
- [ ] POST cria entry com URN gerado e status='draft'
- [ ] POST 422 quando agent/skill sem artifact_id
- [ ] GET single 404 quando user não tem visibilidade
- [ ] PUT 403 para não-owner não-root
- [ ] PUT 409 quando status != 'draft'
- [ ] DELETE 409 quando status not in ('draft','archived')
- [ ] audit_log popula em create/update/delete
- [ ] Regressão das telas/endpoints existentes OK

---

# Smoke Test — PR 3 (Workflow: submit → decide → publish → deprecate)

6 endpoints novos para o ciclo de aprovação Root e transições de lifecycle.

## 9 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 152 passed (117 anteriores + 35 novos). Cobre `run_prechecks`
(11 cenários — name/desc/version/owner/disclosure/visibility/adapter) e
plumbing de submit/decide/publish/deprecate/queue/submissions-history.

## 10 — Workflow end-to-end com banco

Pré: app rodando, login como user comum (`u1`), e segundo user Root (`root1`).

### a. Cria draft e submete

```powershell
# Login como u1
$cookies = "user_id=<u1>"

$body = '{"name":"Smoke Workflow","kind":"agent","artifact_type":"agent","artifact_id":"<agent-id-real>","description":"Entry para validar fluxo de aprovação","version":"0.1.0"}'

# Cria
$resp = curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" -b $cookies -d $body
$eid = ($resp | ConvertFrom-Json).id

# Submete
curl -X POST "http://localhost:7000/api/v1/catalog/entries/$eid/submit" `
  -H "Content-Type: application/json" -b $cookies -d '{"notes":"primeira submissão"}'
```

**Esperado** no submit: status 201, body contém `submission_id`, `entry_status="submitted"`, `precheck_report` com lista de checks.

### b. Verifica fila (Root)

```powershell
$cookies_root = "user_id=<root1>"
curl "http://localhost:7000/api/v1/catalog/submissions/queue?status=pending" -b $cookies_root
```

**Esperado**: total ≥ 1, item com `review_status="pending"`, `precheck_report` JSON.

### c. Aprova

```powershell
$sid = "<submission_id capturado em a>"
curl -X POST "http://localhost:7000/api/v1/catalog/submissions/$sid/decide" `
  -H "Content-Type: application/json" -b $cookies_root `
  -d '{"decision":"approved","notes":"liberado"}'
```

**Esperado**: 200, `entry_status="approved"`, submission persiste com `reviewed_by`, `reviewed_at`.

### d. Owner publica

```powershell
curl -X POST "http://localhost:7000/api/v1/catalog/entries/$eid/publish" -b $cookies
```

**Esperado**: 200, entry agora com `status="published"`, `published_at` preenchido.

### e. Owner deprecia

```powershell
curl -X POST "http://localhost:7000/api/v1/catalog/entries/$eid/deprecate" -b $cookies
```

**Esperado**: 200, `status="deprecated"`, `deprecated_at` preenchido.

### f. Histórico de submissões

```powershell
curl "http://localhost:7000/api/v1/catalog/entries/$eid/submissions" -b $cookies
```

**Esperado**: total=1, snapshot+precheck_report+review_status visíveis.

## 11 — Validar audit_log do workflow

```sql
SELECT action, actor, details->>'submission_id' AS sub_id, created_at
FROM audit_log
WHERE entity_type = 'catalog_entry'
  AND action IN ('submitted','review_approved','review_rejected','review_changes_requested','published','deprecated')
ORDER BY created_at DESC LIMIT 20;
```

**Esperado**: linhas para cada transição executada acima, com actor correto.

## Critérios de aceitação do PR 3

- [x] 152 testes unitários passam (117 anteriores + 35 novos)
- [x] 11 rotas registradas em `/api/v1/catalog` (5 PR 2 + 6 PR 3)
- [ ] Fluxo a→f completo sem erro
- [ ] Não-owner / não-root bloqueado em submit/publish/deprecate (403)
- [ ] Não-root bloqueado em decide e queue (403)
- [ ] Transições inválidas retornam 409 (ex: deprecate sem publish)
- [ ] audit_log popula com 'submitted', 'review_*', 'published', 'deprecated'
- [ ] Regressão das telas/endpoints existentes OK

---

# Smoke Test — PR 4 (Capability Disclosure CRUD)

3 endpoints novos para a etiqueta nutricional R6.3. Pré-check
`capability_disclosure_present` agora é **error**, não warning — torna
disclosure efetivamente obrigatória para Root aprovar.

**Bug fix lateral**: corrige uso de `catalog_disclosure_repo.find_by_id`
no submit do PR 3 (Repository hardcoda `WHERE id=$1`, mas disclosure tem
`entry_id` como PK — quebraria em prod). PR 4 substitui pelo helper
especializado `get_disclosure` com SQL correto.

## 12 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 171 passed (152 anteriores + 19 novos). Cobre:
- `test_missing_disclosure_is_error` (upgrade severity)
- `test_submit_passes_prechecks_when_disclosure_declared` (loop fechado)
- 17 testes em `TestCapabilityPut`, `TestCapabilityGet`, `TestCapabilityDelete`

## 13 — Workflow com disclosure end-to-end

```powershell
$cookies = "user_id=<u1>"

# a. Criar entry
$body = '{"name":"Entry com Disclosure","kind":"agent","artifact_type":"agent","artifact_id":"<agent-id>","description":"Validando ciclo completo com R6.3","version":"0.1.0"}'
$eid = (curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" -b $cookies -d $body | ConvertFrom-Json).id

# b. Tenta submeter SEM disclosure → precheck reporta error
curl -X POST "http://localhost:7000/api/v1/catalog/entries/$eid/submit" `
  -H "Content-Type: application/json" -b $cookies -d '{}' | ConvertFrom-Json | Select-Object precheck_report

# Reset para draft (ou crie nova entry)
# c. Declara disclosure
$cap = '{"reads_user_kb":true,"calls_external_apis":true,"external_apis_list":["https://api.openai.com"],"processes_pii":true,"stores_input":true,"storage_retention_days":30,"data_residency":"BR","additional_notes":"Anonimização aplicada"}'
curl -X PUT "http://localhost:7000/api/v1/catalog/entries/$eid/capability" `
  -H "Content-Type: application/json" -b $cookies -d $cap

# d. GET disclosure (transparência — qualquer um que vê a entry pode ler)
curl "http://localhost:7000/api/v1/catalog/entries/$eid/capability" -b $cookies

# e. Submete agora — precheck passa
curl -X POST "http://localhost:7000/api/v1/catalog/entries/$eid/submit" `
  -H "Content-Type: application/json" -b $cookies -d '{}'
```

**Esperado**:
- (b) `precheck_report.errors_count >= 1`, com check `capability_disclosure_present` falhando severity=error
- (c) 200 com payload de retorno
- (d) 200 com disclosure parseada (external_apis_list como list, não string)
- (e) `precheck_report.passed=true` (assumindo demais campos OK)

## 14 — Validações Pydantic

```powershell
# external_apis_list vazia com flag true → 422
curl -X PUT "http://localhost:7000/api/v1/catalog/entries/$eid/capability" `
  -H "Content-Type: application/json" -b $cookies `
  -d '{"calls_external_apis":true,"external_apis_list":[]}'

# storage_retention_days negativo → 422
curl -X PUT "http://localhost:7000/api/v1/catalog/entries/$eid/capability" `
  -H "Content-Type: application/json" -b $cookies `
  -d '{"stores_input":true,"storage_retention_days":-1}'
```

**Esperado**: 422 em ambos.

## 15 — Audit log capability

```sql
SELECT action, actor, details, created_at
FROM audit_log
WHERE entity_type = 'catalog_entry'
  AND action IN ('capability_declared','capability_removed')
ORDER BY created_at DESC LIMIT 10;
```

**Esperado**: 1+ row por chamada PUT/DELETE, details com flags-chave (processes_pii, calls_external_apis, data_residency).

## Critérios de aceitação do PR 4

- [x] 171 testes unitários passam (152 anteriores + 19 novos)
- [x] 14 rotas registradas em `/api/v1/catalog` (11 PR 3 + 3 PR 4)
- [x] Pré-check `capability_disclosure_present` agora severity=error
- [ ] PUT cria disclosure e retorna 200
- [ ] PUT bloqueia (409) se entry status != 'draft'
- [ ] PUT 422 quando calls_external_apis=true + lista vazia
- [ ] GET transparente (qualquer usuário que vê a entry vê a disclosure)
- [ ] DELETE só draft, só owner/root
- [ ] Submit reporta error em disclosure ausente; passa quando declarada
- [ ] Regressão de PR 1-3 OK

---

# Smoke Test — PR 5 (UI Catálogo A1)

Primeiro trilho de UI. Tela `/catalog` com grid de cards, busca client-side
e filtros server-side. Nova entrada na nav (categoria Principal, após Skills).

## 16 — Renderização do template

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 171 passed (sem regressão).

```powershell
.venv\Scripts\python.exe -c "from app.main import app; print('/catalog' in [r.path for r in app.routes])"
```

**Esperado**: `True`.

## 17 — Navegação no browser

1. Login em `/login`
2. Sidebar mostra "Catálogo" como item novo em **Principal** (após Skills)
3. Click → carrega `/catalog`
4. Header mostra "Catálogo" + descrição
5. Item da sidebar fica destacado (active state)

## 18 — Lista vazia (instalação nova)

Sem entries cadastradas:
- Card grande "Catálogo vazio" no centro
- CTA aponta para `/agents` para criar agente que será publicado

## 19 — Lista com entries

Pré: criar 2-3 entries via API (ver seção 7 do PR 2):
```powershell
# Entry pública para u1
curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" -b "user_id=<u1>" `
  -d '{"name":"Agente Fiscal","kind":"agent","artifact_type":"agent","artifact_id":"<aid>","domain":"fiscal","description":"Classifica NF por CFOP","visibility":"company","version":"1.0.0"}'
# Publicar (precisa passar pelo workflow PR 3 — ou setar status='published' manualmente em SQL para o smoke)
```

**Esperado**:
- Grid mostra cards com nome, URN truncado, badges (kind/version/status/domain)
- Trust metrics (reliability, p95, custo) — "—" se zerado
- Descrição truncada em 2 linhas
- Tags se houver
- Hover destaca borda azul

## 20 — Busca client-side

Digite no campo de busca: filtra cards já carregados (nome / descrição / URN).
Contador "X / Y" no header atualiza.

## 21 — Filtros server-side

- **Tipo**: agent / skill / recipe / application / external_platform — recarrega via API
- **Status**: draft / submitted / approved / published / deprecated / archived
- **Domínio**: input texto livre

Cada filtro dispara `load()` novamente (watcher em Alpine.init).

## 22 — Click no card

Card linka para `/catalog/{id}` — rota será criada no PR 6. Por enquanto, devolve 404 — comportamento esperado.

## 23 — Visibilidade

- User comum vê apenas suas próprias entries (qualquer status) + visibility=company published/deprecated + department-match
- Root vê tudo
- Testar logando com 2 users diferentes

## Critérios de aceitação do PR 5

- [x] 171 testes passam (sem regressão)
- [x] Rota `/catalog` registrada em `app.routes`
- [x] Templates parseiam (catalog.html + base.html)
- [ ] Item "Catálogo" aparece na sidebar em Principal
- [ ] Page carrega entries via `/api/v1/catalog/entries`
- [ ] Busca filtra em tempo real (client-side)
- [ ] Filtros tipo/status/domínio recarregam (server-side)
- [ ] Cards mostram trust metrics
- [ ] Empty state quando catálogo vazio
- [ ] Click em card → tenta /catalog/{id} (404 até PR 6)

---

# Smoke Test — PR 6 (UI Detalhe da Entry A2)

Página `/catalog/{entry_id}` com tabs, ações contextuais e 2 modais
(editar capability + decidir submissão Root).

## 24 — Renderização do template + rota

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe -c "from app.main import app; print('/catalog/{entry_id}' in [r.path for r in app.routes])"
```

**Esperado**: 171 passed, rota `/catalog/{entry_id}` presente.

## 25 — Fluxo visual end-to-end

Pré: app rodando, login como `u1`, ter uma entry draft criada (PRs 2-4).

1. Acesse `/catalog` → click em uma entry → carrega `/catalog/{id}`
2. **Header**: nome grande + URN monospace + 3 badges (status, kind, version)
3. **Stats row**: owner, steward, domínio, visibilidade, data criação
4. **Action menu** (direita, contextual):
   - Status=draft + owner/root: **Submeter** + **Declarar Disclosure**
   - Status=submitted + Root: **Aprovar** / **Pedir Mudanças** / **Rejeitar**
   - Status=approved + owner/root: **Publicar**
   - Status=published + owner/root: **Depreciar**
5. **Tabs**: Visão Geral / Capability Disclosure / Histórico / Manifest

## 26 — Tab Capability Disclosure (diferencial UX)

Quando disclosure não existe:
- Card amarelo "Disclosure ainda não declarada"
- Botão "Declarar Agora" (se canMutate + draft)

Quando disclosure existe:
- Header com soberania + método de verificação
- 4 cards de categoria:
  - 🔐 Dados do consumer (reads_user_kb, writes_user_kb, stores_input + retention)
  - 🌐 Integrações externas (calls_external_apis + lista, accesses_internet)
  - ⚖️ Dados regulados (PII, financial, health)
  - 🧠 Modelo (trains_on_input, output_is_deterministic)
- Cada linha: label + badge Sim (rose/amber se high-severity, emerald para "Não")
- Notas adicionais ao final

## 27 — Modal editar capability

1. Click "Declarar Disclosure" → abre modal
2. Checkboxes agrupados por categoria
3. Condicionais:
   - `calls_external_apis=true` → textarea "APIs externas (uma por linha)"
   - `stores_input=true` → input numérico "Retenção (dias)"
4. Select "Soberania" (BR/EU/US/global ou Sem restrição)
5. Textarea "Notas adicionais"
6. Validações:
   - calls_external_apis=true + lista vazia → 422 + toast erro
   - retention negativo → 422 + toast erro
7. Save → modal fecha + toast "Disclosure salva" + recarrega

## 28 — Modal decidir (Root)

Pré: entry está submitted, logar como Root.

1. Click "Aprovar" / "Pedir Mudanças" / "Rejeitar" → abre modal
2. Header explica consequência (entry → approved / draft)
3. Textarea para notas
4. Confirmar → POST /submissions/{sid}/decide → toast + recarrega

## 29 — Tab Histórico (submissões)

- Não-owner não-root: card "visível apenas para owner ou Root"
- Owner/Root: lista de submissões com badges, datas, notas, pré-checks expansível (`<details>`)

## 30 — Tab Manifest (debug)

JSON cru da entry em bloco escuro com sintaxe monoespaçada.

## 31 — Fluxo completo via UI

1. Owner cria entry via API → vai para `/catalog/{id}` → status=draft
2. Click "Declarar Disclosure" → preenche modal → salva
3. Click "Submeter" → status=submitted, aparece na fila (futura PR 8)
4. Logar como Root → mesma página → status=submitted → Aprovar
5. Voltar como owner → status=approved → Publicar
6. Click "Depreciar" → status=deprecated

Todo o ciclo de governança pela UI, sem cURL.

## Critérios de aceitação do PR 6

- [x] 171 testes passam (sem regressão)
- [x] Rota `/catalog/{entry_id}` registrada
- [x] Template parseia
- [ ] Tab "Visão Geral" mostra trust metrics + adapter + tags
- [ ] Tab "Capability Disclosure" mostra etiqueta nutricional bonita
- [ ] Tab "Histórico" lista submissões com pré-checks expansível
- [ ] Tab "Manifest" mostra JSON cru
- [ ] Botões de ação aparecem só para status + role corretos
- [ ] Modal capability salva via PUT, valida calls_external_apis
- [ ] Modal decide (Root) envia POST decide e recarrega
- [ ] Toasts de erro/sucesso aparecem corretamente

---

# Smoke Test — PR 7 (UI Wizard de Submissão B1)

Wizard `/catalog/publish` em 4 passos: artefato → metadata → disclosure → revisão.

## 32 — Renderização + rota

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe -c "from app.main import app; print(sorted([r.path for r in app.routes if r.path.startswith('/catalog')]))"
```

**Esperado**:
- 171 passed
- Rotas: `['/catalog', '/catalog/publish', '/catalog/{entry_id}']`

**Importante**: `/catalog/publish` precisa estar registrada ANTES de `/catalog/{entry_id}` para o match literal ganhar do path parameter.

## 33 — Acesso pelo catálogo

1. `/catalog` → header tem botão azul "Publicar no Catálogo" (era cinza apontando para /agents)
2. Click → vai para `/catalog/publish`
3. Page abre com stepper visual (4 círculos numerados ligados por barras)
4. Step 1 ativo (azul), demais cinzas

## 34 — Step 1: selecionar artefato

- Lista combinada de agents + skills (badge colorido distingue)
- Filtros: busca por nome, "Apenas agentes" / "Apenas skills"
- Click no radio seleciona, destaca borda azul
- Pré-preenche metadata (name, description, domain, version) — visível ao avançar
- Botão "Próximo" só habilita quando algo está selecionado

## 35 — Step 2: metadata

- Campos vêm preenchidos com dados do artefato
- Versão precisa ser semver (`1.0.0`) — botão Próximo desabilita se inválida
- Visibilidade: select de 3 opções; "Departamento" expõe campo de scope
- Preview do URN gerado abaixo (atualiza em tempo real)

## 36 — Step 3: capability disclosure

- 4 grupos de checkboxes idênticos ao modal do detalhe
- Campos condicionais:
  - `calls_external_apis=true` → textarea de URLs (obrigatório se flag, validação client-side bloqueia Próximo)
  - `stores_input=true` → input numérico (validação client-side ≥ 0)
- Select de soberania
- Textarea de notas adicionais

## 37 — Step 4: revisão + submit

- Resumo dos dados-chave (artefato, URN, visibilidade, steward, capabilities count, soberania)
- Card amarelo explicando o que vai acontecer (create + capability + submit)
- Click "Confirmar e Submeter para Revisão":
  - 3 chamadas sequenciais à API
  - Sucesso → redireciona para `/catalog/{id}`
  - Falha → exibe erro com info de qual step falhou (entry pode ter ficado em estado parcial — mensagem orienta usuário)

## 38 — Fluxo end-to-end

1. Owner cria agente novo em `/agents/new`
2. Vai para `/catalog/publish`
3. Seleciona o agente recém-criado
4. Confirma metadata
5. Declara disclosure (mínimo: nada marcado, sem soberania)
6. Revisa → Submete
7. Redireciona para `/catalog/{id}` → status=submitted
8. Logar como Root → mesma página → aprova
9. Owner volta → status=approved → publica

## Critérios de aceitação do PR 7

- [x] 171 testes passam (sem regressão)
- [x] Rota `/catalog/publish` registrada antes de `/catalog/{entry_id}`
- [x] Template parseia
- [ ] Stepper visual marca step ativo e completed
- [ ] Step 1 lista agents + skills com busca/filtro
- [ ] Pick de artefato pré-preenche step 2
- [ ] Validações client-side (semver, external_apis_list, retention)
- [ ] Submit final faz 3 chamadas em sequência
- [ ] Redireciona para `/catalog/{id}` em sucesso
- [ ] Em erro, mensagem orienta (entry pode ter ficado parcial)
- [ ] Botão "Publicar no Catálogo" na lista do catálogo aponta para `/catalog/publish`

---

# Smoke Test — PR 8 (UI Fila de Aprovação Root C1)

Tela `/catalog/queue` exclusiva para Root. Lista submissões com pré-checks
visuais e ações inline (sem precisar abrir cada entry).

## 39 — Renderização + rotas

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe -c "from app.main import app; print(sorted([r.path for r in app.routes if r.path.startswith('/catalog')]))"
```

**Esperado**:
- 171 passed
- Rotas: `['/catalog', '/catalog/publish', '/catalog/queue', '/catalog/{entry_id}']`

## 40 — Nav item condicional

1. Logar como user comum → sidebar NÃO mostra "Fila Root"
2. Logar como Root → sidebar mostra item "Fila Root (revisão)" abaixo de Catálogo
3. Click → carrega `/catalog/queue`

## 41 — Acesso negado para não-Root

1. Logar como user comum
2. Acessar manualmente `/catalog/queue` (URL direto)
3. **Esperado**: card "Acesso restrito" + link para voltar ao catálogo

## 42 — Fila vazia

Sem submissões pendentes:
- Card "Nenhuma submissão pendente"
- Subtítulo explicativo

## 43 — Fila com submissões

Pré: ter 2+ entries submetidas (PR 7 ou cURL).

- Tab "Pendentes" mostra contador
- Cada item:
  - Nome da entry como link para `/catalog/{id}`
  - Badges (kind, version)
  - Submetida por + data
  - Pré-checks: badges resumidos (X erros, Y avisos, ou "OK")
  - `<details>` expansível mostrando cada check com bullet colorido
  - **Capability declarada** preview (flags coloreadas por severidade)
  - **Botões inline**: Aprovar / Pedir Mudanças / Rejeitar

## 44 — Modal de decisão

1. Click em qualquer botão → abre modal
2. Header mostra entry name + texto contextual
3. Textarea para notas (especialmente recomendado em rejected/changes_requested)
4. Confirmar → POST /submissions/{sid}/decide → toast + recarrega fila

## 45 — Tabs de status

- "Aprovadas": entries que viraram approved
- "Mudanças solicitadas": entries que voltaram para draft
- "Rejeitadas": entries rejeitadas

Cada item nas tabs decididas mostra reviewer + data + notes.

## 46 — Fluxo completo end-to-end

1. User comum submete entry (PR 7)
2. Root abre `/catalog/queue` → vê pré-checks
3. Decide pela UI (Aprovar/Mudanças/Rejeitar)
4. Tab "Pendentes" atualiza, item migra para tab apropriada
5. User recebe (não há notificação ainda — vê no /catalog/{id})

## Critérios de aceitação do PR 8

- [x] 171 testes passam (sem regressão)
- [x] Rota `/catalog/queue` registrada
- [x] Template parseia
- [ ] Nav item "Fila Root" aparece SÓ para role=root
- [ ] Não-Root acessando `/catalog/queue` vê "Acesso restrito"
- [ ] Lista paginada de submissões com filtro por status
- [ ] Pré-checks resumidos + detalhe expansível
- [ ] Capability declarada exibe flags coloreadas
- [ ] Botões inline funcionam (modal + POST decide + recarrega)
- [ ] Tabs decididas mostram reviewer/data/notes

---

# Smoke Test — PR 9 (Integrações + Dashboard)

Penúltimo PR. Integra o catálogo com Agentes, Skills, Dashboard, e
permite pre-fill via query string no wizard de publish.

## 47 — Renderização

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 171 passed.

Templates afetados (todos parseiam):
- `pages/dashboard.html`
- `pages/agents.html`
- `pages/skills.html`
- `pages/catalog_publish.html`

## 48 — Botão "Publicar no Catálogo" em Agentes/Skills

1. Acesse `/agents` → hover em qualquer linha → novo botão (ícone cubo) entre "Editar" e "Toggle status"
2. Click → vai para `/catalog/publish?kind=agent&artifact_id={agent_id}`
3. Wizard abre **já no Step 2** (metadata) — Step 1 foi auto-completado
4. Metadata pré-preenchida com nome/desc/version/domain do agente
5. Mesmo fluxo em `/skills` (kind=skill)

## 49 — Card Catálogo no Dashboard

Acesse `/` (Dashboard). Abaixo dos 3 cards de topologia (AOBD/AR/SA),
nova seção "Catálogo Corporativo":

- **Publicadas**: count em verde (entries visíveis a consumers)
- **Em revisão**: count em amarelo se >0 (submetidas, aguardando Root)
- **Minhas drafts**: count das suas entries em status=draft
- **Fila Root** (só visível para Root quando há pendentes): bloco amarelo clicável que leva a `/catalog/queue`. Não-Root vê "Total" no lugar.

Links: "Publicar →" e "Ver catálogo".

## 50 — Pre-fill via query params

Manual:
```
/catalog/publish?kind=agent&artifact_id=<id-real>
```
**Esperado**: Wizard pula para Step 2 com metadata pré-preenchida.

Edge cases:
- ID inexistente → fica no Step 1 (pick manual)
- kind inválido → ignora pre-fill, fica no Step 1

## Critérios de aceitação do PR 9

- [x] 171 testes passam (sem regressão)
- [x] Templates parseiam
- [ ] Botão "Publicar no Catálogo" aparece em agents/skills e funciona
- [ ] Wizard pula para Step 2 quando recebe query params válidos
- [ ] Card "Catálogo Corporativo" aparece no dashboard
- [ ] Cards mostram contagens corretas (Publicadas / Em revisão / Minhas drafts)
- [ ] Bloco "Fila Root" só aparece quando Root + há pendentes
- [ ] Links "Publicar →" e "Ver catálogo" navegam corretamente

═══════════════════════════════════════════════════════════════════
# ONDA 2
═══════════════════════════════════════════════════════════════════

# Smoke Test — Onda 2 / PR 1 (External Platforms backend)

Adiciona suporte a kind=external_platform com metadata vendor/contrato/custo.
Catalog continua funcionando para os outros kinds — zero breaking change.

## 51 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 196 passed (171 Onda 1 + 25 Onda 2/PR1). Novos:
- `TestExternalMetadataCheck` (4 testes em prechecks)
- `TestExternalPlatformMetadata` (8 testes em models)
- `TestExternalMetadataPut` + `TestExternalMetadataGet` (13 testes em api)

## 52 — Schema migrado

```sql
\d catalog_external_metadata
```

**Esperado**: tabela com 13 colunas, PK=entry_id, FK→catalog_entries com CASCADE, CHECK em contract_status.

## 53 — Endpoints novos

```powershell
.venv\Scripts\python.exe -c "from app.main import app; print(sorted([r.path for r in app.routes if 'external-metadata' in r.path]))"
```

**Esperado**: 2 rotas (`GET` e `PUT` em `/api/v1/catalog/entries/{entry_id}/external-metadata`).

## 54 — Fluxo end-to-end (cURL)

```powershell
$cookies = "user_id=<u1>"

# a. Cria entry external_platform (sem artifact link)
$body = '{"name":"ChatGPT Enterprise","kind":"external_platform","adapter_type":"openai_assistants","description":"ChatGPT Enterprise — license para o time de Eng"}'
$eid = (curl -X POST http://localhost:7000/api/v1/catalog/entries `
  -H "Content-Type: application/json" -b $cookies -d $body | ConvertFrom-Json).id

# b. Tenta GET → 404 (ainda não declarado)
curl "http://localhost:7000/api/v1/catalog/entries/$eid/external-metadata" -b $cookies

# c. Tenta PUT sem vendor → 422
curl -X PUT "http://localhost:7000/api/v1/catalog/entries/$eid/external-metadata" `
  -H "Content-Type: application/json" -b $cookies `
  -d '{"contract_status":"active"}'

# d. PUT com vendor → 200
$meta = '{"vendor":"OpenAI","vendor_url":"https://openai.com","contract_status":"active","contract_renewal_date":"2026-12-31","monthly_cost_usd":15000,"vendor_contact":"enterprise@openai.com","approved_use_cases":"Code generation, docs","restrictions":"No PII data without anonymization"}'
curl -X PUT "http://localhost:7000/api/v1/catalog/entries/$eid/external-metadata" `
  -H "Content-Type: application/json" -b $cookies -d $meta

# e. GET → metadata completa
curl "http://localhost:7000/api/v1/catalog/entries/$eid/external-metadata" -b $cookies

# f. PUT update parcial (sem vendor — mantém valor)
curl -X PUT "http://localhost:7000/api/v1/catalog/entries/$eid/external-metadata" `
  -H "Content-Type: application/json" -b $cookies `
  -d '{"monthly_cost_usd":18000}'
```

**Esperado**:
- (b) 404 sem corpo de erro estranho
- (c) 422 "vendor é obrigatório..."
- (d) 200 com payload completo
- (e) 200 igual ao salvo
- (f) 200 com vendor preservado + cost atualizado

## 55 — Pré-check funciona

```powershell
# Submete entry external_platform sem metadata
curl -X POST "http://localhost:7000/api/v1/catalog/entries/$eid/submit" `
  -H "Content-Type: application/json" -b $cookies -d '{}'
```

Verificar no `precheck_report`: check `external_metadata_present` falha com
severity=warning. Submit prossegue (não bloqueia) mas Root vê o warning.

## 56 — Tabela CASCADE funciona

```powershell
# DELETE entry → metadata externa some junto
curl -X DELETE "http://localhost:7000/api/v1/catalog/entries/$eid" -b $cookies
```

Verificar em SQL: `SELECT * FROM catalog_external_metadata WHERE entry_id='<eid>'` → 0 rows.

## Critérios de aceitação Onda 2 / PR 1

- [x] 196 testes unitários passam (171 + 25 novos)
- [x] Tabela `catalog_external_metadata` criada via SCHEMA
- [x] 2 endpoints novos registrados (`GET` + `PUT` `/external-metadata`)
- [x] Pré-check `external_metadata_present` adicionado (warning para external_platform)
- [ ] CRUD end-to-end OK
- [ ] CASCADE delete funciona
- [ ] Regressão Onda 1 OK (171 testes anteriores continuam passando)

---

# Smoke Test — Onda 2 / PR 2 (External Platforms UI)

Habilita criar e visualizar plataformas externas pela UI. Sem mudança backend.

## 57 — Renderização

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 196 passed (sem regressão).

## 58 — Detalhe da Entry (tab nova)

Pré: criar entry com kind=external_platform via API.

1. Acesse `/catalog/{id}` da entry external_platform
2. **Esperado**: aparece tab "Metadata Externa" entre Capability Disclosure e Histórico
3. Para entries não-external (agent/skill), a tab NÃO aparece
4. Click na tab:
   - Sem metadata: card vermelho "Metadata externa não declarada" + botão "Declarar Agora" (se canMutate + draft)
   - Com metadata: cards organizados (Vendor + Site / Contrato / Aprovação / Casos de uso / Restrições)
5. Click "Declarar Agora" ou "Editar":
   - Modal com vendor (obrigatório), URL, contrato (status + renewal date), custo, contato, casos de uso, restrições
   - Save → PUT external-metadata → toast + recarrega

## 59 — Wizard de Publish: opção External Platform

Acesse `/catalog/publish`:

1. **Step 1**: card vermelho destacado no topo "Registrar Plataforma Externa" + badge "novo"
2. Click no radio → seleciona kind=external_platform; abaixo, lista de artefatos internos fica deselecionada
3. Click "Próximo" → vai para Step 2 (metadata padrão)
4. **Step 3**: aparece bloco vermelho extra acima do disclosure: "Metadata da Plataforma Externa" com vendor (obrigatório), status contrato, custo, restrições. Validação client-side bloqueia Próximo se vendor vazio.
5. **Step 4**: revisão mostra dados normais (sem section especial — refine no detail)
6. **Submit final**: 4 chamadas em sequência:
   - POST `/entries` (cria)
   - PUT `/external-metadata` (Onda 2 — só quando external)
   - PUT `/capability` (sempre)
   - POST `/submit`
7. Sucesso → redireciona para `/catalog/{id}` → tab "Metadata Externa" já populada

## 60 — Fluxo end-to-end UI

1. `/catalog/publish` → click radio external → Próximo
2. Step 2: name="ChatGPT Enterprise", description="ChatGPT Enterprise...", version="1.0.0"
3. Step 3: vendor="OpenAI", contrato status=active, custo=15000, restrições="sem PII"
4. Step 3: capability disclosure (default checkboxes OK)
5. Step 4: revisão → Submeter
6. Redirect para `/catalog/{id}` → 5 tabs visíveis incluindo "Metadata Externa"
7. Tab "Metadata Externa" mostra OpenAI + contrato + custo + restrições
8. Click "Editar" → modal → altera custo → salva → toast + recarrega

## Critérios de aceitação Onda 2 / PR 2

- [x] 196 testes passam (sem regressão)
- [x] Templates parseiam
- [ ] Tab "Metadata Externa" aparece APENAS para kind=external_platform
- [ ] Detail page exibe metadata em cards visuais
- [ ] Modal de edição funciona (PUT)
- [ ] Wizard mostra card de External Platform no Step 1
- [ ] Wizard exige vendor no Step 3 quando external
- [ ] Submit final encadeia 4 chamadas (com PUT external-metadata)
- [ ] Após submit, entry visível em /catalog com badge "Externa"

---

# Smoke Test — Onda 2 / PR 3 (Inventário Regulatório)

Relatório cross-entries para comitê de privacidade/segurança. 2 endpoints
+ 1 página + 1 nav item. Tudo gated por Root.

## 61 — Testes + rotas

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 202 passed (196 + 6 novos).

Endpoints: `/api/v1/catalog/inventory`, `/api/v1/catalog/inventory/export.csv`, UI `/catalog/inventory`. Nav "Inventário" abaixo de "Fila Root" gated por Root.

## 62 — Auth gate

| User | Recurso | Esperado |
|---|---|---|
| comum | `GET /inventory` | 403 |
| comum | `GET /inventory/export.csv` | 403 |
| comum | UI `/catalog/inventory` | "Acesso restrito" |
| root | tudo acima | 200 OK |

## 63 — Filtros tristate

URL: `/catalog/inventory?processes_pii=true&calls_external_apis=false&kind=external_platform&residency=BR`

Esperado: filtra entries com PII=true E externas APIs=false E kind=external_platform E residency=BR. Vazio = não filtra.

## 64 — Agregados rápidos

4 cards no topo: Processam PII (rosa) / Chamam APIs externas (âmbar) / Plataformas externas (azul) / Custo mensal USD (verde, sum). Atualizam com filtros.

## 65 — Export CSV

Click "Export CSV" → download `maestro-catalog-inventory-<timestamp>.csv` com 28 colunas. `external_apis_list` serializado com `; `. Datetimes em ISO string.

## Critérios de aceitação Onda 2 / PR 3

- [x] 202 testes passam (196 + 6 novos)
- [x] 3 rotas registradas
- [ ] Auth gate funciona (3 rotas)
- [ ] Filtros tristate (true/false/vazio)
- [ ] Agregados refletem filtros
- [ ] CSV baixa com 28 colunas e nome timestamp

---

# Smoke Test — Onda 2 / PR 4 (Stewardship Dashboard)

Visualiza saúde de entries agrupadas por área + ação reassign.

## 66 — Testes + rotas

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 213 passed (202 + 11 novos).

3 rotas: `GET /api/v1/catalog/stewardship`, `POST /entries/{id}/reassign`, UI `/catalog/stewardship`. Nav "Stewardship" gated por Root.

## 67 — Flags de saúde (SQL-derivadas)

| Flag | Critério |
|---|---|
| `is_orphan` | owner deletado OU `users.status != 'active'` |
| `is_stale` | `status='published'` AND último uso > 30 dias |
| `has_low_reliability` | `trust_reliability < 0.5` |

## 68 — Reassign

```powershell
# Root realoca owner + steward
curl -X POST http://localhost:7000/api/v1/catalog/entries/<eid>/reassign `
  -H "Content-Type: application/json" -b $cookies_root `
  -d '{"new_owner_user_id":"<user-id-valido>","new_steward_team":"rh"}'
```

**Esperado**:
- 200 com payload atualizado
- 422 se new_owner_user_id não existe
- 422 se payload vazio
- Audit `stewardship_reassigned` com `details.{owner,steward_team}.{from,to}`
- `new_steward_team=""` limpa o campo (vira NULL)

## 69 — UI

- 4 cards totais (áreas / órfãs / paradas / baixa conf.)
- Filtro por área
- Cards por team com entries listadas
- Flags visuais (rosa/amber/violet) por entry
- Botão "Realocar" abre modal

## Critérios de aceitação Onda 2 / PR 4

- [x] 213 testes passam (202 + 11 novos)
- [x] 3 rotas registradas
- [ ] Auth gate (não-Root bloqueado)
- [ ] Flags is_orphan / is_stale / has_low_reliability detectadas
- [ ] Reassign valida user existe
- [ ] Audit `stewardship_reassigned` com from/to
- [ ] Nav "Stewardship" visível só para Root

---

# Smoke Test — Onda 2 / PR 5 (Bulk decide + filtros avançados)

Root processa N submissions em batch + filtros client-side adicionais.

## 70 — Testes + endpoint

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 221 passed (213 + 8 novos).

Novo endpoint: `POST /api/v1/catalog/submissions/bulk-decide`.

## 71 — Bulk approve em N submissions

```powershell
curl -X POST http://localhost:7000/api/v1/catalog/submissions/bulk-decide `
  -H "Content-Type: application/json" -b $cookies_root `
  -d '{"submission_ids":["sid1","sid2","sid3"],"decision":"approved","notes":"lote OK"}'
```

**Esperado** response:
```json
{
  "decision": "approved",
  "total": 3,
  "succeeded_count": 3,
  "failed_count": 0,
  "succeeded": ["sid1", "sid2", "sid3"],
  "failed": []
}
```

## 72 — Validações Pydantic

| Payload | Esperado |
|---|---|
| `submission_ids: []` | 422 |
| `submission_ids: ["a","a"]` | 422 (duplicatas) |
| `decision: "maybe"` | 422 |
| `submission_ids` com 101+ itens | 422 (max_length) |

## 73 — Falhas individuais não interrompem batch

Misture IDs válidos com inexistente:
```json
{"submission_ids": ["sid_valido", "nonexistent"], "decision": "approved"}
```
Esperado: `succeeded_count=1, failed_count=1`, `failed[0].reason="não encontrada"`.

## 74 — UI: checkbox + filtros

Na tela `/catalog/queue` (tab Pendentes):
- Painel novo de filtros: submitter (input) / kind (select) / capability (select) / residency (input)
- Contador "X / Y" mostra filtrados/total
- Checkbox por linha + Select-all no topo
- Bulk action bar aparece com seleção (Aprovar todas / Pedir mudanças / Rejeitar todas)
- Modal mostra resultado após submit (succeeded + failed list com motivos)

## 75 — Audit do bulk

```sql
SELECT action, details->>'bulk' AS bulk, COUNT(*)
FROM audit_log
WHERE entity_type = 'catalog_entry'
  AND action LIKE 'review_%'
GROUP BY action, bulk;
```

Esperado: linhas com `bulk=true` distinguíveis das individuais.

## Critérios de aceitação Onda 2 / PR 5

- [x] 221 testes passam (213 + 8 novos)
- [x] Endpoint `/bulk-decide` registrado
- [ ] Não-Root: 403
- [ ] Payload inválido: 422
- [ ] Falhas individuais isoladas (não bloqueiam batch)
- [ ] Filtros client-side reduzem visíveis sem nova request
- [ ] Bulk modal mostra resultado com falhas detalhadas
- [ ] audit_log com `details.bulk=true`

---

# Smoke Test — Onda 2 / PR 6 (Fechamento)

Sem código novo. Apenas documentação consolidada de fechamento.

## 76 — Documentação atualizada

- `docs/catalog/README.md` — métricas Onda 1+2 + status atual + reservado para Onda 3
- `docs/catalog/ONDA2.md` — novo, resumo da Onda 2 com mapa de PRs + delta
- `docs/catalog/REGRESSION.md` — adiciona Fase 6 (Onda 2)
- `docs/catalog/SMOKE_TEST.md` — seções 61-76 cobrindo todos os PRs Onda 2

## 77 — Sign-off final

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
```

**Esperado**: 221 passed.

## Critérios de aceitação Onda 2 / PR 6

- [x] 221 testes passam
- [x] ONDA2.md criado
- [x] README.md atualizado com métricas combinadas
- [x] REGRESSION.md tem Fase 6 com checklist Onda 2
- [x] SMOKE_TEST.md tem seções 61-77

**Onda 2 do Catálogo está pronta para sign-off e produção.**

---

# Smoke Test — Onda 4 / PR 1 (Execução real de recipes)

Primeiro PR da Onda 4. Recipes deixam de ser apenas manifest declarativo —
agora são executáveis via chain sequencial. Async (POST 202 + polling),
chain quebra com skip dos demais ao primeiro erro, cost auto-wire por step.

## 1 — Testes unitários (sem banco)

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 281 passed (257 Onda 1-3 + 24 novos do PR #67):
- `test_catalog_recipe_execution.py`:
  - **TestExecuteEndpoint** (7): 404 inexistente, 404 não-visível, 422 não-recipe, 409 draft, 422 sem manifest, 202 caminho feliz, 422 input vazio
  - **TestGetExecution** (5): 404 inexistente, 404 sem relação, consumer/owner/root podem ver
  - **TestListExecutions** (4): 404, 422, lista vazia, paginação
  - **Executor direto** (8): chain 3 steps, skip após falha, target inexistente, draft, kind=skill, sem artifact_id, cost auto-wire, ordenação defensiva

## 2 — Schema (com Postgres)

Subir a app aplica a tabela nova via `CREATE TABLE IF NOT EXISTS`.

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 7000
```

```sql
\dt catalog_recipe_executions
\d catalog_recipe_executions
```

**Esperado**: tabela com 12 colunas (id, recipe_entry_id, consumer_user_id,
input, steps_results JSONB, status com CHECK, total_cost_usd, total_latency_ms,
error_message, started_at, finished_at) + 3 índices em (recipe_entry_id,
started_at DESC), (consumer_user_id, started_at DESC) e (status).

## 3 — Fluxo via API (com Postgres)

Pré: ter um recipe `published` com manifest válido (ver smoke do PR #65).

```bash
# 1. Disparar execução — retorna 202 imediato
curl -X POST http://localhost:7000/api/v1/catalog/entries/<RECIPE_ID>/execute \
  -H "X-API-Key: <KEY>" -H "Content-Type: application/json" \
  -d '{"input":"oi, executa o pipeline"}'

# Esperado: 202 {execution_id, recipe_entry_id, status:"running", step_count, started_at}

# 2. Polling — repetir até status != "running"
curl http://localhost:7000/api/v1/catalog/executions/<EXEC_ID> \
  -H "X-API-Key: <KEY>"

# Esperado em sucesso: {status:"completed", steps_results:[...], total_cost_usd, total_latency_ms, finished_at}
# Esperado em falha de meio: {status:"partial", steps com mix de success/error/skipped}

# 3. Histórico do recipe
curl "http://localhost:7000/api/v1/catalog/entries/<RECIPE_ID>/executions?limit=10" \
  -H "X-API-Key: <KEY>"

# Esperado: {items:[...], limit, offset, has_more}
```

## 4 — Visibilidade

| Caso | Esperado |
|---|---|
| Recipe em draft → POST /execute | 409 (só published roda) |
| Recipe sem manifest → POST /execute | 422 |
| Entry kind=agent → POST /execute | 422 (só recipe) |
| Execution de outro user (não consumer/owner/root) → GET | 404 |

## 5 — Cost auto-wire

Após executar com sucesso ao menos 1 step:

```sql
SELECT entry_id, consumer_user_id, tokens_used, latency_ms, invoked_at
FROM catalog_costs
WHERE invoked_at > now() - interval '5 minutes'
ORDER BY invoked_at DESC;
```

**Esperado**: 1 row por step `success`. `tokens_used` reflete o total do
engine. `cost_usd` ainda persiste como `0` nesta onda (pricing table fica
para PR de cost auto-wire pleno).

## Critérios de aceitação Onda 4 / PR 1 (#67)

- [x] 281 testes passam (257 anteriores + 24 novos)
- [x] Tabela `catalog_recipe_executions` criada com 3 índices
- [x] 3 endpoints novos sob `/api/v1/catalog`:
      `POST /entries/{id}/execute`, `GET /executions/{id}`, `GET /entries/{id}/executions`
- [x] Executor faz chain (output[N-1] → input[N])
- [x] Falha de step quebra chain (demais → `skipped`); status final = `partial`
- [x] Crash do executor finaliza como `failed` (não fica `running` forever)
- [x] Cost auto-wire grava 1 row em `catalog_costs` por step `success`
- [x] Audit `recipe_execution_started` registrado
- [x] Zero breaking changes em PRs Onda 1-3
- [ ] Smoke manual com Postgres rodando (seções 2-5 desta) — pendente em homolog

---

# Smoke Test — Onda 4 / PR 2 (UI de execução de recipes)

Coloca rosto na execução real do PR #67. Tab nova "Execuções" em
`/catalog/{id}` (visível só p/ kind=recipe), modal de disparo e modal
de polling em tempo real.

## 1 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 281 passed (mesmo do PR #67 — este PR é só template, sem código novo testável via pytest).

## 2 — Fluxo via UI

Pré: ter um recipe `published` com manifest válido com ao menos 2 steps
publicados também (agents do catálogo).

1. Acessar `/catalog/{recipe_id}` como qualquer user que vê a entry.
2. **Verificar tab nova "Execuções"** aparece (só p/ kind=recipe).
3. Tab "Execuções" mostra:
   - Botão `▶ Executar recipe` (visível só se status='published' e manifest com steps).
   - Tabela de execuções anteriores (vazia inicialmente) ou empty state.
4. Clicar `▶ Executar recipe` → abre **modal de disparo** com textarea.
5. Digitar input + clicar **▶ Executar**.
6. **Modal de polling** abre automaticamente:
   - Status badge `running` + indicador "↻ polling" pulsando.
   - Steps aparecem progressivamente conforme executor avança.
   - Cada step mostra: nome do target, status colorido (success/error/skipped),
     output (truncado a 5000 chars), tokens, latência.
   - Botão **Fechar** fica disabled enquanto `running`.
7. Quando status vira `completed | partial | failed`:
   - Badge atualiza cor.
   - Polling para automaticamente.
   - Botão Fechar habilita.
   - Histórico no fundo da tab atualiza com nova entry.
8. Clicar **Ver** em uma execução do histórico → modal abre com drill-down
   da execução passada (sem polling — read-only).

## 3 — Validações de gating

| Cenário | Comportamento esperado |
|---|---|
| Entry kind=agent | Tab "Execuções" não aparece |
| Recipe em draft | Banner amber "só published é executável"; botão Executar oculto |
| Recipe sem steps | Banner amber "declare manifest"; botão Executar oculto |
| Recipe published com steps | Botão Executar visível para qualquer user que vê a entry |

## 4 — Polling resiliente

- Se GET /executions/{id} retornar 4xx/5xx, polling para automaticamente
  (não fica martelando).
- Esc fecha modal de disparo (se não está submitting).
- Esc no modal de polling: só fecha se status != running.

## Critérios de aceitação Onda 4 / PR 2

- [x] 281 testes continuam passando (sem regressão)
- [x] Tab "Execuções" condicional a `kind=recipe`
- [x] Botão `▶ Executar recipe` gated em `status='published'` + manifest com steps
- [x] Modal de disparo com textarea (1-50000 chars)
- [x] Modal de polling auto a cada 1.5s; para sozinho ao terminar
- [x] Drill-down de execuções históricas (read-only) reusa o mesmo modal
- [x] Status badges coloridos (running/completed/partial/failed/success/error/skipped)
- [x] Zero novo arquivo .py — apenas `catalog_detail.html` mudou
- [ ] Smoke manual no browser — pendente em homolog

---

# Smoke Test — Onda 4 / PR 3 (Cost pleno por provider/model)

Substitui o `cost_usd=0` placeholder do PR #67. Agora cada step success
calcula custo real baseado em `tokens.input × input_per_1k + tokens.output × output_per_1k`,
buscando pricing por `provider/model` do agent.

## 1 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 298 passed (281 anteriores + 17 novos):
- `test_llm_pricing.py` (15): lookup case-insensitive, calculo basico, input/output separados, ollama=0, anthropic claude-opus, maritaca sabia, modelo desconhecido → 0 + warning, estrutura consistente da tabela
- `test_catalog_recipe_execution.py` ajustes: mock default agora retorna tokens_input/tokens_output/provider/model; assertion adicional em cost_auto_wire valida cost_usd > 0 e provider/model registrados no step_result

## 2 — Pricing snapshot

Os preços são hardcoded em `app/core/llm_pricing.py`. Snapshot de 2026-05:

| Provider/Model | Input USD/1k | Output USD/1k |
|---|---|---|
| azure/gpt-4o | 0.0025 | 0.01 |
| azure/gpt-4o-mini | 0.00015 | 0.0006 |
| azure/gpt-4-turbo | 0.01 | 0.03 |
| anthropic/claude-opus-4-7 | 0.015 | 0.075 |
| anthropic/claude-sonnet-4-6 | 0.003 | 0.015 |
| maritaca/sabia-4 | 0.0005 | 0.0015 |
| ollama/* | 0 | 0 |

Modelo novo? Adicionar em `PRICING` dict + commit. Modelo desconhecido em
runtime → custo=0 + log WARNING (não derruba o fluxo).

## 3 — Validação end-to-end (com Postgres + LLM real)

Pré: recipe published com 2 steps apontando para agents reais.

1. Executar recipe via UI (`/catalog/{id}` → tab Execuções → ▶ Executar).
2. Aguardar polling terminar com status `completed`.
3. Verificar no modal de polling:
   - Cada step success mostra `tokens` > 0
   - Custo total agregado no header
4. Validar persistência:
```sql
SELECT entry_id, cost_usd, tokens_used, latency_ms
FROM catalog_costs
WHERE invoked_at > now() - interval '5 minutes'
ORDER BY invoked_at DESC;
```
**Esperado**: 1 row por step success com `cost_usd > 0`. Valor coerente
com a tabela acima (ex.: gpt-4o-mini com ~500 in + 200 out ≈ $0.0002).

5. Dashboard `/catalog/cost` agora mostra custos reais (não mais zeros).
   Filtrar por entry_id do recipe — totalizado por dia.

## 4 — Modelo desconhecido (defensivo)

Se agent estiver configurado com provider/model fora da tabela:
- Step ainda completa com sucesso
- `cost_usd=0` gravado em catalog_costs
- WARNING no log: `llm_pricing: modelo desconhecido 'foo/bar'`

## Critérios de aceitação Onda 4 / PR 3

- [x] 298 testes passam (281 + 17 novos)
- [x] `compute_cost(provider, model, in_tok, out_tok)` substitui placeholder no executor
- [x] Engine retorna tokens.input/output separados — executor passa ambos
- [x] step_results agora incluem tokens_input, tokens_output, provider, model
- [x] Modelo desconhecido → 0 + warning (não quebra fluxo)
- [x] Tabela cobre azure, openai, anthropic, maritaca, ollama
- [ ] Smoke manual com Postgres + LLM real (seção 3) — pendente em homolog

---

# Smoke Test — Onda 4 / PR 4 (Sandbox de invocação)

Sandbox para o owner/Root rodar o recipe ANTES de publicar, sem poluir
dashboards de chargeback. LLM real (testa qualidade/latência de verdade),
mas `record_invocation_cost` é skipado.

## 1 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 307 passed (298 anteriores + 9 novos):
- **TestSandboxEndpoint** (7): 404 inexistente, 403/404 não-owner, 422 não-recipe, 422 sem manifest, 202 owner em draft, 202 root em qualquer status, 202 owner em published
- **Executor**: sandbox=True NÃO chama record_invocation_cost mas step_results têm cost_usd; default=False continua gravando (regressão)

## 2 — Schema

```sql
\d catalog_recipe_executions
```

**Esperado**: coluna nova `is_sandbox BOOLEAN DEFAULT FALSE` (via ALTER TABLE
idempotente em `_IDEMPOTENT_MIGRATIONS`).

## 3 — Fluxo via UI

Pré: ter um recipe em **draft** com manifest declarado (sem publish).

1. Acessar `/catalog/{recipe_id}` como **owner** do recipe.
2. Tab "Execuções":
   - Banner azul: "Recipe em draft — use 🧪 Sandbox para testar antes de publicar"
   - Botão `🧪 Sandbox` (laranja) aparece
   - Botão `▶ Executar recipe` NÃO aparece (gated em published)
3. Clicar **🧪 Sandbox** → modal abre com:
   - Título "🧪 Sandbox de Recipe"
   - Banner amber explicando que custo não vai para chargeback
   - Mesmo textarea de input
   - Botão primário laranja "🧪 Rodar sandbox"
4. Submeter → modal de polling abre com badge **🧪 SANDBOX** ao lado do status.
5. Steps executam de verdade (LLM real); cost_usd aparece nos step_results
   (drill-down), mas:
6. **Validar no banco**:
```sql
-- Nada novo em catalog_costs nos últimos minutos:
SELECT count(*) FROM catalog_costs WHERE invoked_at > now() - interval '2 minutes';
-- Mas a execution está marcada:
SELECT id, status, is_sandbox FROM catalog_recipe_executions
WHERE started_at > now() - interval '2 minutes';
```
**Esperado**: zero rows novas em catalog_costs; execution com `is_sandbox=true`.

7. Histórico da tab "Execuções" mostra a linha com badge 🧪 ao lado do status.
8. Clicar **Ver** → modal de polling abre em read-only mostrando os steps
   com cost_usd > 0 mas total ainda não foi para o chargeback.

## 4 — Validações de auth

| Cenário | Esperado |
|---|---|
| Não-owner, não-root → POST /sandbox | 403 (ou 404 se nem vê a entry) |
| Owner em draft → POST /sandbox | 202 |
| Owner em published → POST /sandbox | 202 (também pode usar sandbox em prod) |
| Root em qualquer status, qualquer owner | 202 |
| Botão `▶ Executar` em draft (qualquer user) | continua oculto (só published) |

## 5 — Audit

```sql
SELECT action, entity_id, details FROM audit_log
WHERE action = 'recipe_sandbox_started'
ORDER BY created_at DESC LIMIT 5;
```

**Esperado**: 1 row por sandbox disparado, com `details.entry_status` no JSONB
(pra rastreabilidade do status no momento do teste).

## Critérios de aceitação Onda 4 / PR 4

- [x] 307 testes passam (298 + 9 novos)
- [x] Coluna `is_sandbox` adicionada idempotente
- [x] Endpoint `POST /entries/{id}/sandbox` (auth=owner|root, qualquer status)
- [x] Executor `is_sandbox=True` pula `record_invocation_cost`
- [x] UI mostra botão Sandbox + badge 🧪 em modal/polling/histórico
- [x] Audit `recipe_sandbox_started` registrado
- [x] Zero breaking changes em PRs Onda 1-3 e Onda 4 #67-#69
- [ ] Smoke manual no browser (seções 3-5) — pendente em homolog

---

# Smoke Test — Onda 4 / PR 5 (Anomalias de cost)

Detecção de picos/limites no consumo do dia. Reusa catalog_costs já
gravado pelo PR #69. Thresholds hardcoded; banner vermelho em /catalog/cost
+ audit cost_anomaly_detected.

## 1 — Testes unitários

```powershell
.venv\Scripts\python.exe -m pytest tests/ -v
```

**Esperado**: 322 passed (307 anteriores + 15 novos):
- **detect_anomalies puro** (8): pico detectado/ignorado por baseline floor/ratio threshold, limite global detectado/ignorado, ambos simultâneos, dia normal sem anomalias, shape do response inclui thresholds
- **Endpoint** (7): 200 root scope=all, 403 scope=all sem root, auto vira all/mine, filtro department, audit gravado quando há anomalia, audit NÃO gravado quando sem anomalia

## 2 — Thresholds em uso (hardcoded em app/catalog/anomalies.py)

| Constante | Valor | Significado |
|---|---|---|
| `PICO_MULTIPLIER` | 3.0 | Hoje ≥ 3× média 7d → pico |
| `PICO_MIN_BASELINE_USD` | 1.0 | Ignora pico se baseline < $1 (ratio é meaningless) |
| `LIMITE_GLOBAL_USD` | 100.0 | Hoje > $100 absoluto → limite_global |
| `BASELINE_WINDOW_DAYS` | 7 | Janela do baseline |

## 3 — Fluxo via UI

Pré: ter dados em `catalog_costs` (rodar alguns recipes em prod via PR #67-#69).

1. Acessar `/catalog/cost`.
2. Banner vermelho aparece **só se há anomalia** no scope atual:
   - "1 anomalia(s) detectada(s) no consumo de hoje"
   - Lista das mensagens humanizadas
   - Botão "Ocultar" some o banner (session-local)
3. Mudar scope (Root): banner refaz fetch (`loadAnomalies()` é chamado de dentro de `load()`).
4. Mudar filtro de department: idem.

Se não há anomalia hoje, banner não aparece (zero ruído visual).

## 4 — Disparar uma anomalia para teste manual

```sql
-- Injeta cost alto em hoje para gerar pico
INSERT INTO catalog_costs (id, entry_id, consumer_user_id, cost_usd, tokens_used, latency_ms, invoked_at)
SELECT gen_random_uuid()::text, id, 'u-test', 50.0, 1000, 100, now()
FROM catalog_entries WHERE kind='agent' LIMIT 1;
-- Repetir 3-4x → today_usd > $150
```

Recarregar `/catalog/cost` → banner aparece com pelo menos `limite_global`.

## 5 — Audit

```sql
SELECT created_at, actor, details
FROM audit_log
WHERE action = 'cost_anomaly_detected'
ORDER BY created_at DESC LIMIT 5;
```

**Esperado**: 1 row por chamada de GET /cost/anomalies quando count > 0.
Sem anomalia, nenhum row é gravado (evita spam).

## Critérios de aceitação Onda 4 / PR 5

- [x] 322 testes passam (307 + 15 novos)
- [x] Módulo `app/catalog/anomalies.py` com 4 thresholds + `detect_anomalies()`
- [x] Endpoint `GET /api/v1/catalog/cost/anomalies` (auto-scope)
- [x] Banner vermelho em `/catalog/cost` com botão Ocultar
- [x] Audit `cost_anomaly_detected` quando count > 0
- [x] Sandbox NÃO infla baseline/today (PR #70 não grava em catalog_costs)
- [x] Zero breaking changes em PRs Onda 1-3 e Onda 4 #67-#70
- [ ] Smoke manual no browser (seções 3-5) — pendente em homolog

---

# Smoke Test — Onda 4 / PR 6 (Fechamento / regressão)

PR puramente documental. Marca o sign-off da Onda 4 com 5 PRs entregues.

## 1 — Verificar docs novos/atualizados

```powershell
git diff origin/main --stat -- docs/catalog/
```

**Esperado**: ONDA4.md novo + README.md/REGRESSION.md/SMOKE_TEST.md atualizados.
Zero arquivos `.py` mudados.

## 2 — Executar regressão consolidada

Seguir o [REGRESSION.md](REGRESSION.md) Fase 8 (Onda 4):
- 8.1 — `pytest tests/` (322 passed)
- 8.2 — schema (7 tabelas catalog, com `is_sandbox` em recipe_executions)
- 8.3 — 32 endpoints REST registrados
- 8.4 — comportamentos novos (chain, sandbox isolation, anomalias)
- 8.5 — telas (tab Execuções, botão Sandbox, banner anomalias)
- 8.6 — 15 audit actions distintas no audit_log
- 8.7 — fluxo end-to-end (sandbox → publish → execute → cost → anomalia)

## Critérios de aceitação Onda 4 / PR 6 (#72)

- [x] ONDA4.md criado
- [x] README.md reflete Onda 4 entregue (sem 🚧) + roadmap Onda 5 enxuto
- [x] REGRESSION.md ganha Fase 8 com sign-off da Onda 4
- [x] SMOKE_TEST.md ganha esta seção
- [x] Zero código de produção tocado (chore puro)
- [ ] Regressão consolidada validada em homolog

**Onda 4 do Catálogo está pronta para sign-off e produção.**
