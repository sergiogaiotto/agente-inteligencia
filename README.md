# AgenteInteligência-AI

**Plataforma de Gestão e Desenvolvimento de Multi-Agentes Orientada a SKILL.md sobre AI Mesh**

Versão 2.0.0 — Implementação da Especificação Funcional §1-§24

---

## 1. Visão Geral

Plataforma de agentes hierárquica, poliárquica em execução e monárquica em governança, onde cada agente é um processo computacional cuja identidade funcional é definida por um artefato declarativo SKILL.md. O SKILL.md não é documentação: é o contrato executável e a alma semântica do agente.

A topologia é composta por três camadas verticais de agentes (AOBD, AR, SA), acopladas lateralmente por protocolo Agent2Agent (A2A) que propaga contexto, estado e telemetria. O runtime opera uma máquina de estados de 9 estados (FSM §15) com verificação obrigatória de evidência antes de qualquer recomendação.

---

## 2. Arquitetura

```
┌──────────────────────────────────────────────────────────────────┐
│                      FRONTEND (Jinja2 + Tailwind + Alpine.js)    │
│  ┌───────────┐  ┌────────────────────────────┐  ┌────────────┐   │
│  │ Navigation│  │     Central Workspace      │  │  Context   │   │
│  │ (Left)    │  │  Dashboard/Chat/Editor/Mesh│  │  (Right)   │   │
│  └───────────┘  └────────────────────────────┘  └────────────┘   │
│  Command Palette (⌘K) · Guided Tour · Help (?) · Tooltips       │
└─────────────────────────────┬────────────────────────────────────┘
                              │ REST API (70+ endpoints)
┌─────────────────────────────▼───────────────────────────────────┐
│                        FASTAPI BACKEND                          │
│                                                                 │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────────┐ │
│  │ Routes  │  │ Wizard   │  │ Skill     │  │ Agent Engine     │ │
│  │ (CRUD)  │  │ IA       │  │ Parser    │  │ (LangGraph)      │ │
│  └─────────┘  └──────────┘  └───────────┘  └──────────────────┘ │
│  ┌─────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────────┐ │
│  │ A2A     │  │ Evidence │  │ State     │  │ Harness          │ │
│  │Protocol │  │ Runtime  │  │ Machine   │  │ Evaluator        │ │
│  └─────────┘  └──────────┘  └───────────┘  └──────────────────┘ │
│  ┌─────────┐  ┌──────────┐  ┌─────────────────────────────────┐ │
│  │ LLM     │  │LangFuse  │  │ SQLite (21 tabelas)             │ │
│  │Providers│  │Observab. │  │ Repositórios genéricos + KV     │ │
│  └─────────┘  └──────────┘  └─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 Princípios Arquiteturais Implementados

1. **SKILL.md é soberano** — nenhum comportamento de agente existe fora do declarado em seu SKILL.md
2. **Separação de planos** — control plane (catálogo, registro, política) distinto do data plane (execução)
3. **Determinismo declarativo** — o LLM decide como dentro do espaço que o SKILL.md permite; nunca o quê fora dele
4. **Contexto é cidadão de primeira classe** — propaga-se por envelope tipado, não por concatenação de histórico
5. **Evidência sobre geração livre** — toda recomendação verificada contra fontes autorizadas
6. **Recusa controlada é comportamento correto** — evidência insuficiente resulta em recusa estruturada, nunca silêncio

---

## 3. Stack Tecnológico

| Camada            | Tecnologia                                     | Função                                    |
|-------------------|-------------------------------------------------|-------------------------------------------|
| Linguagem         | Python 3.11+                                    | Backend completo                          |
| Framework Web     | FastAPI + Uvicorn                               | API REST assíncrona, alta performance     |
| Motor de Agentes  | LangGraph (StateGraph)                          | Grafos de execução com ciclos e condições |
| LLM — OpenAI      | langchain-openai (ChatOpenAI)                   | GPT-4o, GPT-4.1, o3, o4-mini             |
| LLM — Maritaca    | httpx + endpoint compatível OpenAI              | Sabiá-3, Sabiá-2                          |
| Observabilidade   | LangFuse (v2/v3/v4 compatível)                  | Traces, spans, métricas de custo          |
| Banco de Dados    | SQLite + aiosqlite                              | 21 tabelas, acesso assíncrono             |
| Template Engine   | Jinja2                                          | Renderização server-side de HTML          |
| CSS               | Tailwind CSS (CDN)                              | Utility-first, responsive, design system  |
| Interatividade    | Alpine.js 3.x                                   | Reatividade no frontend sem build step    |
| Tipografia        | DM Sans + JetBrains Mono                        | Display + monospace para código           |
| Containerização   | Docker + docker-compose                         | Deploy padronizado                        |

---

## 4. Instalação Passo a Passo

### 4.1 Pré-requisitos

- Python 3.11 ou superior
- pip (gerenciador de pacotes Python)
- Git (para controle de versão)
- Chave de API OpenAI e/ou Maritaca AI (para execução de agentes)

### 4.2 Clone e Setup

```bash
# 1. Clone o repositório
git clone https://github.com/sergiogaiotto/agente-inteligencia-ai.git
cd agente-inteligencia-ai

# 2. Crie o ambiente virtual
python -m venv .venv

# 3. Ative o ambiente virtual
# Linux/Mac:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\activate

# 4. Instale as dependências
pip install -r requirements.txt

# 5. Configure as variáveis de ambiente
cp .env.example .env
# Edite .env com suas API keys (ver seção 4.3)

# 6. Inicie a aplicação
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 7. Acesse no navegador
# http://localhost:8000
```

### 4.3 Configuração do .env

```env
# OpenAI — obrigatório para execução de agentes
OPENAI_API_KEY=sk-sua-chave-aqui
OPENAI_MODEL=gpt-4o

# Maritaca AI — opcional, LLM brasileiro
MARITACA_API_KEY=sua-chave-maritaca
MARITACA_API_URL=https://chat.maritaca.ai/api
MARITACA_MODEL=sabia-3

# LangFuse — opcional, observabilidade
LANGFUSE_PUBLIC_KEY=pk-sua-chave
LANGFUSE_SECRET_KEY=sk-sua-chave
LANGFUSE_HOST=https://cloud.langfuse.com

# DeepAgent — parâmetros do harness
DEEPAGENT_MAX_ITERATIONS=25
DEEPAGENT_TIMEOUT=120
```

### 4.4 Execução via Docker

```bash
docker compose up --build
# Acesse http://localhost:8000
```

### 4.5 Inicialização Automática

Ao iniciar, a aplicação executa automaticamente:
1. **Criação do banco** — SQLite em `data/agente_inteligencia.db` com 21 tabelas
2. **Migrações** — adiciona colunas faltantes (`created_at`, `title`) em tabelas existentes via `ALTER TABLE`
3. **Registro de rotas** — 70+ endpoints REST montados no FastAPI

---

## 5. Estrutura do Projeto

```
agente-inteligencia-ai/
├── app/
│   ├── main.py                    # Entry point FastAPI, lifespan, montagem de rotas
│   ├── core/
│   │   ├── config.py              # Pydantic Settings — carrega .env
│   │   ├── database.py            # Schema SQLite (21 tabelas), Repository genérico, SettingsStore, migrações
│   │   ├── llm_providers.py       # Factory de provedores LLM (OpenAI, Maritaca)
│   │   └── observability.py       # LangFuse client + CallbackHandler (v2/v3/v4 compatível)
│   ├── agents/
│   │   ├── engine.py              # Motor 3 camadas (AOBD→AR→SA), DeepAgent Harness, execute_interaction
│   │   └── state_machine.py       # FSM de 9 estados (§15), transições atômicas auditadas
│   ├── a2a/
│   │   └── protocol.py            # Envelope A2A tipado, IntentDescriptor, ContextDelta, assinatura
│   ├── evidence/
│   │   └── runtime.py             # Retriever + Reranker + Evidence Checker (§14)
│   ├── harness/
│   │   └── evaluator.py           # Avaliação contra dataset gold, gate de release (§9.5)
│   ├── skill_parser/
│   │   └── parser.py              # Parser canônico SKILL.md — frontmatter YAML + 17 seções (§5)
│   ├── models/
│   │   └── schemas.py             # Pydantic models para validação de request/response
│   ├── routes/
│   │   ├── agents.py              # CRUD agentes (AOBD/AR/SA)
│   │   ├── skills.py              # CRUD skills com parse canônico e validação
│   │   ├── workspace.py           # Chat via FSM, sessões (CRUD + rename)
│   │   ├── mesh.py                # Topologia AI Mesh + CAR (Catálogo de Roteadores)
│   │   ├── wizard.py              # Wizard IA — geração assistida de agentes e skills
│   │   ├── dashboard.py           # Dashboard, releases, gold cases, harness, knowledge, tools, settings, search, system prompts
│   │   └── frontend.py            # Rotas de renderização Jinja2 (14 páginas)
│   ├── templates/
│   │   ├── layouts/
│   │   │   └── base.html          # Layout base — nav, tour, help, search, toasts (750+ linhas)
│   │   └── pages/
│   │       ├── dashboard.html     # Dashboard com cards, topologia 3 camadas, tooltips
│   │       ├── agents.html        # Listagem de agentes com badges por tipo
│   │       ├── agent_form.html    # Formulário wizard 4 passos + IA assistida + prompts salvos
│   │       ├── skills.html        # Listagem de skills com preview do SKILL.md
│   │       ├── skill_form.html    # Editor SKILL.md + IA wizard + preview/validação
│   │       ├── workspace.html     # Chat com FSM, sidebar de sessões (rename/delete)
│   │       ├── mesh.html          # Visualização SVG da topologia + conexões
│   │       ├── evidence.html      # Gestão de bases de conhecimento (KNOWLEDGE_SOURCE)
│   │       ├── harness.html       # Dataset gold + execuções de avaliação + gate
│   │       ├── releases.html      # Version Registry — promoção staging→canary→production
│   │       ├── observability.html # Métricas e log de execuções + link LangFuse
│   │       ├── history.html       # Consulta unificada (interações, turnos, envelopes, auditoria)
│   │       └── settings.html      # Plataforma (API keys, modelos) + System Prompts (CRUD)
│   └── static/                    # CSS, JS, imagens estáticas
├── data/                          # Banco SQLite (auto-criado)
├── requirements.txt               # Dependências Python
├── .env.example                   # Template de variáveis de ambiente
├── Dockerfile                     # Build containerizado
├── docker-compose.yml             # Orquestração local
└── README.md                      # Este documento
```

---

## 6. Modelo de Dados — 21 Tabelas SQLite (§16)

### 6.1 Plataforma

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `agents` | Agentes nas 3 camadas | id, name, kind (aobd/router/subagent), domain, skill_id, llm_provider, model, system_prompt, status |
| `skills` | Contratos executáveis SKILL.md | id, urn, name, kind, domain, version, stability, purpose, workflow, tool_bindings, output_contract, raw_content, content_hash |
| `agent_bindings` | Vinculação agente↔skill↔MCP | agent_id, skill_id, model_serving_ref, mcp_servers |
| `mesh_connections` | Topologia do AI Mesh | source_agent_id, target_agent_id, connection_type (sequential/parallel/conditional) |
| `system_prompts` | Biblioteca de prompts reutilizáveis | name, category, kind, prompt_text, variables, is_default, version |
| `platform_settings` | Configurações key-value | key, value, updated_at (upsert via ON CONFLICT) |

### 6.2 Protocolo A2A (§7)

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `envelopes` | Mensagens entre agentes | trace_id, span_id, origin_agent_id, target_agent_id, intent (JSON), skill_ref, context (JSON), budget_remaining, deadline, signature |

### 6.3 Runtime de Interação (§15, §16)

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `interactions` | Sessões de atendimento | title, agent_id, channel, journey_id, state (FSM), release_id |
| `turns` | Turnos conversacionais | turn_number, user_text_redacted, output_text_redacted, interaction_id, latency_ms |
| `journeys` | Classificação de jornada | name, description, domain |

### 6.4 Evidence Runtime (§14)

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `knowledge_sources` | Bases autorizadas de conhecimento | name, source_type, confidentiality_label, authorized |
| `evidences` | Trechos citados em recomendações | snippet_text, relevance_score, confidentiality_label, knowledge_source_id, turn_id |

### 6.5 Tool Registry (§10)

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `tools` | Ferramentas MCP registradas | name, mcp_server, operations, cost_per_call, sensitivity, requires_trusted_context |
| `tool_calls` | Log de invocações MCP | tool_name, mcp_server, input_hash, output_hash, latency_ms, cost_usd |

### 6.6 Observabilidade (§17)

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `traces` | Rastreabilidade OpenTelemetry | trace_id, interaction_id, spans (JSON), duration_ms |
| `audit_log` | Log imutável de auditoria | entity_type, entity_id, action, actor, details (JSON), trace_id |

### 6.7 Release e Avaliação (§18, §9)

| Tabela | Descrição | Campos Principais |
|--------|-----------|-------------------|
| `releases` | Composições de artefatos | name, environment, model_config, prompt_config, index_config, policy_config, status, baseline_metrics |
| `gold_cases` | Dataset gold adversarial | dataset_version, case_type (normal/adversarial), input_text, expected_output, expected_state |
| `eval_runs` | Execuções de avaliação | release_id, run_type (baseline/regression), accuracy, correct_refusal_rate, false_positive_rate, gate_result |
| `car_entries` | Catálogo de Agentes Roteadores | skill_urn, domain, activation_keywords, success_rate, latency_p95 |
| `drift_events` | Detecção de drift | release_id, metric_name, baseline_value, current_value, magnitude, detection_method |

### 6.8 Técnicas de Persistência

- **Repository Pattern** — classe genérica `Repository(table)` com métodos `find_all`, `find_by_id`, `create`, `update`, `delete`, `count`, `search`. Reutilizada em 21 instâncias.
- **Auto-detecção de coluna de ordenação** — `_order_col(db)` consulta `PRAGMA table_info` e seleciona `created_at > started_at > id > rowid` como fallback, eliminando erros de coluna inexistente.
- **Migração automática** — `init_db()` executa `ALTER TABLE ADD COLUMN` para colunas faltantes em bancos existentes. Sem necessidade de deletar o banco ao atualizar.
- **SettingsStore** — key-value com `INSERT ... ON CONFLICT DO UPDATE` (upsert nativo SQLite).

---

## 7. Motor de Agentes — Três Camadas (§4)

### 7.1 AOBD — Agente Orquestrador de Business Domain

**Arquivo:** `app/agents/engine.py` — classe `AOBDOrchestrator`

Entidade única por domínio de negócio. Responsabilidades:

1. **Interpretação de intenção** — recebe texto natural, produz `IntentDescriptor` estruturado via LLM: `{domain, process_candidate, entities, constraints, urgency, actor}`
2. **Consulta ao CAR** — matching híbrido entre IntentDescriptor e roteadores registrados (filtro por keywords + score de sucesso)
3. **Delegação com envelope** — emite `DelegationEnvelope` assinado ao roteador eleito

### 7.2 AR — Agente Roteador

Representa um processo de negócio discreto. Responsabilidades:

1. **Hidratação de Skill** — carrega SKILL.md e valida contra schema
2. **Planejamento** — decompõe processo em DAG derivado da seção `## Workflow`
3. **Ativação de subagentes** — dispara A2A para cada nó do DAG

### 7.3 SA — Subagente

Unidade atômica de execução. Responsabilidades:

1. **Carregar SKILL.md** — torna-se system prompt efetivo
2. **Inspecionar tools** — cruza inventário MCP com Tool Bindings do skill
3. **Executar** — invoca tools sob condições prescritas
4. **Emitir resultado tipado** — conforme Output Contract

### 7.4 DeepAgent Harness

**Classe:** `DeepAgentHarness` — loop de raciocínio com auto-reflexão

**Técnica:** LangGraph `StateGraph` com dois nós (`reason` e `reflect`) conectados por edge condicional.

```
[reason] ──┬── "end" ──→ [END]
           └── "reflect" ──→ [reflect] ──→ [reason]
```

O nó `reason` gera resposta via LLM. O nó `reflect` avalia a resposta contra Output Contract e Guardrails. Se insatisfatória, retorna para `reason` com instrução de refinamento. Ciclo limitado por `max_iterations` (default: 3).

**AgentState (TypedDict):**
- `messages` — histórico de mensagens LangChain (append-only via reducer)
- `current_agent` — ID do agente em execução
- `agent_kind` — camada (aobd/router/subagent)
- `iteration` / `max_iterations` — controle de ciclo
- `context` — dicionário de fatos acumulados
- `envelope` — dados do envelope A2A
- `skill_data` — seções parseadas do SKILL.md

### 7.5 Tratamento de Erros de LLM

O `execute_interaction` captura erros de API com mapeamento:

| Código | Causa | Ação |
|--------|-------|------|
| — | API key ausente/placeholder | Retorna mensagem orientando para Configurações |
| 404 | Modelo inexistente | "Modelo X não encontrado no provedor" |
| 401 | Chave inválida | "API Key inválida ou expirada" |
| 429 | Rate limit | "Limite de requisições atingido" |
| timeout | LLM lento | "Timeout na chamada ao provedor" |

O erro é exibido como resposta do agente no chat (não como exceção HTTP 500).

---

## 8. Parser Canônico SKILL.md (§5)

**Arquivo:** `app/skill_parser/parser.py`

### 8.1 Anatomia do SKILL.md

```markdown
---
id: urn:skill:<domain>:<process>:<task>
version: 0.1.0
kind: orchestrator | router | subagent
owner: <equipe>
stability: alpha | beta | stable | deprecated
---

# Nome Humano

## Purpose              (obrigatória)
## Activation Criteria  (obrigatória)
## Inputs               (obrigatória)
## Workflow             (obrigatória)
## Tool Bindings        (obrigatória)
## Output Contract      (obrigatória)
## Failure Modes        (obrigatória)
## Delegations          (opcional)
## Compensation         (opcional)
## Guardrails           (opcional)
## Budget               (opcional)
## Examples             (opcional)
## Telemetry            (opcional)
## Data Dependencies    (opcional)
## Model Constraints    (opcional)
## Evidence Policy      (opcional)
## Gold Refs            (opcional)
```

### 8.2 Técnicas do Parser

1. **Pré-processamento** — remove code fences (`` ```markdown ... ``` ``) que o LLM wizard frequentemente adiciona
2. **Frontmatter YAML** — `re.search` (não `re.match`) para encontrar `---` em qualquer posição. Se ausente, gera defaults `SkillFrontmatter(kind='subagent', stability='alpha')` sem rejeitar
3. **Extração de seções** — regex `^##\s+(.+)$` com `re.MULTILINE` identifica todos os H2 e extrai conteúdo entre eles
4. **Nome (H1)** — extraído do primeiro `# heading`. Fallback: primeira linha não-vazia
5. **Hash de integridade** — SHA-256 do conteúdo bruto para detecção de alterações
6. **Validação tolerante** — erros de seções faltantes são registrados como warnings, não bloqueiam criação. Apenas ausência total de conteúdo bloqueia.

### 8.3 Endpoints

| Método | Rota | Função |
|--------|------|--------|
| `POST` | `/api/v1/skills/parse` | Preview/validação sem salvar — retorna seções encontradas, erros, hash |
| `POST` | `/api/v1/skills` | Cria skill via parse canônico (salva com warnings) |
| `PUT` | `/api/v1/skills/{id}` | Atualiza skill — re-parse, bump de versão automático, preserva tags |

---

## 9. Protocolo Agent2Agent — A2A (§7)

**Arquivo:** `app/a2a/protocol.py`

### 9.1 Envelope Tipado

```python
@dataclass
class Envelope:
    envelope_id: str      # UUID v4
    trace_id: str         # OpenTelemetry
    span_id: str          # OpenTelemetry
    parent_span_id: str   # Encadeamento
    origin_agent_id: str  # Emissor
    target_agent_id: str  # Destinatário
    intent: IntentDescriptor  # Preservado em toda a cadeia
    skill_ref: str        # urn:skill:...@version
    context: dict         # Fatos acumulados (append-only)
    budget_remaining: Budget  # {tokens, wall_ms, usd}
    deadline: str         # Timestamp absoluto
    signature: str        # SHA-256 truncado
```

### 9.2 IntentDescriptor

Estrutura produzida pelo AOBD após interpretação de texto natural:

```python
@dataclass
class IntentDescriptor:
    domain: str           # Ex: "financeiro"
    process_candidate: str # Ex: "apuracao_icms_mensal"
    entities: dict        # Ex: {competencia: "2026-03", filial: "07"}
    constraints: dict     # Restrições adicionais
    urgency: str          # normal | high | critical
    actor: str            # Identificação do requisitante
```

### 9.3 ContextDelta

Mudanças emitidas por subagente ao terminar — append-only, nunca sobrescreve:

```python
def apply_context_delta(current_context, delta):
    merged = {**current_context}
    for k, v in delta.additions.items():
        if isinstance(merged.get(k), list) and isinstance(v, list):
            merged[k] = merged[k] + v  # Append para listas
        else:
            merged[k] = v
    merged["_deltas"].append(...)  # Histórico de mutações
    return merged
```

---

## 10. Máquina de Estados da Interação — FSM (§15)

**Arquivo:** `app/agents/state_machine.py`

### 10.1 Estados

```
[*] → Intake → PolicyCheck → RetrieveEvidence → DraftAnswer → VerifyEvidence
                                                                    ├── Recommend → LogAndClose → [*]
                                                                    ├── Refuse → LogAndClose → [*]
                                                                    └── Escalate → LogAndClose → [*]
```

| Estado | Responsabilidade |
|--------|-----------------|
| **Intake** | Recebe solicitação, normaliza, cria InteractionContext, persiste turno |
| **PolicyCheck** | Avalia permissões via Policy Engine (OPA). Se negado → Refuse |
| **RetrieveEvidence** | Retriever busca evidências em bases autorizadas + Reranker reordena |
| **DraftAnswer** | LLM gera rascunho via DeepAgent Harness + Guardrails |
| **VerifyEvidence** | Evidence Checker valida consistência, cobertura, conflitos |
| **Recommend** | Evidência OK + política OK → recomendação final com citações |
| **Refuse** | Evidência insuficiente → recusa controlada com próximo passo |
| **Escalate** | Risco alto ou fraude → delegação a supervisor humano |
| **LogAndClose** | Estado terminal obrigatório — registra decisão, fecha ProcessContext |

### 10.2 Invariantes

1. Todo caminho termina em `LogAndClose` — não existe caminho que evite registro
2. `VerifyEvidence` é obrigatório — nenhum rascunho chega ao requisitante sem verificação
3. Transições são atômicas e auditadas — cada transição gera entrada no `audit_log`
4. `Escalate` preserva contexto completo (incluindo rascunho rejeitado)

### 10.3 Técnica de Implementação

- Enum `State` com 9 valores
- Dict `TRANSITIONS` define transições válidas por estado
- `InteractionStateMachine.transition()` valida transição, persiste estado no banco, registra em audit_log
- `InteractionContext` (dataclass) carrega todo o estado da interação: evidências, draft, score, transition_log

---

## 11. Evidence Runtime (§14)

**Arquivo:** `app/evidence/runtime.py`

### 11.1 Retriever

Busca híbrida em bases autorizadas de conhecimento:

- **Busca textual** — matching por keywords do query contra nome e descrição das KNOWLEDGE_SOURCEs (implementação SQLite; em produção: BM25 + busca vetorial)
- **Score de relevância** — proporção de termos encontrados, normalizada para [0, 1]
- **Filtro** — apenas bases com `authorized=1`
- **Top-N** — retorna as 5 evidências mais relevantes

### 11.2 Reranker

Cross-encoder simplificado que reordena evidências:

- Boost para evidências com termos exatos da query
- Score ajustado: `min(base_score + overlap * 0.1, 1.0)`

### 11.3 Evidence Checker

Verificador independente do LLM gerador:

**Prompt de verificação** estruturado com 4 dimensões:
1. **Consistência** — rascunho semanticamente consistente com evidências?
2. **Cobertura** — todas as afirmações cobertas por evidências?
3. **Conflito** — evidências mutuamente contraditórias?
4. **Risco** — indicativo de fraude ou risco alto?

**Saída:** `VerificationResult(ok, confidence, issues[], risk_high, fraud_suspected)`

**Fallback heurístico:** se LLM falhar, score médio de relevância das evidências determina OK/falha (threshold: 0.3)

---

## 12. Harness de Avaliação (§9.5)

**Arquivo:** `app/harness/evaluator.py`

### 12.1 Fluxo

1. Carrega casos gold do `dataset_version` especificado
2. Para cada caso, executa `execute_interaction` com input do caso
3. Compara `final_state` com `expected_state` (Recommend/Refuse/Escalate)
4. Compara output com `expected_output` via similaridade de termos (threshold: 30%)
5. Calcula métricas agregadas
6. Aplica gate de release

### 12.2 Métricas

| Métrica | Cálculo | Threshold |
|---------|---------|-----------|
| Acurácia | passed / total | ≥ 80% |
| Taxa de recusa correta | recusas corretas em casos adversariais / total adversariais | ≥ 70% |
| Taxa de falso positivo | recusas indevidas em casos normais / total | ≤ 15% |
| Regressão | (baseline_acc - current_acc) / baseline_acc × 100 | ≤ 5% |

### 12.3 Gate de Release

Resultado binário: `approved` ou `rejected`. Release com `rejected` não pode ser promovida para canary/production.

---

## 13. Wizard IA — Criação Assistida

**Arquivo:** `app/routes/wizard.py`

### 13.1 Endpoints

| Endpoint | Função |
|----------|--------|
| `POST /api/v1/wizard/agent` | Gera config completa de agente (nome, kind, domínio, system prompt, skills sugeridas) |
| `POST /api/v1/wizard/skill` | Gera SKILL.md canônico completo com todas as seções |
| `POST /api/v1/wizard/refine` | Melhora um campo ou conteúdo existente |
| `GET /api/v1/wizard/models` | Lista modelos disponíveis por provedor com context window e tier |

### 13.2 Técnica

- Prompt engineering com instruções estruturadas para output JSON (agentes) ou Markdown (skills)
- Extração de JSON de respostas envolvidas em code fences via regex
- Fallback gracioso: se JSON inválido, retorna conteúdo bruto como system_prompt

### 13.3 Modelos Disponíveis

**OpenAI:** GPT-4o, GPT-4o Mini, GPT-4 Turbo, GPT-4.1, GPT-4.1 Mini, GPT-4.1 Nano, o4-mini, o3, o3-mini, o1, o1-mini

**Maritaca AI:** Sabiá-3, Sabiá-3 (Jan/25), Sabiá-2 Medium, Sabiá-2 Small

---

## 14. Interface — 14 Páginas

### 14.1 Layout Base (`base.html`)

Componentes globais implementados em Alpine.js:

- **Navigation** — sidebar esquerda com 12 itens agrupados em 4 categorias (Principal, Operação, Ciclo de Vida, Análise)
- **Guided Tour** — 11 passos com spotlight overlay (CSS `box-shadow: 0 0 0 9999px`), card flutuante posicionado dinamicamente, progress dots
- **Help System** — botão `?` em cada item do menu com modal de 3 seções (O que é, Fundamento, Como usar) — 12 entradas cobrindo §4-§18
- **Command Palette** — `⌘K` / `Ctrl+K` abre busca global com debounce 300ms, resultados agrupados por tipo com ícones coloridos, navegação direta
- **Toast Notifications** — feedback visual para ações (success/error/info)

### 14.2 Páginas

| Página | Funcionalidades |
|--------|-----------------|
| **Dashboard** | Cards de métricas, topologia 3 camadas (AOBD/AR/SA com contagem), interações recentes com estado FSM, ações rápidas, módulos implementados com tooltips |
| **Agentes** | Grid de cards com badges (kind, status, modelo), editar/excluir com hover |
| **Novo Agente** | Wizard 4 passos (Básico→LLM→Prompt→Revisão), botão "IA, me ajude" com geração assistida, seleção de prompts salvos, botão "IA, refine" no system prompt, combo dinâmico de modelos com reset ao trocar provedor |
| **Skills** | Lista com preview SKILL.md, badges (version, kind, stability, domain), editar/excluir |
| **Nova/Editar Skill** | Editor SKILL.md com "IA, me ajude" (gera canônico completo), "IA, melhore" (refina), tab Preview/Validação (parse em tempo real com seções encontradas e erros), modo edição com PUT + bump de versão |
| **Workspace** | Chat com FSM, sidebar de sessões (rename inline, excluir), seletor de agente, indicador de conexão, formatação markdown na resposta, typing indicator |
| **AI Mesh** | Topologia SVG (nodes como divs posicionados, edges via x-html), formulário de conexão (sequential/parallel/conditional), lista de conexões com delete |
| **Evidência** | CRUD de KNOWLEDGE_SOURCEs com classificação de confidencialidade (public/internal/confidential/restricted) |
| **Harness** | Split view: dataset gold (criar casos normal/adversarial com expected_state) + eval runs (executar harness com gate approved/rejected) |
| **Releases** | Cards com badges de ambiente (staging/canary/production), promoção gated, filtro por ambiente |
| **Observabilidade** | Métricas (execuções, latência, tokens), log tabular de execuções, link para LangFuse externo |
| **Histórico** | Tabs (Interações/Turnos/Envelopes/Auditoria), busca textual, resultados com metadata |
| **Configurações** | Tab Plataforma (API keys, combos de modelos, LangFuse, DeepAgent, salvar com dirty detection) + Tab System Prompts (CRUD completo com filtros, editor modal, copiar para clipboard, versionamento) |

---

## 15. API REST — 70+ Endpoints

### 15.1 Agentes

```
GET    /api/v1/agents                    # Listar (filtros: kind, status, domain)
GET    /api/v1/agents/{id}               # Detalhe
POST   /api/v1/agents                    # Criar
PUT    /api/v1/agents/{id}               # Atualizar
DELETE /api/v1/agents/{id}               # Remover
```

### 15.2 Skills

```
GET    /api/v1/skills                    # Listar (filtros: kind, domain, stability)
GET    /api/v1/skills/{id}               # Detalhe
POST   /api/v1/skills/parse              # Preview/validação sem salvar
POST   /api/v1/skills                    # Criar via parse canônico
POST   /api/v1/skills/manual             # Criar via formulário
PUT    /api/v1/skills/{id}               # Atualizar (re-parse + bump version)
DELETE /api/v1/skills/{id}               # Remover
```

### 15.3 Workspace

```
GET    /api/v1/workspace/sessions        # Listar sessões
GET    /api/v1/workspace/sessions/{id}   # Detalhe + mensagens
PATCH  /api/v1/workspace/sessions/{id}   # Renomear
DELETE /api/v1/workspace/sessions/{id}   # Excluir
POST   /api/v1/workspace/chat            # Executar interação via FSM
```

### 15.4 AI Mesh + CAR

```
GET    /api/v1/mesh/topology             # Topologia (nodes + edges)
POST   /api/v1/mesh/connections          # Criar conexão
DELETE /api/v1/mesh/connections/{id}     # Remover conexão
GET    /api/v1/car                       # Catálogo de Roteadores
POST   /api/v1/car                       # Registrar entrada CAR
DELETE /api/v1/car/{id}                  # Remover entrada
```

### 15.5 Plataforma

```
GET    /api/v1/dashboard/stats           # Métricas agregadas
GET    /api/v1/settings                  # Carregar configurações
PUT    /api/v1/settings                  # Salvar configurações
GET    /api/v1/search?q=termo            # Busca global (5 tabelas)
GET    /api/v1/history                   # Histórico unificado
GET    /api/v1/drift-events              # Eventos de drift
```

### 15.6 Knowledge + Tools

```
GET    /api/v1/knowledge-sources         # Listar bases
POST   /api/v1/knowledge-sources         # Registrar base
DELETE /api/v1/knowledge-sources/{id}    # Remover
GET    /api/v1/tools                     # Listar tools MCP
POST   /api/v1/tools                     # Registrar tool
DELETE /api/v1/tools/{id}                # Remover
```

### 15.7 Releases + Harness

```
GET    /api/v1/releases                  # Listar releases
POST   /api/v1/releases                  # Criar release
PUT    /api/v1/releases/{id}/promote     # Promover (staging→canary→production)
GET    /api/v1/gold-cases                # Listar dataset gold
POST   /api/v1/gold-cases                # Criar caso gold
DELETE /api/v1/gold-cases/{id}           # Remover caso
GET    /api/v1/eval-runs                 # Listar avaliações
POST   /api/v1/eval-runs/execute         # Executar harness
```

### 15.8 System Prompts

```
GET    /api/v1/system-prompts            # Listar (filtros: category, kind)
GET    /api/v1/system-prompts/{id}       # Detalhe
POST   /api/v1/system-prompts            # Criar
PUT    /api/v1/system-prompts/{id}       # Atualizar (auto-bump version)
DELETE /api/v1/system-prompts/{id}       # Remover
```

### 15.9 Wizard IA

```
POST   /api/v1/wizard/agent              # Gerar config de agente
POST   /api/v1/wizard/skill              # Gerar SKILL.md canônico
POST   /api/v1/wizard/refine             # Refinar campo/conteúdo
GET    /api/v1/wizard/models             # Listar modelos por provedor
```

---

## 16. Técnicas de Frontend

| Técnica | Implementação |
|---------|--------------|
| **Spotlight Tour** | CSS `box-shadow: 0 0 0 9999px rgba()` cria overlay com recorte. Posição calculada via `getBoundingClientRect()`. Card posicionado dinamicamente (direita, esquerda ou abaixo conforme espaço). |
| **Command Palette** | Modal centralizado com input `@input.debounce.300ms`, resultados via `GET /api/v1/search`. Keyboard shortcut via `document.addEventListener('keydown')` para `Ctrl+K / ⌘K`. |
| **Tooltips** | `group` + `group-hover:block` do Tailwind. Div posicionada `absolute bottom-full` com seta CSS (border trick). |
| **Help Modal** | Conteúdo estático em objeto JS `helpContent` com 12 entradas. Modal ativado por `openHelp(key)`. |
| **Dirty Detection** | `$watch('config', ...)` compara `JSON.stringify(config)` com snapshot salvo. Botão salvar muda de cinza para azul quando há alterações. |
| **Inline Rename** | `renamingId` controla qual sessão está em modo edição. `x-show` alterna entre visualização e input. Enter confirma, Escape cancela. |
| **Toast Notifications** | `document.createElement('div')` com classes Tailwind, `setTimeout` para remoção automática (3.5s). |
| **SVG Mesh** | Nodes como `<div>` com `position:absolute` + `transform:translate(-50%,-50%)`. Edges geradas como string SVG via `renderEdges()` e injetadas com `x-html` (contorna incompatibilidade Alpine.js + SVG namespace). |
| **Step Wizard** | Variável `step` (0-3) controla visibilidade com `x-show="step === N"`. Indicadores com estados: ativo (azul), completo (verde ✓), pendente (cinza). |

---

## 17. Observabilidade (§17)

### 17.1 LangFuse

**Arquivo:** `app/core/observability.py`

- **Compatibilidade multi-versão** — tenta importar de `langfuse.langchain` (v4), `langfuse.callback` (v2/v3), `langfuse` (direto). Degrada silenciosamente se indisponível.
- **ObservabilityTracker** — singleton com métodos `create_trace`, `log_generation`, `log_span`, `flush`. Todos com try/except que nunca propagam erros.
- **CallbackHandler** — injetado no `ainvoke` do LangChain para rastreamento automático de chamadas LLM.

### 17.2 Audit Log

Toda ação significativa gera entrada no `audit_log`:
- Criação/edição de agentes, skills, prompts
- Transições de estado na FSM
- Salvamento de configurações
- Promoção de releases

---

## 18. Segurança e Boas Práticas

| Prática | Implementação |
|---------|--------------|
| **API keys nunca no código** | Carregadas via `.env` + `pydantic-settings` |
| **Validação de input** | Pydantic models em todos os endpoints |
| **Hash de integridade** | SHA-256 em SKILL.md para detecção de alterações |
| **Assinatura de envelope** | Hash truncado de campos críticos (id + target + skill) |
| **Auditoria completa** | `audit_log` append-only com entity_type, action, details |
| **Degradação graciosa** | LangFuse, Evidence Checker, LLM — todos com fallback |
| **Migração segura** | `ALTER TABLE` com try/except, nunca perde dados |

---

## 19. Mapeamento Especificação → Implementação

| Seção | Título | Arquivo Principal | Status |
|-------|--------|-------------------|--------|
| §4 | Topologia de Agentes | `agents/engine.py` | Implementado |
| §5 | SKILL.md Anatomia Canônica | `skill_parser/parser.py` | Implementado |
| §6 | Catálogo de Agentes Roteadores | `routes/mesh.py` (car_router) | Implementado |
| §7 | Protocolo A2A | `a2a/protocol.py` | Implementado |
| §8 | Gestão de Contexto e Estado | `a2a/protocol.py` (ContextDelta) | Implementado |
| §9.4 | Dataset Gold Adversarial | `routes/dashboard.py` (gold-cases) | Implementado |
| §9.5 | Harness de Avaliação | `harness/evaluator.py` | Implementado |
| §10 | Integração MCP / Tool Registry | `routes/dashboard.py` (tools) | Implementado |
| §11 | AI Mesh Infraestrutura | `routes/mesh.py` + `templates/mesh.html` | Implementado |
| §14 | Evidence Runtime | `evidence/runtime.py` | Implementado |
| §15 | Máquina de Estados | `agents/state_machine.py` | Implementado |
| §16 | Modelo de Dados | `core/database.py` (21 tabelas) | Implementado |
| §17 | Observabilidade | `core/observability.py` | Implementado |
| §18 | Version Registry / Drift | `routes/dashboard.py` (releases, drift) | Implementado |
| §20 | Avaliação Contínua | `harness/evaluator.py` | Implementado |

---

## 20. Licença

Proprietário — Sergio Gaiotto

**Contato:** sergio.gaiotto@gmail.com | https://falagaiotto.com.br | https://github.com/sergiogaiotto