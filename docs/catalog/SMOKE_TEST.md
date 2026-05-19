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
