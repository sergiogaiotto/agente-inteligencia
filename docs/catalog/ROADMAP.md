# Roadmap — Catálogo / Marketplace

Documento vivo. Captura o **plano da Onda 5+** mais o contexto necessário
para retomar sem reconstruir nada. Atualize conforme novas Ondas forem
fechando.

> **Última atualização**: 2026-05-19, fim da Onda 4 (PR #72 mergeado).

---

## Estado atual

**4 Ondas entregues** entre 2026-04 e 2026-05-19 — 26 PRs em main, 322
testes verdes, zero breaking changes acumulados.

| Indicador | Valor |
|---|---|
| PRs entregues | **26** (PRs #47-#72) |
| Endpoints REST | **32** sob `/api/v1/catalog` |
| Tabelas PostgreSQL | **7** (entries, submissions, capability_disclosure, costs, external_metadata, recipes, recipe_executions) |
| Páginas UI | **7** + 14 alteradas |
| Testes unitários | **322** em `tests/` |
| Módulos Python no catálogo | catalog/{lifecycle, models, prechecks, queries, urn, executor, anomalies} + core/llm_pricing |
| Audit actions distintas | **15** |
| Pré-checks no submit | **9** |

Veja [README.md](README.md) para visão geral, [ONDA4.md](ONDA4.md) para a
última leva entregue, e [REGRESSION.md](REGRESSION.md) Fase 1-8 para
checklist de regressão consolidado.

---

## Onda 5 — backlog priorizado

Sete itens reservados quando a Onda 4 fechou. Ordem sugerida por
**valor entregue ÷ esforço**, com o item mais leve primeiro para manter
momentum.

### 1. ⚙ Pricing editável via UI (leve)

**O quê**: migrar `app/core/llm_pricing.py` (hardcoded) para
`platform_settings` (key-value store já existente, usado pelo Onda 7
routing) ou tabela dedicada `llm_pricing`. UI em `/settings` permite
FinOps editar preço sem deploy.

**Por quê**: hoje toda mudança de preço passa por commit + revisão + deploy.
Tabelas públicas dos providers mudam trimestralmente (às vezes semanal em
preview). Self-service tira atrito de FinOps.

**Toca**: `app/core/llm_pricing.py` (`PRICING` dict → leitura dinâmica),
`app/templates/pages/settings.html` (nova seção), endpoint
`GET/PUT /api/v1/settings/llm_pricing`.

**Complexidade**: baixa-média. API de `compute_cost()` não muda — apenas
a fonte do dict.

**Pré-trabalho**: nenhum. Decidir só: platform_settings vs tabela nova
(perguntar ao user).

---

### 2. 🔍 Capability fingerprint (médio)

**O quê**: verificação por execução do capability disclosure (R6.x).
Quando agent é invocado, instrumentação detecta o que ele **de fato fez**
(chamou API externa? Leu KB? Processou padrão PII no input?) e compara
com o que o owner **declarou** em `catalog_capability_disclosure`.
Divergências viram alertas e baixam trust score.

**Por quê**: hoje a disclosure é auto-declarada — owner pode mentir (ou
errar). Sem verificação, R6.3 (etiqueta nutricional) é tão confiável
quanto o owner. Fingerprint fecha esse gap.

**Toca**: `app/agents/engine.py` (instrumentação do hot path),
`app/catalog/capability_disclosure` (campo `verification_method` já
prevê 'execution'), nova tabela `catalog_capability_evidence` ou JSONB
em disclosure.

**Complexidade**: média. Detectar "chamou API externa" é fácil (tem
binding_executions); detectar "processou PII no input" é heurístico
(regex de CPF/CNPJ/email/etc.).

**Pré-trabalho**: definir o que conta como "evidência" para cada um
dos 12 flags. Provavelmente começar pelos 4-5 mais óbvios (calls_external_apis,
processes_pii, stores_input, accesses_internet) e expandir.

---

### 3. 🚦 OPA tiered approval (médio)

**O quê**: três tiers de aprovação para submissões (R3.1):
- **community**: auto-approve (skills simples sem PII/external/etc.)
- **verified Root**: revisão Root atual
- **official auditor**: revisão dupla (Root + auditor externo) para
  kinds críticos (financial/health)

Decisão de tier é function dos pré-checks + capability disclosure +
kind/visibility.

**Por quê**: hoje **todo** submit espera fila Root. Volume cresce, Root
vira gargalo. Skills triviais (sem PII, sem APIs externas, visibility=private)
poderiam pular para auto-approve liberando atenção Root.

**Toca**: `app/catalog/lifecycle.py` (transições), `catalog_submissions`
(novo campo `tier`), `app/catalog/prechecks.py` (novo check para
classificar tier), `app/templates/pages/catalog_queue.html` (mostra tier).

**Complexidade**: média. Lógica de classificação é simples; o desafio é
**não criar back-door**: um submit auto-aprovado que depois muda capability
disclosure deve voltar para fila Root.

**Pré-trabalho**: definir matriz de classificação (quais combinações de
kind/visibility/disclosure caem em qual tier).

---

### 4. 📣 Audit de anomalias expandido (depende da infra)

**O quê**: anomalias (PR #71) hoje só viram banner visual em /catalog/cost
+ row no audit_log. Adicionar **notificação ativa**: email para Root,
mensagem em canal Slack/Teams, ou webhook configurável.

**Por quê**: anomalia que ninguém vê = anomalia que não existe. Banner
só dispara se alguém abre /catalog/cost.

**Toca**: `app/catalog/anomalies.py` (hook de notification),
nova integração `app/notifications/*` se não existir, settings para
configurar destino.

**Complexidade**: depende do que já existe. Se não houver infra de
notification, é um PR maior (criar essa infra). Se existe, é trivial.

**Pré-trabalho**: explorar `app/` por infra de notification existente.

---

### 5. 📉 Trust score erosion por drift (R5.2)

**O quê**: `catalog_entries.trust_reliability` decai automaticamente
quando entry não é invocada por N dias. Owner recebe alerta antes do
score cair de tier.

**Por quê**: agent não-usado pode estar quebrado e ninguém saber. Score
estagnado mascara o problema. Decay força revalidação.

**Toca**: novo background job (scheduler), `catalog_entries.trust_*`,
audit `trust_score_eroded`.

**Complexidade**: média. Lógica de decay é trivial. O ponto difícil é
**scheduler**: cron interno, APScheduler, ou Celery? O projeto não
parece ter um job runner formal hoje — checar antes.

**Pré-trabalho**: decidir job runner (consultar projeto).

---

### 6. 💸 Revenue-share em recipes (médio)

**O quê**: quando recipe da área X usa agent da área Y, área Y "recebe"
crédito interno proporcional ao custo. `catalog_costs` ganha agregação
por `agent_owner_department`.

**Por quê**: chargeback interno cria incentivo para áreas exporem
agents bem feitos (geram receita virtual). Sem isso, publicar no
catálogo é só custo para o owner.

**Toca**: agregação em `app/catalog/queries.py:aggregate_costs`
(novo group_by `agent_owner`), `/catalog/cost` (nova aba ou filtro),
relatório CSV.

**Complexidade**: média. Lógica de business mais que de infra. Schema
existente já guarda tudo necessário.

**Pré-trabalho**: alinhar com FinOps qual a unidade de "área" (owner
direto? `users.domains[0]`? Tag custom?).

---

### 7. 🌐 Federation URN entre instâncias Maestro (R5.3)

**O quê**: `urn:maestro:<workspace>:...` pode resolver entre instâncias
federadas. Empresa com Maestro corporativo + Maestro de BU pode
referenciar agents cross-instance via URN.

**Por quê**: multi-tenant/multi-org sem duplicar catálogo. Schema URN
de Onda 1 já prevê essa expansão (workspace='default' é placeholder).

**Toca**: `app/catalog/urn.py` (parsing de URN federado),
discovery protocol (HTTP entre instâncias), auth cross-instance.

**Complexidade**: alta. Network protocol + segurança. Provavelmente
quebra em sub-PRs (catalog/federation/discovery; catalog/federation/auth;
catalog/federation/resolver).

**Pré-trabalho**: design doc separado antes de codar.

---

### 8. ⚡ A2A bidirecional (design pesado)

**O quê**:
- Maestro **consome** agents de Maestros externos (cliente A2A)
- Maestro **expõe** agents publicados como MCP server (cliente externo invoca via MCP)

**Por quê**: ecossistema aberto. Integração com Cursor, Claude Desktop,
outros agentes externos que falam MCP.

**Toca**: `app/mcp/runtime.py` (873 linhas — base do servidor MCP já existe),
`app/a2a/protocol.py` (130 linhas — base do A2A), nova camada de
discovery + auth para cross-instance.

**Complexidade**: alta — exige **fase de design dedicada** antes do
primeiro PR. Não é one-shot.

**Pré-trabalho**:
- Documentar contratos A2A + MCP que vamos suportar
- Decidir auth cross-instance (API key trocada? OAuth? mTLS?)
- Definir gating (qualquer agent published vira MCP server? Tem opt-in?)
- Provavelmente **Onda 6** própria

---

## Ordem recomendada

```
Onda 5 (próxima leva, 4-5 PRs):
  PR 1  → Pricing editável via UI       (leve, valor pra FinOps)
  PR 2  → Capability fingerprint        (médio, fecha gap de governança)
  PR 3  → OPA tiered approval           (médio, libera Root)
  PR 4  → Audit de anomalias expandido  (depende de infra de notification)
  PR 5  → Regressão e fechamento

Onda 6 (uma para cada item pesado):
  - A2A bidirecional (fase de design dedicada)
  - Federation URN (provavelmente fora da Onda — multi-PR)

Pode entrar em Onda 5 ou Onda 6, dependendo de capacidade:
  - Trust score erosion (depende de scheduler)
  - Revenue-share em recipes (médio, business logic)
```

---

## Convenções estabelecidas (mantém em todas as Ondas)

Decisões aprendidas nas Ondas 1-4 que devem ser respeitadas em PRs futuros:

1. **Auth**: `Depends(require_user)` (cookie ou X-API-Key). Roles existentes
   (`root` / `comum` / `admin`); `users.domains` (JSON list) para steward
   de área. **Sem novas roles** — reusa o que existe.

2. **Helpers especializados em `queries.py`** quando PK ≠ `id`. Repository
   genérico hardcoda `WHERE id=$1` e não serve para `disclosure` (PK=entry_id),
   `external_metadata` (PK=entry_id), `recipes` (PK=entry_id), nem para
   queries com SQL específico de paginação ordenada.

3. **Visibility-aware via `can_user_see(user, entry)`** + `list_visible_entries`
   (SQL com filtro complexo). Filtro em SQL escala melhor que filtro em Python
   e produz paginação correta.

4. **Auditoria** via `audit_repo.create()` num helper `_audit()` em
   `routes/catalog.py`. Não auditar volume alto (cost por invocação) — só
   eventos discretos (decisão, publicação, anomalia detectada).

5. **Tests usam mini FastAPI** + `dependency_overrides[require_user]` +
   `monkeypatch` dos **helpers especializados** (não dos repos diretos
   quando PK ≠ id). Padrão estabelecido em `tests/test_catalog_api.py`.

6. **Auto-merge do GitHub está ativo**. `gh pr create` pode falhar com
   "No commits between main and X" se branch já foi auto-mergeada — checar
   com `git log origin/main` antes. Workflow normal:
   ```
   git checkout main && git pull
   git checkout -b feat/catalog-xxx
   # ... commits ...
   git push -u origin feat/catalog-xxx
   gh pr create --title "..." --body "..."
   # PR fica MERGED em segundos via auto-merge
   ```

7. **Fechamento de Onda** = PR `chore(catalog): regressão e fechamento da
   Onda N` com:
   - `docs/catalog/ONDAN.md` (novo)
   - `docs/catalog/REGRESSION.md` ganha Fase N+1 (numeração contínua)
   - `docs/catalog/README.md` atualiza métricas e tira 🚧
   - `docs/catalog/SMOKE_TEST.md` ganha seção PR de fechamento
   - Zero código de produção

8. **Commits** seguem `feat(catalog): tema (Onda N, PR M/total)` ou
   `chore(catalog): ...`. Co-author padrão.

9. **Onda 4 trade-offs aprendidos** (manter coerência em Onda 5):
   - Hardcoded > tabela DB para configs que mudam raramente (pricing, thresholds)
   - Background task via `asyncio.create_task` > Celery/RQ (in-process basta)
   - Cost auto-wire em fluxos que geram cost real; sandbox/dev → NÃO grava
   - Tabela dedicada > reaproveitar `interactions` quando o conceito é distinto

---

## Como retomar (próxima sessão)

Sugestão de primeira mensagem para a próxima sessão da Onda 5:

> "Onda 5 do Catálogo. Veja `docs/catalog/ROADMAP.md` para o backlog
> priorizado e `docs/catalog/ONDA4.md` para o que ficou pronto. Recomendação
> atual é começar por **Pricing editável via UI** — leve, alinha com
> demanda de FinOps. Concorda ou prefere outro item?"

A sessão saberá:
- Quais 8 itens estão no backlog e por que cada um
- Em que ordem fazem sentido
- Convenções a manter
- Que decisões precisam ser pré-fechadas antes de codar

---

## Quando atualizar este documento

- **Ao fechar Onda 5**: marcar itens entregues, mover restantes para
  Onda 6, atualizar "Estado atual".
- **Ao adicionar item novo ao backlog**: inserir na ordem certa por
  valor/esforço.
- **Ao descobrir nova convenção** durante uma Onda: adicionar à seção
  "Convenções estabelecidas".
