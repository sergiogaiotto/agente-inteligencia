# Backlog — evoluções do Playground (console de API)

> Ponto de partida para uma **sessão futura**. O Playground (submenu de AI Mesh)
> já entregou: builder + "Executar como integração" (X-API-Key, fidelidade) +
> Resposta (streaming ao vivo + cartões) + Tempo (profiler) + Trace (FSM/SQL, só
> Debug) + HTTP (status/headers/rate-limit/mapa de erros) + Código (curl/Python/JS)
> + Histórico/REPL (localStorage). Este doc detalha as 3 evoluções pendentes.

## Onde tudo vive (mapa do código)
- **Página:** `app/templates/pages/mesh_playground.html` → componente Alpine `playgroundPage()`.
  - Estado: `pipelines, selectedId, message, verbosity, tab, lang, result, error, sizeKb, timings, _t0, expanded, http, errTests, history, ERRORS`.
  - Métodos: `run()` (fetch SSE p/ `/invoke/stream`, `credentials:'omit'`+`X-API-Key`), `_ev()` (parser SSE), `runOutputCards()`, `stepMeta()`, `liveIcon()`, getters `fullSteps/totalMs/totalCost/totalTokens/waterfall/traceItems`, `fmtMs()`, `snippet()` + `_pyDict()`, `copyCode()`, `testError(code)`, `_pushHistory/_saveHistory/_loadHistory/clearHistory/restore` (localStorage key `pg_history`, últimas 20).
- **Rotas:** `app/routes/pipelines.py` → `invoke_pipeline` (sync, projeção de verbosidade) e `invoke_pipeline_stream` (SSE, projeta `pipeline_done` por verbosidade). Auth = `require_user` (cookie OU `X-API-Key`; chave seta `request.state.api_key_id`).
- **Projeção de verbosidade:** `app/agents/result_view.py` (`resolve_verbosity`, `project_pipeline_result`).
- **Registro de página/nav:** `app/routes/frontend.py` (`PAGES["/mesh/playground"]` + `pg_mesh_playground`); `app/templates/layouts/base.html` (link no submenu AI Mesh + mapa de seção). ⚠️ **Bloco `tour-nav-mesh` deve ficar balanceado** (teste `test_bloco_ai_mesh_fecha_certo` trava isso — contagem TOTAL mascara mis-nesting).
- **Engine emite eventos** via `progress_callback`: `pipeline_start / agent_start / agent_done / agent_skipped / agent_error / pipeline_done(result)`. `agent_start` carrega `processing_message`.

## Convenções da casa (seguir SEMPRE)
- Frontend é BAKED na imagem → a cada mudança: `docker compose build app && docker compose up -d app` (mesmo só template). Verificar página renderiza (autenticado via cookie `user_id` do 1º user) sem erro de Jinja.
- Testes obrigatórios (pytest). Padrão: varredura de template (`tests/test_playground_page.py`) + rota via `TestClient` com `dependency_overrides[require_user]`.
- Bump `app/core/version.py` por PR (MAJOR=nova func / MEDIUM=melhoria / MINOR=fix).
- 1 PR por feature, `--base main`. **Mergear SEMPRE com** `gh pr merge <n> --squash --delete-branch --match-head-commit $(git rev-parse HEAD)` (a flag mata o squash-drop que já dropou commits 3× — sem ela, verificar a main com symbol-grep pós-merge é obrigatório).
- Checar balanço de `<div>` (total E por bloco) em qualquer mudança de `base.html`.

---

## Feature 1 — Persistir histórico no servidor
**Hoje:** histórico só em `localStorage` (`pg_history`), por-navegador. **Meta:** por-usuário, sobrevive a troca de máquina, auditável.

**Decisão de design (confirmar):** tabela dedicada `playground_runs` (recomendado — desacopla do interno de execução, guarda exatamente o card) **vs** reusar `interactions`/`audit` (o invoke já é auditado em `pipelines.py` via `audit_repo.create(action='invoked')`, mas sem `message`/`output`/`size`). Recomendo tabela dedicada.

**Passos:**
1. **Migração** em `app/core/database.py` (padrão `CREATE TABLE IF NOT EXISTS`): `playground_runs(id TEXT PK, user_id TEXT, pipeline_id TEXT, pipeline_name TEXT, message TEXT, verbosity TEXT, status TEXT, size_bytes INT, duration_ms INT, created_at TIMESTAMP DEFAULT now())`. Criar `playground_runs_repo = Repository("playground_runs")`.
2. **Schema** (`app/models/schemas.py`): `PlaygroundRunCreate(pipeline_id, pipeline_name, message, verbosity, status, size_bytes, duration_ms)`.
3. **Rota** nova `app/routes/playground.py` (registrar o router em `main.py`):
   - `POST /api/v1/playground/runs` (`require_user`) → grava com `user_id=user['id']`. ⚠️ datetime: usar `datetime.utcnow()` (naive) — coluna TIMESTAMP.
   - `GET /api/v1/playground/runs?limit=20` → últimos do user (ordem desc).
   - `DELETE /api/v1/playground/runs` (limpar tudo do user) + `DELETE /api/v1/playground/runs/{id}`.
4. **Frontend** (`mesh_playground.html`): trocar `_pushHistory` → `POST` (otimista: empurra local + persiste); `_loadHistory` (no `init()`) → `GET`; `clearHistory` → `DELETE`. Manter `localStorage` como cache offline opcional.
5. **Testes:** rota CRUD (`TestClient` + mock `playground_runs_repo`/`require_user`); varredura do template (chama `/playground/runs`).
6. **Bump:** MEDIUM (melhoria). **Ressalvas:** privacidade (mensagens podem ser sensíveis — é per-user, ok); `Repository.create` com JSONB não se aplica aqui (tudo escalar).

---

## Feature 2 — Comparar 2 pipelines lado a lado
**Meta:** mesma entrada → 2 pipelines (ou 2 versões/2 verbosidades) → respostas lado a lado. A/B / regressão. **Sem backend novo** (reusa `/invoke/stream`).

**Passos:**
1. **Refatorar `run()`** em `runOne(pipelineId, slot)` que escreve em `slot` ('A'/'B'): `resultA/resultB`, `timingsA/timingsB`, `httpA/httpB`. O `_ev()` recebe o slot.
2. **Estado:** `compareMode` (toggle), `pipelineB` (2º destino). No modo compare, "Executar" dispara `Promise.all([runOne(selectedId,'A'), runOne(pipelineB,'B')])` (paralelo).
3. **Markup:** painel direito vira 2 colunas (A | B), cada uma com Resposta (+ opcional Tempo/Trace). Um **resumo de deltas** no topo: "B: −2,1 s · −30% custo · mesma resposta? (diff)". Calcular delta de `totalMs`, `totalCost` (se Debug), `sizeKb`, e um diff simples do `output` (igual/diferente, ou highlight).
4. **Variante barata:** comparar o MESMO pipeline em 2 verbosidades (1 execução full + 2 projeções client-side) — sem 2× LLM. Oferecer como modo "comparar verbosidade".
5. **Testes:** varredura (compareMode, runOne, 2 colunas, deltas). **Ressalva:** 2 execuções = 2× custo LLM (avisar na UI). Reaproveitar `outCards/stepMeta/waterfall` por slot.
6. **Bump:** MAJOR (nova funcionalidade).

---

## Feature 3 — Codegen para mais SDKs
**Hoje:** `snippet()` gera curl / Python (`requests`) / JS (`fetch`). **Meta:** mais linguagens + variante **streaming** (consumir o SSE do `/invoke/stream`).

**Passos:**
1. **Expandir `LANGS`** e `snippet()` (`mesh_playground.html`): adicionar `python-httpx`, `node-axios`, `go` (`net/http`), `php` (`curl`), `ruby` (`net/http`), `csharp` (`HttpClient`), `java` (`HttpClient`). Cada um: POST + header `X-API-Key` + body JSON com `verbosity`.
2. **Refator (recomendado):** extrair um "spec" `{method,url,headers,body}` e formatadores por linguagem (evita duplicar a request em N strings).
3. **Variante streaming** (toggle "sync | streaming"): mostrar como consumir o `/invoke/stream` (SSE) — `curl -N`, Python `httpx.stream`/`sseclient`, JS `fetch`+`ReadableStream`/`EventSource`. Alto valor: o endpoint de streaming é nosso diferencial.
4. **Testes:** varredura (cada linguagem presente + o toggle streaming).
5. **Bump:** MEDIUM (melhoria). Pura frontend, zero backend.

---

## Ordem sugerida & versão
Main atual: **18.3.1**. Sugiro: **(3) SDKs** (frontend-only, rápido, MEDIUM → 18.4.0) → **(1) histórico no servidor** (backend, MEDIUM → 18.5.0) → **(2) comparar** (maior, MAJOR → 19.0.0). Cada uma é 1 PR independente `--base main`. Bumps cascateiam conforme a regra (MAJOR zera MEDIUM/MINOR).
