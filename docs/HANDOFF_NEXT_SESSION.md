# Handoff — Próxima Sessão

> **Última atualização**: 2026-05-23, Onda Tabular + 7 PRs de polimento (UX, help contextual, fix XLSX multi-aba, infra DuckDB).
> **Como usar**: leia este documento primeiro na próxima sessão, depois decida por onde começar.

---

## Estado atual da plataforma

- **~99 PRs em main** (após Onda Tabular base #110/#114/#115/#116 + #118 infra + #119 help submenu + #120/#121 fix XLSX multi-aba + #122/#123 help RAG+Tabelas + #124 UX defensiva)
- **686 testes verdes** (`pytest tests/`) — +18 desde o handoff anterior
- **Última atividade**: hardening da Onda Tabular após bug reportado pelo user (XLSX 2 abas + header mergeado não funcionava). Backend ganhou auto-detect de header_row + listagem de abas via openpyxl; frontend ganhou modal persistente fora do modal de ingest, seletor de aba, toast sempre presente, botão manual "Analisar planilha agora" como fallback. Ajuda contextual `/rag` reescrita para explicar RAG E Tabelas como técnicas distintas/complementares (#122/#123). Tela `/infra` ganha card+painel DuckDB (#118). Submenu Catálogo: cada sub-item tem help dedicado (#119).
- **Smoke manual em homolog pendente** — re-testar upload de XLSX multi-aba com header mergeado (deveria abrir modal próprio com 2 abas + auto-detect ativo).

### Onde olhar para contexto rápido

| Tópico | Documento |
|---|---|
| Visão geral do Catálogo (4 ondas entregues) | [docs/catalog/README.md](catalog/README.md) |
| Backlog priorizado da Onda 5+ | [docs/catalog/ROADMAP.md](catalog/ROADMAP.md) |
| Última Onda fechada (Onda 4: execução real de recipes) | [docs/catalog/ONDA4.md](catalog/ONDA4.md) |
| Checklist consolidado de regressão | [docs/catalog/REGRESSION.md](catalog/REGRESSION.md) |
| Smoke tests manuais (PR a PR) | [docs/catalog/SMOKE_TEST.md](catalog/SMOKE_TEST.md) |

---

## Backlog priorizado

### A. Gaps imediatos do API Connectors (não cobertos pelo PR #84)

Esses 4 itens foram **reconhecidos no PR #84** mas deixados fora do escopo para o PR não inchar. Resolvê-los completa a revisão de qualidade.

#### A1. ✅ Teste de integração declarative_engine ↔ API Connectors — concluído (PR #86)

**Entregue 2026-05-20**:
- `app/agents/declarative_engine.py` migrado para `app.core.http_auth` (remove `_build_auth_headers` e `_redact_headers` duplicados — 99 linhas down de 57)
- Suporte a 5 body types (json/form_urlencoded/multipart/text/xml) via `prepare_request_body` — antes só JSON
- `verify_ssl` e `follow_redirects=True` agora respeitados pelo engine (paridade com proxy_call)
- Auth headers passam por `redact_headers` e aparecem em `api_call_logs.request_headers` para auditoria (antes ficavam só nas chamadas HTTP, fora dos logs)
- `tests/test_declarative_engine_api.py`: **22 testes E2E novos** cobrindo connector resolution, 5 auth types (com decifragem at-rest), 5 body types, templating Jinja2 + JSONPath, retry/circuit breaker, persistência cruzada binding_executions ↔ api_call_logs, DAG com `depends_on` e compensação

---

#### A2. SimpleCookie — fallback para formatos exóticos

**O que é**: O parser atual usa `http.cookies.SimpleCookie` (RFC 6265 strict). Algumas APIs retornam Set-Cookie com formatos não-standard (ex: domínios sem ponto, atributos custom, valores com `;` literal escapado) que o SimpleCookie pode rejeitar. Hoje há fallback ao parser antigo manual, mas sem teste.

**Onde toca**: [app/routes/api_connectors.py:638-665](../app/routes/api_connectors.py#L638) — bloco do `extract_cookie`.

**Plano técnico**:
1. Coletar 3-4 exemplos reais de Set-Cookie problemáticos (Cloudflare CF_BM, AWS ALB stickiness, etc.)
2. Adicionar testes em `tests/test_api_connectors_routes.py` validando que cada formato é parseado OU cai no fallback sem perder o cookie principal
3. Documentar atributos efetivamente extraídos vs descartados

**Testes esperados**: ~6

**Complexidade**: baixa. Mais investigação que código.

---

#### A3. Encoding de body com charsets não-UTF-8

**O que é**: O response handling assume UTF-8 (`r.text[:5000]` em `app/routes/api_connectors.py:397`). APIs com `Content-Type: ...; charset=iso-8859-1` (sistemas legados brasileiros, governo, mainframes) vão devolver texto corrompido se houver acentos.

**Onde toca**:
- [app/routes/api_connectors.py:395-397](../app/routes/api_connectors.py#L395) — parse de response em `proxy_call`
- [app/routes/api_connectors.py:759](../app/routes/api_connectors.py#L759) — mesmo padrão em `extract_cookie`
- [app/agents/declarative_engine.py:236+](../app/agents/declarative_engine.py#L236) — também trata response

**Plano técnico**:
1. Usar `r.encoding` do httpx (que detecta via header `Content-Type` + meta tag HTML) em vez de assumir UTF-8
2. Helper `decode_response_body(response)` em `app/core/http_auth.py` que centraliza isso
3. Adicionar testes em `tests/test_http_auth.py` com `FakeResponse` retornando bytes ISO-8859-1, Windows-1252, EUC-JP, etc.
4. Garantir que strings sem encoding válido caem em `errors='replace'` (não levantam exception)

**Testes esperados**: ~5

**Complexidade**: baixa-média. Cuidar para não quebrar APIs UTF-8 que já funcionam.

---

#### A4. Circuit breaker em proxy_call e test_connector

**O que é**: O `app/agents/declarative_engine.py:343-378` tem circuit breaker (5xx + timeout disparam fail/continue/compensate). Mas o `proxy_call` e `test_connector` (usados pela UI Request Builder) não têm — uma API morta vai martelar a cada clique.

**Onde toca**:
- [app/routes/api_connectors.py:360+](../app/routes/api_connectors.py#L360) — `proxy_call`
- [app/routes/api_connectors.py:307+](../app/routes/api_connectors.py#L307) — `test_connector`

**Plano técnico**:
1. Helper `circuit_breaker_check(connector_id)` em `app/core/http_auth.py`:
   - Lê últimas N chamadas a esse connector em `api_call_logs`
   - Se M consecutivas falharam (timeout/5xx), abre o circuito por T segundos
   - Cache in-memory simples (dict com timestamp); reset ao retentar com sucesso
2. Aplicar em `proxy_call` (returna 503 com mensagem clara em vez de mais um timeout) e `test_connector`
3. Endpoint `POST /admin/circuit-reset/{connector_id}` para reset manual
4. Testes: mock múltiplas falhas seguidas + verificar bloqueio + reset

**Configuração sugerida** (hardcoded em `core/http_auth.py`):
- `CIRCUIT_FAILURES_TO_OPEN = 5`
- `CIRCUIT_OPEN_DURATION_S = 60`
- `CIRCUIT_HALF_OPEN_PROBE = True` (tenta 1 chamada após o cooldown)

**Testes esperados**: ~8

**Complexidade**: média. Lógica de estado in-memory + concorrência (async lock).

---

### A0 (2026-05-23). Onda Tabular — ENTREGUE em main + 7 PRs de polimento

**Status**: ✅ Backbone + frontend + 7 PRs de hardening em main. Suite 686 verde.

**PRs base (Onda Tabular original)**:
- [#110](https://github.com/sergiogaiotto/agente-inteligencia/pull/110) — `feat(tabular): backend DuckDB + endpoints (PR 1/4)` — `app/data_tables/`, `app/evidence/tabular.py`, `app/routes/data_tables.py`, DDL + repos, 55 testes
- [#115](https://github.com/sergiogaiotto/agente-inteligencia/pull/115) — `feat(tabular): parser ## Data Tables + engine (re-PR 2/4)` — `app/skill_parser/parser.py`, `app/agents/declarative_engine.py`, 19 testes (originais #111 ficou em base obsoleta)
- [#116](https://github.com/sergiogaiotto/agente-inteligencia/pull/116) — `feat(tabular): frontend KB — promote + tab Tabelas (re-PR 3/4)` — `app/templates/pages/evidence.html` (originais #112 ficou em base obsoleta)
- [#114](https://github.com/sergiogaiotto/agente-inteligencia/pull/114) — `feat(tabular): frontend Skills — Query Builder (PR 4/4)` — `app/templates/pages/skill_form.html` (recriado automaticamente pelo GitHub após #113 ficar em base obsoleta)

**PRs de polimento pós-entrega**:
- [#118](https://github.com/sergiogaiotto/agente-inteligencia/pull/118) — `feat(infra): DuckDB visível na tela Infraestrutura` — card+painel com versão, tabelas ready, linhas totais, tamanho DB vs disco
- [#119](https://github.com/sergiogaiotto/agente-inteligencia/pull/119) — `fix(help): ajuda específica para Fila Root / Inventário / Stewardship` — 3 chaves help dedicadas (antes todos abriam ajuda genérica de Catálogo)
- [#120](https://github.com/sergiogaiotto/agente-inteligencia/pull/120) e [#121](https://github.com/sergiogaiotto/agente-inteligencia/pull/121) — `fix(tabular): XLSX multi-aba + auto-detect header mergeado + modal persistente` — backend ganha `_list_xlsx_sheets()` via openpyxl + `_xlsx_sheet_dimensions()` para range válido, auto-retry com header_row=2 quando linha 1 parecia título mergeado (TB_*); frontend ganha modal próprio independente do modal de ingest (sobrevive ao fechar) com seletor de aba; +7 testes
- [#122](https://github.com/sergiogaiotto/agente-inteligencia/pull/122) e [#123](https://github.com/sergiogaiotto/agente-inteligencia/pull/123) — `docs(help): ajuda RAG agora explica RAG E Tabelas (text-to-SQL)` — chave evidence reescrita (5x mais conteúdo), nova seção "Quando usar cada técnica", 5 pegadinhas específicas de Tabelas
- [#124](https://github.com/sergiogaiotto/agente-inteligencia/pull/124) — `fix(tabular-ux): feedback explícito + botão manual de análise + logs` — painel inline pós-ingest sempre visível (loading/error/ok/idle states), botão "Analisar planilha agora" como fallback, console.log para debug

**Lições aprendidas (cf. memory feedback_stacked_prs_automerge.md)**:
1. Stacked PRs (`--base feat/X`) + auto-merge ativo = armadilha. Próxima vez: criar com `--base main` sempre. Validar com `git log main --oneline | grep <feature>` antes de declarar entregue.
2. DuckDB `read_xlsx` exige range no formato `A2:Cx` (limite explícito de coluna). `A2`, `ZZZ` ou range só com origem retornam Binder Error. Sempre usar openpyxl pra descobrir `max_column` da aba.
3. Auto-trigger de análise tabular em background sem feedback visível ≠ "deu certo silenciosamente". UX precisa SEMPRE mostrar estado (loading/ok/error/idle) + botão manual de retry.

**Smoke test manual pendente** (em homolog):
1. Upload XLSX com 2 abas + título mergeado em /rag → modal próprio abre com 2 abas + auto-detect ativo
2. "Promover todas as 2 abas" → cria 2 data_tables com URNs diferenciadas
3. Tab "Tabelas" no drawer Inspecionar lista as 2 com schema correto
4. /skills/new → 4º botão "Inserir Tabela" mostra as 2 com KS de origem
5. Query Builder → preview real com inputs templated → insere YAML válido em ## Data Tables
6. Criar skill declarative, executar agente, verificar resultado em `context.tables.<id>`
7. User não-root NÃO vê tabelas de KS com `confidentiality_label='restricted'`
8. /infra mostra DuckDB como 9º serviço + painel "Tabelas Operacionais"
9. `?` em Fila Root / Inventário / Stewardship abre ajuda específica de cada
10. `?` em /rag abre ajuda nova com 5 tabs (RAG + Tabelas)

**Decisões fixadas (não revisar sem motivo)**:
- DuckDB embarcado (1 arquivo `.duckdb` por tabela em `data/tabular/<ks_id>/<table_id>.duckdb`)
- Read-only por execução (safety técnica, não só prompt)
- Bind vars `?` (nunca string interpolation — defesa contra SQL injection)
- Dual-mode (RAG + tabela coexistem na mesma KS)
- 13 operadores SQL no MVP (= != > >= < <= LIKE ILIKE IN NOT IN BETWEEN IS NULL IS NOT NULL)
- Hardcoded: MAX_ROWS_RETURNED=1000, MAX_TABLE_SIZE_MB=50, MAX_COLUMNS=100 (convenção #9)
- Visibility herdada da KS (`confidentiality_label`)
- XLSX multi-aba: 1 data_table por aba (display name "arquivo — aba", slug com sufixo)
- XLSX header_row=2 auto-detect quando linha 1 parecer título mergeado

**Onda Tabular 2+ (não escopo)**: JOIN cross-table, AND/OR grouping em filtros, case-sensitivity explícita em LIKE, dashboard de queries mais usadas, GROUP BY + agregações.

---

### B. Outros pendentes do projeto

#### B1. PR #79 — módulos reescritos (status open?)

Durante a sessão atual, o arquivo `app/static/js/module-guide.js` foi **revertido localmente** (por linter ou edição manual do usuário) para o conteúdo antigo. O PR #79 foi aberto com os 14 módulos no tom novo, mas pode estar fora de sincronia.

**Verificar primeiro**:
```bash
gh pr view 79 --json state,mergedAt,mergeable
```

Se ainda open: ou (a) atualizar o branch com a versão atual de main e fechar manualmente, ou (b) marcar como obsolete e gerar PR novo. Não reverter o que está em main.

#### B2. Roadmap Onda 5+ — 8 itens em backlog

Lista completa em [docs/catalog/ROADMAP.md](catalog/ROADMAP.md) (linhas com sub-headings `### 1.` até `### 8.`). Sumário por valor/esforço:

1. **Pricing editável via UI** (leve) — migrar `app/core/llm_pricing.py` para `platform_settings` (DB editável)
2. **Capability fingerprint** (médio) — verificação por execução do disclosure (declarado vs detectado)
3. **OPA tiered approval** (médio) — community auto, verified Root, official auditor
4. **Audit de anomalias expandido** (depende infra notification) — email/Slack/webhook em cima do PR #71
5. **Trust score erosion por drift** (médio) — background job
6. **Revenue-share em recipes** (médio) — chargeback interno entre áreas
7. **Federation URN** (alta) — multi-instância Maestro
8. **A2A bidirecional** (alta — design pesado) — consumir Maestros externos + expor agents como MCP server

Ordem recomendada no ROADMAP: começar pelo #1 (leve, valor pra FinOps), depois #2 ou #3.

#### B3. Smoke tests manuais pendentes em homolog

Cada PR grande tem um item "smoke manual no browser" não marcado:
- PR #67-#71 (Onda 4 do Catálogo)
- PR #74 (gpt-oss + Qwen3)
- PR #76 (Modelo Primário)
- PR #77-#82 (reescrita Guia Interativo)
- PR #83 (tool strategy adaptive)
- PR #84 (API Connectors quality)

Quando subir homolog, percorrer os SMOKE_TEST.md correspondentes.

---

## Convenções estabelecidas (manter)

Decisões aprendidas em 84 PRs que devem ser respeitadas em PRs futuros — extraído também do [docs/catalog/ROADMAP.md](catalog/ROADMAP.md):

1. **Auth via `Depends(require_user)`** (cookie ou X-API-Key). Roles existentes: `root`/`comum`/`admin` + `users.domains` (JSON list) para steward de área. Sem criar novas roles — reusa as 3.
2. **Helpers especializados em `queries.py`** quando PK ≠ `id`. Repository genérico não serve para tabelas com PK composta ou semântica diferente.
3. **Visibility-aware em SQL**, não filtro Python. Paginação correta + performance.
4. **Auditoria via `audit_repo.create()`** num helper `_audit()` em routes. Não auditar volume alto (cost por invocação) — só eventos discretos.
5. **Tests usam mini FastAPI** + `dependency_overrides[require_user]` + `monkeypatch` dos helpers especializados. Padrão em `tests/test_catalog_api.py`.
6. **Auto-merge do GitHub está ativo** — `gh pr create` pode falhar com "No commits between main and X" se branch já foi auto-mergeada. Checar com `gh pr list --search "head:nome"` antes.
7. **Fechamento de Onda** = PR `chore(catalog): regressão e fechamento da Onda N` com ONDAN.md + Fase N+1 em REGRESSION.md + tira 🚧 do README.
8. **Commits** seguem `feat(scope): tema (Onda N, PR M/total)` ou `chore(scope): ...`. Co-author padrão.
9. **Hardcoded > tabela DB** para configs que mudam raramente (pricing, thresholds, capability map). Quando frequência aumentar, migra sem mudar API.
10. **Background task** via `asyncio.create_task` em rotas FastAPI (in-process basta). Sem Celery/RQ até demanda comprovada.
11. **Cost auto-wire** em fluxos que geram cost real; sandbox/dev → NÃO grava em `catalog_costs`.
12. **Tabela dedicada > reaproveitar `interactions`** quando o conceito é distinto (ex: `catalog_recipe_executions` em vez de overload em `interactions`).
13. **Helpers HTTP centralizados em `app/core/http_auth.py`** (auth headers + body preparation). Sem duplicação entre rotas e engine.
14. **Secrets at-rest cifrados** via `app/core/crypto.py` (Fernet). Backward compat preservada (valores sem `enc::` tratados como plaintext legacy).
15. **Tom de UX writing**: profissional friendly, sem emojis, com analogias concretas. Schema rico do help-content.js (concept/fundamentos/campos/casos_de_uso/exemplo/pegadinhas) é o padrão para qualquer tela nova.

---

## Como retomar (prompt sugerido)

Copie-cole isto na primeira mensagem da próxima sessão:

> Continue de onde paramos. Leia `docs/HANDOFF_NEXT_SESSION.md` para contexto. Estado atual: ~99 PRs em main, 686 testes verdes. Onda Tabular entregue 2026-05-23 (#110/#114/#115/#116) + 7 PRs de polimento (#118-#124). Smoke manual em homolog ainda pendente (10 passos listados na seção A0). Próximos candidatos: (a) smoke da Onda Tabular, (b) Onda Tabular 2+ (JOIN cross-table / AND-OR grouping / dashboard de queries), (c) A2 SimpleCookie fallback do API Connectors, (d) Roadmap Onda 5+ do Catálogo (Pricing editável via UI = leve). Qual prioridade?

A primeira sessão saberá:
- Estado atual sem precisar reconstruir
- Onda Tabular entregue + smoke test pendente
- 3 gaps restantes do API Connectors (A2/A3/A4) com plano técnico
- 8 itens da Onda 5+ se quiser pivotar
- Convenções a manter (incluindo nova: cuidado com stacked PRs + auto-merge)

---

## Quando atualizar este documento

- **Ao fechar um bloco de PRs** (ex: gaps do API Connectors resolvidos): mover de "pendente" para "concluído" no histórico abaixo.
- **Ao identificar gap novo** durante review/incident: adicionar na seção A com plano técnico.
- **Ao finalizar uma Onda**: replicar pattern de `docs/catalog/ONDA4.md` e atualizar o ROADMAP.

### Histórico de handoffs (mais recente primeiro)

| Data | Sessão | PRs entregues | Próximo |
|---|---|---|---|
| 2026-05-23 (noite) | Polimento da Onda Tabular pós-bug report | #118-#124 (7 PRs, +18 testes) | Smoke manual em homolog |

| Data | Sessão | PRs entregues | Próximo |
|---|---|---|---|
| 2026-05-23 | Onda Tabular completa (4 fases) — CSV/XLSX → DuckDB consultável via Skills | NÃO commitado (+74 testes) | Empacotar em 4 PRs ou consolidar em 1, smoke manual em homolog |
| 2026-05-20 | A1 — declarative_engine integration tests | #86 (1 PR, +22 testes) | A2/A3/A4 do API Connectors |
| 2026-05-20 | Sessão de qualidade (API Connectors + tool strategy + Guia Interativo) | #77-#84 (8 PRs) | Gaps A1-A4 do API Connectors |
| 2026-05-19 | Sessão Onda 4 Catálogo + GPT-OSS/Qwen3 + Modelo Primário | #67-#76 (10 PRs) | Reescrita Guia Interativo (5 PRs) |
| 2026-05-19 | Sessão Catálogo Ondas 1-3 | #47-#66 (20 PRs) | Onda 4 (execução real de recipes) |
