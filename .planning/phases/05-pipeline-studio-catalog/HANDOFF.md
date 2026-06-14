# HANDOFF — Estúdio de Pipelines (entregue) + próximos passos

> **Documento de passagem para uma NOVA sessão.** Auto-contido: traz o estado
> atual, o que foi entregue, como o sistema funciona HOJE (para NÃO re-descobrir),
> as convenções obrigatórias do projeto, e as próximas instruções (pendências +
> como executá-las). Leia a seção **6 (Convenções)** ANTES de codar.
>
> Companheiro deste arquivo: [`PLAN.md`](./PLAN.md) (o plano original, histórico).

---

## 0. Estado atual (2026-06-13)

- **`origin/main` em `APP_VERSION = "9.10.0"`** — fonte única em `app/core/version.py`.
- **TODOS os 12 PRs do Estúdio de Pipelines estão MERGEADOS (#361–#372).** **Não há PRs abertos.**
- O arco completo foi entregue de ponta a ponta. **Comece a próxima sessão fresco da main:**
  ```bash
  git checkout main && git pull origin main      # deve estar em 9.10.0 / commit 2d5b959+
  ```
  (A branch `feat/pipeline-trust-observability` é o PR7, já mergeado — pode ignorar/deletar.)
- App roda em Docker local: `127.0.0.1:7000`. Cookie root: `user_id=08768f10-a3ab-41db-8134-ba7c6768d1b1`.

---

## 1. O que foi entregue (12 PRs, v9.0.0 → 9.10.0)

### Parte A — Estúdio de Pipelines (pipeline vira entidade)
| PR | Versão | Entrega |
|----|--------|---------|
| #361 | 9.0.0 | Tabelas `pipelines`+`pipeline_agents`, `pipeline_lifecycle.py` (3 estados), `routes/pipelines.py` (CRUD+/status 422+/agents exclusivo), migração `mesh_groups→pipelines`, frontend `mesh_flow.html` (painel/lente/filtros/pill/hand-offs) |
| #362 | 9.1.0 | Status gateia o runtime: **só `aposentado` bloqueia, só na ENTRADA**, fail-open |
| #363 | 9.2.0 | `_detect_roots` fonte única + `/topology` enriquecido (`roots` + `pipeline_id` por nó); `mesh.html`/`workspace.html` consomem |

### Parte B — Catálogo pipeline-native + UI + trust
| PR | Versão | Entrega |
|----|--------|---------|
| #364 | 9.3.0 | `kind='pipeline'` no catálogo (CHECK estendido via DROP+ADD CONSTRAINT); `POST /catalog/entries/from-pipeline` cria draft; botão "Publicar no Catálogo" no estúdio |
| #365 | 9.4.0 | Pipeline = GRAFO: tabela `catalog_pipeline_defs` (snapshot na publicação) + execução via `execute_pipeline` (rotas `/execute-pipeline`, `/sandbox-pipeline`, `/pipeline-def`) |
| #371 | 9.9.0 | `catalog_detail.html` pipeline-aware: aba **Fluxograma** read-only (SVG do snapshot) + Executar/Sandbox kind-aware + aba Execuções |
| #372 | 9.10.0 | **Trust real**: cost auto-wire por step (engine) + `recompute_entry_trust` popula `reliability`/`latency_p95`/`avg_cost` de execuções reais |

### Trilha A — pipeline = unidade SELADA de execução/invocação
| PR | Versão | Entrega |
|----|--------|---------|
| #366 | 9.5.0 | Execução **delimitada**: `execute_pipeline(..., allowed_agent_ids=None)` — BFS só anda dentro do conjunto (opt-in; None=global) |
| #367 | 9.6.0 | `POST /api/v1/pipelines/{id}/invoke` — invoke pela ENTIDADE, selado pela membership; `aposentado`→409 |

### Trilha B — Fluxograma é o editor ÚNICO; Topologia aposentada
| PR | Versão | Entrega |
|----|--------|---------|
| #368 | 9.7.0 | Portou pro Fluxograma: aviso **fan-out × cadeia** + preview de **system_prompt** |
| #369 | 9.8.0 | Aposentou a página Topologia: `GET /mesh` → **308** → `/mesh/flow`; nav/links migrados |
| #370 | 9.8.1 | **Deletou** `mesh.html` + entry `PAGES['/mesh']`; podou testes UI-only mantendo os de API |

---

## 2. Como o sistema funciona HOJE (fatos do código — confiar, não re-descobrir)

### 2.1 O grafo executável (substrato)
- **`mesh_connections`** (tabela, `app/core/database.py`) é a **fonte única do grafo executável**: `id, source_agent_id, target_agent_id, connection_type (sequential|parallel|conditional|default), config (TEXT JSON: {expr?, context_scope?})`. **Não foi removida** e não deve ser.
- Endpoints `/api/v1/mesh/*` em `app/routes/mesh.py` (ATIVOS, usados por Fluxograma/Workspace/engine): `/topology`, CRUD `/connections`, `/layout`, `/groups`, `/fsm/{id}`, `/last-run`, `/conditional-vars`, `/connection-types`, `/context-scope-vars`, `/connections/test-conditional`, `/connections/test-context-scope`.
- **`GET /api/v1/mesh/topology`** retorna `{nodes[], edges[], fanout_roots[], roots[]}` + cada node tem `pipeline_id`. Helpers: `_detect_roots(edges)` (source-never-target, fallback ciclo-puro — **fonte única** da detecção de raiz) e `_fanout_roots(edges)` (≥2 saídas conditional).

### 2.2 Pipeline como entidade
- **`pipelines`** (tabela): `id, name, status CHECK(rascunho|publicado|aposentado), domain, color, description, timestamps`.
- **`pipeline_agents`** (tabela): `agent_id` **PK** (membership EXCLUSIVA: 1 agente → 1 pipeline), `pipeline_id` FK CASCADE.
- **`pipeline_membership`** (singleton em `database.py`, espelha `settings_store`): `set/remove/remove_from/agents_of/pipeline_of/all`. **O `Repository` genérico assume PK=`id`** — por isso a membership é este singleton, não um Repository.
- **`pipeline_lifecycle.py`** (`app/agents/`): `PIPELINE_STATES`, `PIPELINE_TRANSITIONS`, `can_transition_pipeline`, `next_pipeline_states`, `is_terminal_pipeline` (espelha `catalog/lifecycle.py`; funções puras retornam bool, o caller levanta HTTPException — pipelines usam **422**; o catálogo usa **409**).
- **`routes/pipelines.py`** (`/api/v1/pipelines`): `GET` (lista + agent_ids/agent_count + filtros), `POST`, `GET/{id}`, `PUT/{id}` (não muda status), `DELETE/{id}`, `POST/{id}/status` (422 governado), `POST|DELETE /{id}/agents`, **`POST /{id}/invoke`** (selado).

### 2.3 Execução (engine) — o coração
`app/agents/engine.py::execute_pipeline(entry_agent_id, user_input, channel, attachments, progress_callback, session_id, context_mode, allowed_agent_ids=None)`:
1. Valida o entry agent (404 → ValueError).
2. **Gate de status** (PR2): se o pipeline do entry é `aposentado` → `ValueError` (não roteável). Só `aposentado`, só na ENTRADA, **fail-open** (erro de lookup não bloqueia).
3. **`_resolve_ordered_chain_with_parents(entry, allowed_agent_ids)`** — BFS downstream sobre `mesh_connections`. Quando `allowed_agent_ids` é fornecido, **SELA** ao subgrafo (só anda para targets no conjunto); `None` = BFS global (histórico).
4. Loop por step: pula inativos (`status!='active'`), passthrough, conditional (skip por expr), executa via `execute_interaction`.
5. **Cost auto-wire por step** (PR7): `_step_cost_and_tokens(result, agent)` lê **`result['trace']['tokens']`** (`input/output/total`) e o provider/model EFETIVO em **`result['trace']['agent_provider'|'agent_model']`** → `compute_cost`. ⚠️ **ARMADILHA: tokens/provider/model NÃO são chaves top-level do result — vivem em `trace`** (o mesmo desempacotamento do `_invoke_step` do executor de recipe).
6. Retorna `{output, pipeline_steps[], total_agents, completed_agents, passthrough_agents, duration_ms, interaction_id, final_state, status, trace{...}}`.

**Chamadores de `execute_pipeline`:** `routes/agents.py` (`/invoke`, só para agentes externamente-invocáveis), `routes/workspace.py` (chat stream+sync), `routes/pipelines.py` (`/invoke`, selado por membership), `catalog/executor.py::execute_pipeline_entry` (selado pelo snapshot).

### 2.4 Catálogo pipeline-native
- **`kind='pipeline'`** permitido em `catalog_entries` (CHECK estendido; `urn.VALID_KINDS`, `models.EntryKind`/`ArtifactType`, `require_artifact_link`). Migração idempotente em `_IDEMPOTENT_MIGRATIONS` (DROP+ADD CONSTRAINT; nomes auto `catalog_entries_kind_check`/`_artifact_type_check`).
- **`catalog_pipeline_defs`** (tabela): `entry_id` PK FK CASCADE, `root_agent_id`, `nodes` JSONB, `edges` JSONB, timestamps. É o **snapshot** do subgrafo no momento da publicação.
- **`app/catalog/pipeline_defs.py`**: `_build_subgraph(pipeline_id)` (membros + arestas intra-pipeline + raiz via `_detect_roots`), `snapshot_pipeline_def(entry)` (upsert no publish), `get_pipeline_def(entry_id)`, `resolve_pipeline_root(entry)`, `resolve_pipeline_exec(entry)→(root, membros)`.
- **`catalog/executor.py::execute_pipeline_entry`**: roda via `execute_pipeline(root, allowed_agent_ids=membros)`, mapeia `pipeline_steps`→`steps_results`, grava em **`catalog_recipe_executions`** (tabela reusada; `recipe_entry_id` guarda o id da entry do pipeline), `finalize_execution`, `record_invocation_cost`, `recompute_entry_trust`. Gravação guardada (finalize-as-failed; nunca deixa 'running' forever).
- **`catalog/queries.py`**: `_compute_trust(rows)` (puro), `recompute_entry_trust(entry_id)` (reliability=completed/finished, latency_p95, avg_cost — de execuções não-sandbox), `record_invocation_cost` (insere `catalog_costs` + bumpa `trust_invocation_count`/`trust_last_invoked_at`).
- **Rotas catálogo** (`routes/catalog.py`): `POST /entries/from-pipeline` (cria draft kind=pipeline), `POST /entries/{id}/execute-pipeline`, `POST /entries/{id}/sandbox-pipeline`, `GET /entries/{id}/pipeline-def`. Lifecycle existente: `/submit`→`/submissions/{id}/decide` (root)→`/publish`→`/deprecate` (no `catalog_detail.html`). Publish de kind=pipeline dispara o snapshot.

### 2.5 Contrato de uso por API (invoke externo)
- **Invoke por pipeline (recomendado)**: `POST /api/v1/pipelines/{id}/invoke` `{message|input, session_id?, channel?}` → roda selado ao subgrafo; `aposentado`→409. Descoberta: `GET /api/v1/pipelines?status=publicado`.
- **Invoke por agente-raiz (legado/direto)**: `POST /api/v1/agents/{agent_id}/invoke` — só para "callable orchestrators" (`_is_callable_externally`: aobd/router com outgoing, ou subagent standalone). Descoberta: `GET /api/v1/agents/callable`.
- **Executar pipeline publicado pelo Catálogo**: `POST /api/v1/catalog/entries/{id}/execute-pipeline` (selado ao snapshot) → poll `GET /api/v1/catalog/executions/{id}`.
- Nenhuma dessas rotas exige `require_user` explícito (espelha o `/invoke` existente; auth por middleware/cookie/X-API-Key).

### 2.6 Frontend
- **`mesh_flow.html`** (`/mesh/flow`) — o **editor único** do mesh (Fluxograma). Painel lateral de pipelines, lente (`viewNodes`/`viewEdges`), filtros+paleta, pill de status, publicar no catálogo, replay, minimapa, aviso fan-out (badge), preview de system_prompt no painel de detalhe.
- **`workspace.html`** — `loadPipelineRoots` consome `topology.roots`.
- **`catalog_detail.html`** — aba "Fluxograma" read-only (getters `pipelineLayout`/`pipelineSvg` renderizam o `/pipeline-def` em SVG via `<g x-html>`), Executar/Sandbox kind-aware, aba Execuções, run modal kind-aware. `catalog.html` mostra o badge `pipeline` (emerald).
- **`mesh.html` (Topologia) foi DELETADO.** `GET /mesh` → 308 → `/mesh/flow`. Nav AI Mesh → `/mesh/flow`.

---

## 3. Pendências / próximos passos possíveis (nada bloqueante)

> O produto está **funcionalmente completo** para uso via `invoke`. Estes são
> aprimoramentos opcionais. Faça **1 PR coeso por item** seguindo a seção 6.

### 3.1 Cost-wire + trust para RECIPES (rápido, alto valor)
Os helpers `_step_cost_and_tokens` (engine) e `recompute_entry_trust` (queries) são **genéricos**, mas hoje só o caminho de PIPELINE os usa. Recipes ainda gravam custo via `_invoke_step` mas **não chamam `recompute_entry_trust`** → `trust_reliability/latency_p95/avg_cost` de recipes seguem 0.
- **Instrução**: em `catalog/executor.py::execute_recipe`, após `finalize_execution`, chamar `await recompute_entry_trust(recipe_entry_id)` (só `not is_sandbox`, guardado em try/except). Teste: mesma estrutura de `tests/test_pipeline_trust.py`.

### 3.2 Pricing de modelos locais
`compute_cost` (`app/core/llm_pricing.py`) retorna **0** para modelos fora da tabela de preços (ex.: gpt-oss/modelo local). Por isso `avg_cost` aparece 0 no ambiente local (em produção com gpt-4o = real, ex.: 1000/500 tok = $0.0075).
- **Instrução** (se quiser custo local): adicionar o modelo local à tabela de pricing em `app/core/llm_pricing.py`. (Opcional — não é bug.)

### 3.3 PR8 — Federação / disclosure (do PLAN.md §5.3, marcado como opcional/futuro)
Alinhar pipelines aos recursos avançados do Catálogo: capability disclosure por pipeline, OPA tiered, pricing editável, A2A bidirecional, capability fingerprint. **Só se o usuário quiser ir tão fundo.** Detalhar com o usuário antes.

### 3.4 Backlog de plataforma (NÃO-pipeline, herdado de sessões anteriores)
Ver memória [[project-session-handoff]]: MCP per-conector; Tier 2 text-to-SQL governado; depreciar `operation/query` no MCP per-tool. Independentes do Estúdio de Pipelines.

---

## 4. Comandos úteis (ambiente local)

```bash
# Subir/atualizar (código é BAKED na imagem → SEMPRE rebuild a cada mudança, inclusive front)
docker compose build app && docker compose up -d app          # app em 127.0.0.1:7000

# Testes (rodar antes E depois de cada mudança)
python -m pytest tests/ -q                                    # suíte: ~3292 passed, 6 skipped
python -m pytest tests/test_pipeline_*.py -q                  # testes do estúdio

# Validar JS de template (lição do projeto: x-html SVG precisa de namespace)
#   extrair o maior <script> e: node --check arquivo.js

# Smoke E2E rápido (cookie root)
COOKIE="user_id=08768f10-a3ab-41db-8134-ba7c6768d1b1"
curl -s -b "$COOKIE" -d '{"name":"X"}' -H 'Content-Type: application/json' http://127.0.0.1:7000/api/v1/pipelines     # criar
curl -s -b "$COOKIE" -d '{"message":"oi"}' -H 'Content-Type: application/json' http://127.0.0.1:7000/api/v1/pipelines/{id}/invoke  # invoke selado

# DB (Postgres agente_inteligencia, container agente_postgres, user agente)
docker exec agente_postgres psql -U agente -d agente_inteligencia -c "SELECT id,name,status FROM pipelines;"
```

---

## 5. Verificação esperada (baseline ao começar)
- `git log origin/main --oneline -1` → `...PR7, 9.10.0... (#372)`.
- `python -m pytest tests/ -q` → **3292 passed, 6 skipped** (os 6 skips = integração que pede Postgres em localhost:5432; normal).
- `curl /mesh` → **308** → `/mesh/flow`. `/mesh/flow` → 200. Nav sem "Topologia de conexões".
- Publicar um pipeline → executar → a entry ganha `trust_reliability`/`latency_p95` reais.

---

## 6. Convenções OBRIGATÓRIAS do projeto (ler antes de codar)

- **Ambiente Docker local**: código é **baked na imagem** → a CADA mudança (inclusive só frontend/template): `docker compose build app && docker compose up -d app`. Imagem stale serve versão antiga (sintoma clássico de "minha mudança sumiu").
- **Testes obrigatórios** por mudança (unit + integração quando aplicável). Rodar a suíte ANTES e DEPOIS. Funções puras (lifecycle, `_compute_trust`, `_detect_roots`) → testáveis sem DB; rotas → `TestClient` + monkeypatch dos repos/singletons (objetos compartilhados → patch do método); engine → `asyncio.run` + monkeypatch de `_resolve_*`/`execute_interaction`. LLM em teste local = `OPENAI_API_KEY` do `.env`.
- **Bump de versão por PR** em `app/core/version.py` (`APP_VERSION`): nova func→MAJOR, melhoria→MEDIUM, fix/limpeza→MINOR (SemVer com reset). Rodapé da UI lê `v{{ app_version }}`. **Informar a versão ao usuário ao fim de cada tarefa.**
- **Fluxo de PR — `feat/*` é MANUAL**: branch fresca da `main`; `gh pr create --base main`; depois `gh pr merge <n> --squash --delete-branch`; **sair fresco da main após cada merge** (armadilha do squash-merge: a branch antiga não é ancestral do squash → empilhar nela dá conflito). `fix/*` dispara auto-PR/auto-merge — evitar para feature work.
- **Paleta**: PROIBIDO roxo/violet/fuchsia/purple (`test_platform_red_palette` guarda). Usar vermelho/âmbar/teal/emerald/brand. (kind=pipeline no catálogo usa **emerald**.)
- **JSONB + asyncpg**: `json.dumps()` antes de gravar dict/list; o `Repository` genérico NÃO serializa. Em colunas JSONB use `$n::jsonb` com a string.
- **Logs**: ler `LOG_DIR` (default `logs/`) antes de teorizar bug; instrumentar `event=` + `exc_info`.
- **Adversarial review**: rodar revisão (workflow ou agente) no diff antes de abrir o PR; verificar cada achado contra o código real. (Pegou bugs reais nesta fase: guard de gravação no PR5, edge.config no PR5, cost-wire de chaves erradas no PR7.)

---

## 7. Armadilhas consolidadas (gotchas)
1. **`mesh_connections` (tabela) ≠ página Topologia.** A página foi removida; a tabela + `/api/v1/mesh/*` são a fonte do grafo — **não remover**.
2. **Pipeline só é fronteira de execução quando `allowed_agent_ids` é passado.** `execute_pipeline` sem ele faz BFS GLOBAL (comportamento histórico do `/invoke` de agente). O `/pipelines/{id}/invoke` e o `/execute-pipeline` do catálogo passam os membros (selado).
3. **Custo/tokens vêm de `result['trace']['tokens']`** (`input/output/total`) e provider/model de `trace['agent_provider'|'agent_model']` — **NÃO** são chaves top-level do result (bug clássico). Reusar `_step_cost_and_tokens`.
4. **`compute_cost`=0 para modelos não-precificados** → `avg_cost` 0 no local; não é bug de wiring.
5. **Gate de status**: só `aposentado` bloqueia, só na entrada, fail-open. `rascunho`/`publicado` rodam (a migração do PR1 transformou grupos em `rascunho` — bloquear rascunho quebraria fluxos migrados).
6. **`catalog_recipe_executions` é reusada para runs de pipeline** (`recipe_entry_id` = id da entry do pipeline; FK válida). `recompute_entry_trust` é genérico.
7. **Paleta proíbe roxo** (guard de teste) — sempre validar antes do PR.
8. **Rebuild docker a cada mudança de frontend** (template baked).

---

*Gerado ao fim da sessão de 2026-06-13. Memória viva relacionada:
`memory/project_pipeline_studio_plan.md`, `memory/MEMORY.md`.*
