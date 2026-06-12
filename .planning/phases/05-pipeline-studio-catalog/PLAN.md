# PLAN — Estúdio de Pipelines (Fluxograma) + Catálogo pipeline‑native

> **Handoff para nova sessão.** Este documento é auto‑contido: traz contexto, a
> fundamentação do código (para NÃO re‑descobrir), as decisões já travadas com o
> usuário, o contrato de dados, e o faseamento (PRs) com critérios de aceitação.
> Leia a seção 6 (Convenções) ANTES de codar — ela tem as regras do projeto
> (testes, bump de versão, docker, fluxo de PR).

---

## 0. Estado no início deste plano (2026‑06‑12)

- `main` em **8.6.0** (após o #360 mergear: Fluxograma view+edição+FSM+replay+
  polimento+UX de conexão+fix de namespace SVG das arestas+chevron do AI Mesh).
- A página do Fluxograma é `app/templates/pages/mesh_flow.html` (Alpine + SVG via
  `x-html`). A Topologia é `app/templates/pages/mesh.html`.
- Endpoints do mesh em `app/routes/mesh.py`: `/topology`, CRUD `/connections`,
  `/layout`, `/fsm/{id}`, `/last-run`, `/groups`.

## 1. Objetivo

Transformar o Fluxograma de "uma view do mesh inteiro" num **estúdio de
pipelines**: o usuário cria/renomeia pipelines, organiza agentes por pipeline,
filtra agentes (domínio/tipo/nome) para incluí‑los, e gerencia o **ciclo de vida**
(rascunho → publicado → aposentado). Em seguida, **atualizar o Catálogo em
profundidade** para que pipelines sejam cidadãos de 1ª classe da loja (publicar,
descobrir, executar, governar).

## 2. Fundamentação (fatos do código — confiar, não re‑descobrir)

**Pipeline hoje é EMERGENTE, não existe entidade.** Um "pipeline" = uma RAIZ
(agente que é `source` e nunca `target`) + sua cadeia BFS downstream em
`mesh_connections`. A detecção de raiz está **triplicada** (engine
`_resolve_ordered_chain_with_parents` em `app/agents/engine.py:4647`; mesh.html
`hierarchicalEdges` :819; workspace.html `loadPipelineRoots` :1617).

- **`mesh_connections`** (tabela, `app/core/database.py:97-104`): `id`,
  `source_agent_id`, `target_agent_id`, `connection_type DEFAULT 'sequential'`
  (∈ sequential|parallel|conditional|default), `config TEXT DEFAULT '{}'`
  (JSON: `{expr?, context_scope?}`), `created_at`. **Fonte única do grafo
  executável.**
- **Metadados UI‑only em `platform_settings`** (key/value, `settings_store`):
  - `mesh_groups` = `[{id, name, color, agent_ids[]}]` — grupos com **membership
    exclusiva** (1 agente → 1 grupo; `assignToGroup` remove dos outros antes).
  - `mesh_chain_names` = `{rootId: "nome do pipeline"}` — frágil (órfão se a raiz
    muda).
  - `mesh_node_positions` = `{agent_id: {x,y}}` — posições do Fluxograma.
- **`agents`** (`app/core/database.py:65-85`): `id, name, description, kind
  (CHECK aobd|router|subagent), domain (TEXTO LIVRE, nullable), skill_id,
  llm_provider, model, status (DEFAULT 'active'; na prática active|inactive),
  version, ...`. `GET /api/v1/agents?kind=&status=&domain=&limit=&offset=` filtra
  por igualdade exata, **sem busca por nome**. O Fluxograma NÃO usa esse endpoint
  — usa `GET /api/v1/mesh/topology` (nodes já têm `kind`, `domain`, `name`), então
  **filtros (domínio/tipo/nome‑substring) são CLIENT‑SIDE** sobre `this.nodes`.
- **Mapa kind→rótulo (já em `mesh_flow.html:346-355`)**: `aobd`→**Maestro** (#DB5A47),
  `router`→**Triagem** (#C2620E), `subagent`→**Especialista** (#1D9E75), `start`→Início.
- **Runtime (`execute_pipeline`, `app/agents/engine.py:3003`)**: recebe
  `entry_agent_id` (a raiz). Gates: (1) por‑agente `status=='active'` (inativo →
  skipped); (2) por‑aresta `conditional` + expr Jinja false → passthrough.
  `connection_type` NÃO decide quais agentes entram na chain (BFS pega todos).
  **NÃO existe rascunho/publicado nem gate de publicação.**
- **CICLO DE VIDA já existe no Catálogo** (`app/catalog/lifecycle.py:16-62`):
  state machine `draft→submitted→approved→published→deprecated→archived`,
  funções `can_transition_entry(from,to)`, `next_entry_states`, `is_terminal_entry`.
  Persistido em `catalog_entries.status` (CHECK das 6). **Reusar o PADRÃO.**
- **RECIPE = pipeline LINEAR no Catálogo** (`catalog_entries.kind='recipe'` +
  `catalog_recipes.steps[]` = `[{order, target_entry_id, notes?}]`, 1..50 passos;
  execução em cadeia `catalog_recipe_executions`, `app/catalog/executor.py:58`,
  só roda target `status='published'` e `kind='agent'`). **Recipe é LINEAR** — não
  representa o grafo rico (condicional/paralelo/fan‑out) do mesh. Por isso o
  pipeline NÃO é simplesmente um recipe (ver Parte B).

## 3. Decisões travadas (confirmadas com o usuário)

1. **Membership exclusiva**: 1 agente → no máximo 1 pipeline (igual aos grupos).
2. **Lifecycle de 3 estados simples** para pipeline: `rascunho | publicado |
   aposentado`. Reusar o *padrão* de `app/catalog/lifecycle.py` (máquina de
   estados governada + coluna `status` com CHECK), NÃO os 6 estados do catálogo.
   Transições: `rascunho→publicado`, `publicado→rascunho` (despublicar),
   `publicado→aposentado`, `aposentado→publicado` (reativar). `rascunho→aposentado`
   permitido (descartar).
3. **Runtime intacto no PR1**: status é metadado de organização/governança; o
   **gating de execução por status entra no PR2**.
4. **+ Atualizar o Catálogo em profundidade** (Parte B) — **CONFIRMADO**:
   pipeline publicado vira um **novo kind `pipeline` (GRAFO)** em
   `catalog_entries` (não recipe linear), e a atualização é **pipeline‑native
   completo** (PR4 publicar→catálogo, PR5 modelo de grafo, PR6 UI do catálogo
   pipeline‑aware, PR7 trust/custo/observabilidade). Federação/disclosure (PR8)
   fica opcional/futuro.

---

## 4. PARTE A — Estúdio de Pipelines (Fluxograma)

### 4.1 Modelo de dados

```
TABELA pipelines (nova, Postgres):
  id            TEXT PK
  name          TEXT NOT NULL
  status        TEXT NOT NULL DEFAULT 'rascunho'
                CHECK (status IN ('rascunho','publicado','aposentado'))
  domain        TEXT            -- texto livre (igual agents.domain)
  color         TEXT DEFAULT 'teal'   -- mesma paleta de mesh_groups
  description   TEXT
  created_at    TIMESTAMP DEFAULT now()
  updated_at    TIMESTAMP DEFAULT now()

TABELA pipeline_agents (membership exclusiva):
  pipeline_id   TEXT REFERENCES pipelines(id) ON DELETE CASCADE
  agent_id      TEXT NOT NULL
  PRIMARY KEY (agent_id)        -- exclusividade: 1 agente → 1 pipeline
  -- índice por pipeline_id para listar membros
```

> Decisão de implementação: `pipeline_agents` com PK em `agent_id` garante
> exclusividade no banco. (Alternativa rejeitada: `agent_ids` JSON na pipeline —
> não garante exclusividade nem permite query reversa eficiente.)

**Membership x conexões.** As conexões continuam SÓ em `mesh_connections`. Uma
conexão "pertence" a um pipeline P quando **ambas as pontas são membros de P**.
Conexões com pontas em pipelines diferentes (ou sem pipeline) só aparecem no
"Mesh completo".

**Máquina de estados** (`app/agents/pipeline_lifecycle.py`, NOVO, espelha o
padrão de `catalog/lifecycle.py`):
```
PIPELINE_STATES = {rascunho, publicado, aposentado}
PIPELINE_TRANSITIONS = {
  rascunho:   {publicado, aposentado},
  publicado:  {rascunho, aposentado},
  aposentado: {publicado},
}
can_transition_pipeline(from, to) -> bool
```

### 4.2 Endpoints (`app/routes/pipelines.py`, NOVO; prefix `/api/v1/pipelines`)

```
GET    /api/v1/pipelines                 -> {pipelines:[{...,agent_count}]}  (filtros opc: ?status=&domain=)
POST   /api/v1/pipelines                 {name, domain?, color?} -> cria (status='rascunho')
GET    /api/v1/pipelines/{id}            -> {pipeline, agent_ids:[...]}
PUT    /api/v1/pipelines/{id}            {name?, domain?, color?, description?}  (NÃO muda status)
DELETE /api/v1/pipelines/{id}            -> remove pipeline + membership (conexões intactas)
POST   /api/v1/pipelines/{id}/status     {status} -> transição GOVERNADA (can_transition_pipeline; 422 se inválida)
POST   /api/v1/pipelines/{id}/agents     {agent_id} -> inclui agente (move de outro pipeline se preciso)
DELETE /api/v1/pipelines/{id}/agents/{agent_id} -> remove agente do pipeline
```
- Reusar `Repository` de `app/core/database.py` (criar `pipelines_repo`,
  `pipeline_agents_repo`). Status só muda via `/status` (nunca PUT direto) —
  padrão do catálogo. Auditar via `audit_repo`.
- **Migração** (idempotente, no startup ou script): `mesh_groups` →
  `pipelines` (cada grupo vira um pipeline `rascunho` com os mesmos
  name/color/agent_ids); `mesh_chain_names[rootId]` → se o root tiver um pipeline,
  herda o nome. Manter `mesh_groups` por compat até a Topologia migrar (Parte A
  PR3) — NÃO quebrar a Topologia.

### 4.3 Frontend (`mesh_flow.html`)

- **Painel lateral de Pipelines** (novo, à esquerda do canvas): seções
  colapsáveis `Rascunhos / Publicados / Aposentados`; cada pipeline com dot de
  cor, nome (rename inline), chip de domínio, contagem; botão "+ Novo pipeline";
  item "Mesh completo" (view atual). Selecionar um pipeline → `selectedPipeline`
  filtra o canvas (lente): `nodes` e `edges` reduzidos aos membros + conexões
  intra‑pipeline. Posições continuam de `mesh_node_positions`.
- **Barra de filtros + paleta** (quando um pipeline está selecionado em modo
  construção): chips de domínio (derivar `distinct` dos nodes), tipo
  (Maestro/Triagem/Especialista = `aobd/router/subagent`), e busca por nome
  (substring client‑side). Resultados = agentes ainda NÃO no pipeline → "+ incluir"
  (chama `POST /pipelines/{id}/agents`, recarrega).
- **Pill de status** no header do pipeline com transições (chama `/status`).
  Tratamento visual: rascunho (tracejado/apagado), publicado (sólido), aposentado
  (cinza/baixa opacidade).
- **Criatividade**: "Criar pipeline a partir deste fluxo" (escolhe raiz →
  auto‑inclui a cadeia BFS); no "Mesh completo", conexões que cruzam pipelines
  ganham destaque (hand‑offs).
- Reusar tudo que já existe: render por `x-html` (`edgesSvg`/`minimapSvg`),
  filtros client‑side sobre `this.nodes`, `api`/`showToast` globais.

### 4.4 PRs da Parte A

**PR1 — Pipelines de 1ª classe (organização + lifecycle, runtime INTACTO)**
- Backend: tabelas `pipelines`+`pipeline_agents`, `pipeline_lifecycle.py`,
  `routes/pipelines.py` (CRUD + /status + /agents), migração de `mesh_groups`.
- Frontend: painel lateral, lente do canvas, filtros+paleta, pill de status.
- Testes: máquina de estados (transições válidas/inválidas), endpoints
  (CRUD, /status governado 422, /agents exclusividade), migração.
- **Aceitação**: criar/renomear pipeline; incluir/remover agente (exclusivo);
  selecionar pipeline filtra o canvas; mudar status; "Mesh completo" mostra tudo;
  **runtime inalterado** (execução de pipeline idêntica).
- Versão: nova func → MAJOR (→ 9.0.0). Bump conforme regra.

**PR2 — Status gateia o runtime (governança)**
- `execute_pipeline`/`is_pipeline_entry`: pipeline `aposentado` → não roteável;
  `rascunho` → isolado do runtime (ex.: agentes só‑rascunho não entram em chains
  publicadas, ou um gate explícito por pipeline). Definir a semântica exata com
  cuidado; auditar.
- Testes de runtime (rascunho não executa; publicado executa; aposentado bloqueado).
- Risco médio — toca o motor. Versão: melhoria/func conforme alcance.

**PR3 — Unificar raiz/cadeia + Topologia consome pipelines**
- Centralizar detecção de raiz/cadeia numa fonte (idealmente enriquecer
  `GET /mesh/topology` com `roots`/`chains`/`pipeline_id` por nó) e fazer
  mesh.html + workspace.html consumirem (fim da triplicação e da fragilidade de
  `mesh_chain_names`). Topologia passa a mostrar pipelines (entidade) no lugar de
  chain‑names soltos.

---

## 5. PARTE B — Catálogo pipeline‑native (deep update) — **CONFIRMADO**

> Escopo travado com o usuário: **novo kind `pipeline` (grafo)** + **pipeline‑native
> completo (PR4–PR7)**. Federação/disclosure (PR8) opcional. Detalhar cada PR
> quando chegar a vez (após o PR1 do estúdio).

### 5.1 Visão
Hoje o Catálogo só conhece **recipes lineares** + capabilities únicas. A proposta:
o Catálogo passa a ser a **loja/registro de PIPELINES visuais** — você constrói no
Fluxograma (Parte A) e **publica no Catálogo** como capability governada,
descobrível, executável e medida (trust/custo/observabilidade que o catálogo já
tem).

### 5.2 Mapeamento de lifecycle (pipeline 3 ↔ catálogo 6)
No momento da publicação, a pipeline (3 estados) vira/atualiza uma entry do
catálogo (6 estados):
- pipeline `rascunho` → não publicada (ou catalog `draft`, privada ao owner).
- pipeline `publicado` → catalog `published` (vivo/consumível).
- pipeline `aposentado` → catalog `deprecated` (invocável com aviso) ou `archived`.

### 5.3 Eixos do deep update (cada um vira 1+ PR; priorizar com o usuário)
- **PR4 — Publicar pipeline → Catálogo**: botão "Publicar no Catálogo" no
  estúdio; cria/atualiza uma `catalog_entries` (kind novo `pipeline` OU `recipe`
  estendido) referenciando o pipeline; passa pelo fluxo de lifecycle existente
  (`submit/approve/publish`). Decidir: kind novo `pipeline` vs reusar `recipe`.
- **PR5 — Recipe/Pipeline como GRAFO (não só linear)**: estender o modelo do
  catálogo para representar o grafo do mesh (agentes + conexões tipadas), não só
  `steps[]` lineares. Nova tabela `catalog_pipeline_defs` (snapshot do subgrafo)
  OU evoluir `catalog_recipes.steps` para DAG. Execução: reusar `execute_pipeline`
  em vez de só `execute_recipe`.
- **PR6 — UI do Catálogo pipeline‑aware**: telas do catálogo listam/gerenciam
  pipelines publicadas; detalhe mostra o grafo (mini‑fluxograma read‑only),
  trust/custo/latência, lifecycle. Reusar componentes do Fluxograma para render.
- **PR7 — Observabilidade/trust de pipelines**: alimentar
  `trust_reliability/latency/cost/invocation_count` do catálogo a partir de
  execuções reais (`pipeline_steps`/`catalog_recipe_executions`).
- **(Opcional) PR8 — Federação/disclosure**: alinhar pipelines com os recursos
  avançados do catálogo (disclosure de capability, OPA tiered, pricing) — só se o
  usuário quiser ir tão fundo.

### 5.4 Decisão central da Parte B — **TRAVADA**
**Pipeline publicado = novo kind `pipeline` (GRAFO).** `catalog_entries.kind`
ganha `'pipeline'` (CHECK estendido); o grafo (subconjunto do mesh: agentes +
conexões tipadas, snapshot na publicação) vive numa tabela nova
`catalog_pipeline_defs` (NÃO em `catalog_recipes.steps`, que é linear). Execução
reusa `execute_pipeline` (não `execute_recipe`). REUSAR: a máquina de lifecycle
(`catalog/lifecycle.py`) + o fluxo de publish/review/deprecate do catálogo +
trust/custo. NÃO forçar recipe linear (perderia condicional/paralelo/fan‑out).

---

## 6. Convenções do projeto (OBRIGATÓRIAS — ler antes de codar)

- **Ambiente**: roda em Docker local. A CADA mudança (inclusive só
  frontend/template): `docker compose build app && docker compose up -d app`
  (imagem é baked). App em `127.0.0.1:7000`. Cookie de auth root:
  `user_id=08768f10-a3ab-41db-8134-ba7c6768d1b1`.
- **DB**: Postgres `agente_inteligencia`, user `agente` (container `agente_postgres`).
  `platform_settings` é upsert por‑chave. JSONB+asyncpg: `json.dumps` antes (não
  passar dict cru).
- **Testes obrigatórios** por mudança (unit + integração), rodar suíte antes e
  depois (`python -m pytest tests/ -q`). LLM em teste local = `OPENAI_API_KEY` do
  `.env`. Funções puras (lifecycle, sanitização) → testáveis sem DB; endpoints →
  monkeypatch dos repos.
- **Bump de versão por PR** em `app/core/version.py` (MAJOR=nova func,
  MEDIUM=melhoria, MINOR=fix; SemVer com reset). **Informar a versão ao usuário ao
  fim.**
- **Fluxo de PR**: branch `feat/*` (manual: `gh pr create --base main` +
  `gh pr merge <n> --squash --delete-branch`). **Armadilha**: branch `fix/*`
  dispara auto‑PR+auto‑merge; e empilhar commits numa branch já squash‑mergeada
  gera conflito (resolver mergeando main na branch, ficando com a versão da branch
  quando ela é superconjunto). SEMPRE sair fresco da `main` após um merge.
- **Logs**: ler `LOG_DIR` (default `logs/`) antes de teorizar bug; instrumentar
  `event=` + `exc_info` em pontos de falha.
- **Sem regressão de runtime na Parte A**: o `execute_pipeline` NÃO muda no PR1.

## 7. Riscos e armadilhas
- Detecção de raiz triplicada (engine/mesh.html/workspace.html) — centralizar no
  PR3, não divergir antes.
- `mesh_groups`/`mesh_chain_names` keyed por agent_id são frágeis — a migração
  para `pipelines` resolve, mas manter compat com a Topologia até o PR3.
- Recipe do catálogo é LINEAR — não force pipeline=recipe sem decidir o modelo de
  grafo (5.4).
- Lente do canvas: ao filtrar por pipeline, garantir que conexões intra‑pipeline
  são as que têm AMBAS as pontas membros; cross‑pipeline só no "Mesh completo".

---

## 8. Ordem sugerida de execução
PR1 (estúdio: tabelas+endpoints+painel+filtros+lente, runtime intacto) →
**confirmar Parte B com o usuário** → PR2 (gating runtime) → PR3 (unificar raiz +
Topologia) → PR4..PR7 (catálogo pipeline‑native, na prioridade que o usuário der).
