# Handoff — Próxima Sessão

> **Última atualização**: 2026-05-20, fim da revisão profunda de API Connectors (PR #84).
> **Como usar**: leia este documento primeiro na próxima sessão, depois decida por onde começar.

---

## Estado atual da plataforma

- **84 PRs em main**, zero PRs abertos
- **494 testes verdes** (`pytest tests/`)
- **Última atividade**: revisão de qualidade do módulo API Connectors (#84) — saiu de zero para 71 testes + 4 features + 6 fixes + 2 schema migrations

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

#### A1. Teste de integração declarative_engine ↔ API Connectors

**O que é**: O `app/agents/declarative_engine.py` executa API bindings declarativos invocando o mesmo `httpx.AsyncClient` que o `proxy_call`. Hoje **não há nenhum teste** validando que a integração funciona end-to-end (binding YAML do SKILL.md → resolve connector → envia request → grava `api_call_logs` + `binding_executions`).

**Onde toca**:
- [app/agents/declarative_engine.py:139-175](../app/agents/declarative_engine.py#L139) — `_build_auth_headers` (duplicado; deve usar `app/core/http_auth` agora)
- [app/agents/declarative_engine.py:189-260](../app/agents/declarative_engine.py#L189) — `_execute_http_call` + `_call_with_retry`
- [app/agents/declarative_engine.py:465-556](../app/agents/declarative_engine.py#L465) — persistência em `binding_executions` + `api_call_logs`

**Plano técnico**:
1. Remover `_build_auth_headers` local do declarative_engine; importar de `app.core.http_auth`
2. Aplicar `prepare_request_body(body_type, body)` no `_execute_http_call` (hoje só JSON)
3. Criar `tests/test_declarative_engine_api.py` com:
   - Mock httpx (mesmo padrão do `test_api_connectors_routes.py`)
   - Fixture com connector + skill com `api_bindings` declarado
   - Validar request enviado (URL final, headers de auth, body), response renderizado via Jinja2 + jsonpath
   - Retry com 5xx + backoff
   - Circuit breaker `on_failure: fail|continue|compensate`
   - Persistência cruzada (binding_executions linked com api_call_logs via interaction_id)

**Testes esperados**: ~15-20

**Complexidade**: média. Mais testes que código novo (o engine já existe e funciona; falta só cobertura).

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

> Continue de onde paramos. Leia `docs/HANDOFF_NEXT_SESSION.md` para contexto. Estado atual: 84 PRs em main, 494 testes verdes. Quero atacar os gaps do API Connectors (seção A do handoff) — recomendação atual é começar por **A1 (declarative_engine integration tests)** porque destrava cobertura da parte mais usada do módulo. Concorda ou prefere outro item?

A primeira sessão saberá:
- Estado atual sem precisar reconstruir
- 4 gaps prioritários do API Connectors com plano técnico de cada
- 8 itens da Onda 5+ se quiser pivotar
- Convenções a manter
- Por onde começar (sugestão A1)

---

## Quando atualizar este documento

- **Ao fechar um bloco de PRs** (ex: gaps do API Connectors resolvidos): mover de "pendente" para "concluído" no histórico abaixo.
- **Ao identificar gap novo** durante review/incident: adicionar na seção A com plano técnico.
- **Ao finalizar uma Onda**: replicar pattern de `docs/catalog/ONDA4.md` e atualizar o ROADMAP.

### Histórico de handoffs (mais recente primeiro)

| Data | Sessão | PRs entregues | Próximo |
|---|---|---|---|
| 2026-05-20 | Sessão de qualidade (API Connectors + tool strategy + Guia Interativo) | #77-#84 (8 PRs) | Gaps A1-A4 do API Connectors |
| 2026-05-19 | Sessão Onda 4 Catálogo + GPT-OSS/Qwen3 + Modelo Primário | #67-#76 (10 PRs) | Reescrita Guia Interativo (5 PRs) |
| 2026-05-19 | Sessão Catálogo Ondas 1-3 | #47-#66 (20 PRs) | Onda 4 (execução real de recipes) |
