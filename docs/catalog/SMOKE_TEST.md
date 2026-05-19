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
