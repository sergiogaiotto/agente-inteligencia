# Configurações da Plataforma — Registro de Opções

> **Súmula das opções de configuração possíveis** do Maestro, por área.
> Reflete o código em **`APP_VERSION = 10.24.0`**. Fontes: `app/core/config.py`,
> `app/core/database.py` (tabela `platform_settings`), `app/templates/pages/settings.html`,
> `app/templates/pages/federation.html`, `app/llm_routing.py`, `app/core/logging_setup.py`.

## Como a configuração funciona (modelo mental)

- **`platform_settings` (DB) é a fonte da verdade** para o que é configurável pela UI.
  No boot, `apply_settings_to_env()` (`app/core/config.py`) copia o DB → `os.environ`,
  **removendo resíduos do `.env`** das chaves "seladas". Ou seja: para LLM/credenciais,
  **o `.env` é só semente** — o valor efetivo vem do banco (tela de Configurações).
- **Variáveis "seladas"** (`_SEALED_ENV_VARS`, ~40): credenciais e modelos que vêm
  **exclusivamente** da tela de Configurações, nunca do `.env`. Diagnóstico isolado via
  `python -c get_settings()` **não** enxerga o DB → reporta "não configurado" falso.
- **Flags de segurança/avançadas vêm OFF por default** (zero-risco): `federation.enabled`,
  `OPA_ENABLED`, `CSRF_REQUIRED`, `VERIFIER_V2_ENABLED`, `MCP_PER_TOOL_ENABLED`,
  `TEXT_TO_SQL_ENABLED`, `OTEL_ENABLED`.
- **`MAESTRO_SECRET_KEY`** (env) é a master key da cifra Fernet de segredos at-rest e da
  federação. **Obrigatória em produção** — sem ela, cifra cai em fallback inseguro e a
  federação falha fechado.

---

## 1. Federação A2A
Tela **Federação** · escrita via `PUT /api/v1/federation/config` (root). Toda a rede vem **OFF** por default.

| Opção | Onde vive | Valores / default | Efeito |
|---|---|---|---|
| Habilitar federação | `platform_settings: federation.enabled` | `true`/`false` · **false** | Liga/desliga a rede A2A. OFF → todos os endpoints (manifesto, peers, invoke) retornam 404 (instância invisível). Requer `MAESTRO_SECRET_KEY` (fail-closed). |
| Permitir http:// (dev) | `platform_settings: federation.dev_allow_http` | `true`/`false` · **false** | Relaxa a guarda SSRF de egress para aceitar `http://`. Produção **deve** usar `https://`. |
| Workspace (namespace dos URNs) | `platform_settings: federation.workspace` | `[a-z0-9-]+` · **default** | Identidade da instância nos URNs (`urn:maestro:<workspace>:…`). Evita re-federação (ingress rejeita o próprio workspace). |
| Registrar peer | tabela `federation_peers` (`workspace` UNIQUE, `base_url`, `shared_secret` cifrado, `status`) | workspace `[a-z0-9-]+`; base_url https (ou http se dev); segredo 32 bytes | Peer confiável. `base_url` é opcional (necessário só para **consumir/sync**; não para receber invoke). Segredo é gerado, cifrado at-rest e exibido **uma vez** para compartilhar. |
| Rotacionar segredo do peer | `federation_peers: shared_secret → secret_prev` | novo segredo 32 bytes | Troca o segredo mantendo o anterior numa **janela de sobreposição** (ingress aceita ambos). `POST …/peers/{id}/rotate`. |
| Revogar peer | `federation_peers: status='revoked'` | `active` → `revoked` (nunca apaga) | Bloqueia ingress/egress do peer; preserva audit trail. `DELETE …/peers/{id}`. |
| Meu manifesto | `GET /.well-known/maestro-federation.json` (gerado) | read-only | Capabilities expostas: **só `published` + `company` + `kind=pipeline` + não-federada**. Inclui disclosure summary + fingerprint SHA256. 404 se federação OFF. |
| Sincronizar peer (Sync) | `catalog_entries` (`federated=TRUE`, `remote_urn`, `remote_peer_id`) | UPSERT por capability | Puxa o manifesto do peer (SSRF guard) e espelha as capabilities como entries **read-only** locais. |
| Capabilities remotas | `GET /api/v1/federation/remote-entries` | entries `federated=TRUE` | Invocáveis só via `POST /api/v1/federation/remote/{id}/invoke` (envelope HMAC assinado; custo peer-attested clampado). |

---

## 2. Configurações > Plataforma (UI)
Tela **Configurações**. Persistido em `platform_settings`; aplicado no boot via `apply_settings_to_env()`.

### 2.1 Comportamento geral
| Opção | Chave (`platform_settings`) / env | Default | Efeito |
|---|---|---|---|
| Idioma de resposta (global) | `default_response_language` / `DEFAULT_RESPONSE_LANGUAGE` | `pt-BR` | Diretiva de idioma prefixada no system prompt quando o agente não tem override. BCP-47. |
| Timezone da plataforma | `timezone` / `TZ` | `America/Sao_Paulo` | Formatação de datas/horas (UI + servidor) via `time.tzset()`. Armazenamento permanece UTC. |
| Grounded-by-default (anti-alucinação) | `grounding_strict` / `GROUNDING_STRICT` | **true** | Agentes respondem só com evidência (anexo/RAG/tool); sem fundamento → recusa. Override por agente: `allow_general_knowledge=1`. |
| MCP per-tool | `mcp_per_tool_enabled` / `MCP_PER_TOOL_ENABLED` | **false** | Cada tool MCP vira função própria com schema real (vs legado `{operation,query}`). Requer `discovered_tools`. Vale em runtime. **39.0.0**: compõe com o override POR CONECTOR (`Modo per-tool`: Herdar global / Ligado / Desligado, no form de /mcp) — piloto num conector só, ou opt-out pontual, sem mexer na frota. **39.x**: `GET /api/v1/tools/per-tool-coverage` mede a PRONTIDÃO da frota (quantos conectores têm descoberta persistida) — é o gate objetivo para aposentar o caminho legado, e ignora o modo de propósito (medir adoção reportaria 0% com o toggle OFF, que é o default). Devolve também `em_legado_hoje` (adoção) — outra pergunta, outro número. `POST /api/v1/tools/backfill-discovered` (botão "Descobrir pendentes" em /mcp, **root/admin**: dispara egress com credencial decifrada e spawn de processo em toda a frota) descobre em lote, mas **pula `oauth2`/`mTLS`** — esses saem como pendência `motivo=backfill_nao_cobre_auth` e se resolvem pelo "Testar conexão" do próprio conector, que autentica e persiste normalmente. |
| Tier 2 — Text-to-SQL governado | `text_to_sql_enabled` / `TEXT_TO_SQL_ENABLED` | **false** | Liga "Perguntar à Tabela" (IA compila pergunta→consulta, humano cura). Requer catálogo de dados curado. |
| Detalhe da resposta de invoke (API-key) | `api_invoke_default_verbosity` | `full`/`summary`/`minimal` · **summary** | Verbosidade default de `POST /pipelines/{id}/invoke` quando autenticado por **X-API-Key** (integração). `summary` = resposta + narrativa por etapa, sem trace/custo/SQL. Sessão/Workspace (cookie) é sempre `full`. Override por chamada: `?verbosity=` ou `{"verbosity":...}`. |

### 2.2 Modelo primário (fallback global)
| Opção | Chave / env | Default | Efeito |
|---|---|---|---|
| Provider primário | `primary_provider` / `PRIMARY_PROVIDER` | `''` (→ gpt-oss-120b legacy) | Fallback quando o agente não tem `task_type` (routing) nem snapshot próprio. |
| Model primário | `primary_model` / `PRIMARY_MODEL` | `''` | Forma `<provider>/<model>` do fallback global. |

### 2.3 Roteamento LLM por tipo de tarefa
Tela **Roteamento LLM** · `PUT /dashboard/llm-routing`. Chaves `llm_routing.*`. Valor = `provider/model`.

| Task type | Chave | Default |
|---|---|---|
| tool_calling | `llm_routing.tool_calling` | `gpt-oss-120b/openai/gpt-oss-120b` |
| reasoning | `llm_routing.reasoning` | `gpt-oss-120b/openai/gpt-oss-120b` |
| instruct | `llm_routing.instruct` | `gpt-oss-20b/openai/gpt-oss-20b` |
| classification | `llm_routing.classification` | `gpt-oss-20b/openai/gpt-oss-20b` |
| skill_generation | `llm_routing.skill_generation` | segue o Modelo Primário |
| multimodal_fallback | `llm_routing.multimodal_fallback` | `azure/gpt-4o` |
| Mostrar contingência na rastreabilidade | `llm_fallback.show_in_trace` | **true** | Nota visível no painel quando o fallback dispara. Observabilidade/LOGs são **sempre** registrados (auditoria nunca silenciada). |

### 2.4 Provedores de LLM (credenciais **seladas** — só da UI)
| Provedor | Chaves principais | Notas |
|---|---|---|
| Azure OpenAI | `azure_key`, `azure_endpoint`, `azure_api_version` (def. `2024-02-15-preview`), `azure_chat_deployment` (def. `gpt-4o`), `azure_embeddings_deployment` (def. `text-embedding-3-small`) | "openai" é alias de Azure. Deployment ≠ nome do modelo. |
| OpenAI Público | `openai_public_api_key`, `openai_public_base_url` (def. `https://api.openai.com/v1`), `openai_public_model` (def. `gpt-4o`) | Separado do alias Azure. Aceita proxy OpenAI-compatible. |
| Maritaca AI | `maritaca_key`, `maritaca_model` (def. `sabia-3`), `maritaca_url` | Sabiá-3/4. |
| Ollama | `ollama_url`, `ollama_model` | Sem API key. Endpoint OpenAI-compatible (`/v1`). |
| GPT-OSS-120B | `oss120b_url`, `oss120b_model` (def. `openai/gpt-oss-120b`), `oss120b_api_key` (def. `not-needed`) | Endpoint open-weight; URL termina em `/v1`. |
| GPT-OSS-20B | `oss20b_url`, `oss20b_model` (def. `openai/gpt-oss-20b`), `oss20b_api_key` (def. `not-needed`) | idem. |
| Global | `llm_timeout_seconds` (30–900, def. **300**) | Timeout de chamadas LLM (todos os providers). |

### 2.5 Embeddings (RAG)
| Opção | Chave / env | Default | Efeito |
|---|---|---|---|
| Embedding provider | `embedding_provider` / `EMBEDDING_PROVIDER` | `qwen3` | `qwen3` (open-weight) ou `azure`. Coleção pgvector `agente_evidence`. |
| Qwen3 — source | `qwen3_source` | `oss120b` | Reusa scheme://host do endpoint OSS escolhido (`oss120b`/`oss20b`). |
| Qwen3 — path | `qwen3_path` | `embed06b/v1` | Path relativo (reusa OSS) ou URL absoluta. Sem `/embeddings`. |
| Qwen3 — model | `qwen3_model` | `Qwen/Qwen3-Embedding-0.6B` | Modelo no endpoint. |
| Qwen3 — dimensões (Matryoshka) | `qwen3_dimensions` | `0` (=1024) | ⚠ Mudar **exige re-embed** da coleção; sem reindex, busca semântica para. |

### 2.6 Observabilidade SaaS (opcional)
| Opção | Chave | Default |
|---|---|---|
| LangFuse public/secret/host | `langfuse_public`, `langfuse_secret`, `langfuse_host` | `''` / `''` / `https://cloud.langfuse.com` |

---

## 3. Segurança, Observabilidade e Operação (env / `app/core/config.py`)
Configurado por ambiente (alguns também na UI). Defaults pensados para zero-risco em dev.

### 3.1 Segurança de borda
| Variável | Default | Efeito |
|---|---|---|
| `MAESTRO_SECRET_KEY` | `''` (fallback inseguro) | Master key Fernet de segredos at-rest + federação. **Obrigatória em prod.** |
| `RATE_LIMIT_ENABLED` | true | Rate-limit sliding window (Redis, fallback memória). Buckets: `workspace`/`auth`/`api`. |
| `RATE_LIMIT_WINDOW_SECONDS` | 60 | Janela de contagem. |
| `RATE_LIMIT_DEFAULT_PER_MIN` / `_WORKSPACE_PER_MIN` / `_AUTH_PER_MIN` | 60 / 20 / 10 | Limites por bucket (api / workspace-pesado / login anti-brute-force). |
| `INTERACTION_MAX_TOKENS` | 80000 | Cap de tokens por interação (anti runaway, LLM04). |
| `CSRF_REQUIRED` | **false** | Validação de CSRF token (ligar quando a UI mandar `X-CSRF-Token`). |
| `COOKIE_SECURE` | false | Flag Secure (obrigatório true em prod HTTPS). |
| `COOKIE_SAMESITE` | `lax` | `lax`/`strict`/`none`. |
| `SESSION_MAX_AGE_SECONDS` | 604800 (7d) | Duração da sessão. |

### 3.2 Privacidade / guardas de LLM (OWASP)
| Variável | Default | Efeito |
|---|---|---|
| `DLP_ENABLED` | true | Redação de PII (CPF/email/telefone) ao persistir. |
| `DLP_REDACT_BEFORE_LLM` | false | Se true, redige também no prompt (perde contexto de IDs reais). |
| `PROMPT_GUARD_ENABLED` | true | Detecção de prompt injection (LLM01). |
| `PROMPT_GUARD_BLOCK_THRESHOLD` / `_WARN_THRESHOLD` | 0.7 / 0.4 | Score de bloqueio / warning. |
| `PROMPT_LEAK_GUARD_ENABLED` | true | Mostra hash+preview do system_prompt nas traces. |
| `PROMPT_LEAK_PREVIEW_CHARS` | 60 | Tamanho do preview. |

### 3.3 Política (OPA)
| Variável | Default | Efeito |
|---|---|---|
| `OPA_ENABLED` | **false** | Liga o Policy Engine (Rego). |
| `OPA_URL` | `http://opa:8181` | Endpoint OPA. |
| `OPA_FAILSAFE_OPEN` | true (dev) | Comportamento offline: true=allow+warn; **false (recomendado em prod)**=nega. |
| `OPA_TIMEOUT_SECONDS` | 2.0 | Timeout das chamadas OPA. |

### 3.4 Verifier v2 (juiz multidimensional) e harness

> **Desde 25.1.0 estas opções (exceto `VERIFIER_JUDGE_MODEL`) são editáveis na
> UI em Configurações → Parâmetros (root/admin), com efeito em runtime sem
> restart.** O valor salvo na UI passa a vencer o `.env`; enquanto não salvo,
> o `.env` continua valendo como default. O modelo do juiz é o papel `judge`
> do Roteamento LLM (card "LLM como Juiz").

| Variável | Default | Efeito |
|---|---|---|
| `VERIFIER_V2_ENABLED` | **false** | Juiz 4D (factuality/completeness/tone/safety) + ContractValidator. |
| `VERIFIER_SIGNALS_DRIVE_FSM` | **false** | #684 (Fatia F). Ligado: recusa redigida pelo agente (dado de terceiro, prompt-injection, política) vira estado **Refuse** e escalonamento (NOC/gerência/supervisão) vira **Escalate**, em vez de ficarem invisíveis em `Recommend`. Desligado (padrão): mapeamento de estado inalterado. Detecção conservadora por padrões no rascunho final. |
| `VERIFIER_JUDGE_MODEL` | `azure/gpt-4o` | Modelo do juiz (idealmente provider ≠ do gerador). **Desde 24.8.0**: o card "LLM como Juiz" em Configurações → Roteamento LLM (papel `judge`) tem PRECEDÊNCIA sobre esta env; ela vale só como default quando nenhuma rota foi salva na UI. |
| `VERIFIER_*_THRESHOLD` (factuality/completeness/tone) | 3.0 (0–5) | Score mínimo para aprovação. |
| `VERIFIER_MAX_TOKENS` | 800 | Cap da resposta do juiz. |
| `VERIFIER_CONTRACT_RETRY_ENABLED` / `_MAX_TOKENS` | true / 2000 | Re-chama LLM 1× para corrigir contrato. |
| `VERIFIER_PRODUCTION_ASYNC` / `_SAMPLE_RATE` / `_MAX_CONCURRENT_JOBS` | false / 0.10 / 20 | Julga em background sample% das interações (não bloqueia resposta). |
| `HARNESS_USE_VERIFIER` | true | Harness re-julga casos via Verifier (gate combinado). |
| `HARNESS_MIN_*` / `MAX_*` | vários | Thresholds multidimensionais do gate de qualidade. |
| `HARNESS_PHRASES_GATE` | **false** | Em runs de PIPELINE, Frase-Prova de roteamento reprovada reprova o gate do run. OFF = reprovações viram nota informativa no `gate_reason`. As frases (seladas nas arestas condicionais) provam a REGRA de roteamento — avaliação determinística, sem custo LLM. |
| `HARNESS_ASYNC_ENABLED` | **false** | Harness como JOB durável (43.0.0): `POST /eval-runs/execute` → 202 + `eval_id`; a linha de `eval_runs` é o próprio job (`queued`→`running`→terminal), executado fora do request; polling em `GET /eval-runs/{id}`. OFF = caminho síncrono de sempre; também congela o despacho da fila (kill-switch). |
| `HARNESS_JOBS_MAX_CONCURRENT` | 1 | Runs de harness simultâneos no processo (cap próprio — um run já serializa N casos de LLM). |
| `HARNESS_JOB_TIMEOUT_MINUTES` | 60 | Deadline por run assíncrono; estouro cancela e marca `timeout` (custos por caso já registrados sobrevivem). |
| `HARNESS_BUDGET_USD_PER_RUN` | 0 (sem teto) | Teto de custo LLM por run (invoke + juiz + RAGAS), checado ENTRE casos. Estouro = aborto gracioso: status `budget_exceeded`, métricas PARCIAIS com aviso, gate `skipped`, sem drift events. |
| `HARNESS_SYNTHETIC_RETENTION_DAYS` | 0 (desligado) | Retenção própria das interações SINTÉTICAS do harness (`interactions.origin='harness'`): purga na carona do reaper, mesmo caminho da retenção LGPD (scrub preserva a linha analítica das verifications). |

### 3.5 Harness multi-agente (DeepAgent)
| Variável | Default | Efeito |
|---|---|---|
| `DEEPAGENT_ENABLED` | true | Encadeamento multi-agente (`next_agent`). |
| `DEEPAGENT_MAX_ITERATIONS` | 25 | Cap de iterações (anti-loop). |
| `DEEPAGENT_TIMEOUT` | 120 | Timeout do encadeamento (s). |

### 3.6 RAG v2 (busca híbrida)
| Variável | Default | Efeito |
|---|---|---|
| `RAG_V2_ENABLED` | true | Busca híbrida BM25 + vetorial (pgvector). |
| `RAG_CHUNK_SIZE_TOKENS` / `_OVERLAP_TOKENS` | 500 / 50 | Chunking (mudar exige re-embed). |
| `RAG_TOP_N_VECTOR` / `_BM25` | 20 / 20 | Top-N por perna antes do RRF. |
| `RAG_RRF_K` | 60 | Constante do Reciprocal Rank Fusion. |
| `RAG_RERANK_WITH_LLM` | true | Rerank pós-RRF por LLM (+latência/custo, +qualidade). |

### 3.7 Logging e observabilidade (self-hosted)
| Variável | Default | Efeito |
|---|---|---|
| `LOG_DIR` | `logs` | Pasta dos 5 JSONL (app/tabular/api/audit/errors), rotação diária. |
| `LOG_LEVEL` | `INFO` | Nível mínimo. |
| `LOG_FORMAT` | `json` (prod) / `text` (test) | Formato de saída. |
| `LOG_FILE_ENABLED` / `LOG_CONSOLE_ENABLED` | 1 / 1 | Liga handlers de arquivo / console. |
| `OTEL_ENABLED` | **false** | OpenTelemetry (traces→Tempo, métricas→Prometheus, Grafana). `docker compose --profile full`. |
| `OTEL_SERVICE_NAME` / `_VERSION` | `agente-inteligencia` / `2.0.0` | Identidade nos traces. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://tempo:4317` | OTLP gRPC. |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | Sampling (prod: `parentbased_traceidratio` + `_ARG=0.1`). |
| `LOKI_ENDPOINT` | `http://loki:3100` | Reservado p/ push nativo (hoje via Promtail). |

### 3.8 Versão
| Item | Onde | Efeito |
|---|---|---|
| `APP_VERSION` | `app/core/version.py` (SSOT) | Versão no rodapé. Bump **manual** por PR: nova func→MAJOR, melhoria→MEDIUM, fix→MINOR. Atual: **10.24.0**. |

---

> Este documento é um **registro de referência**, gerado a partir do código em 10.24.0.
> Ao adicionar/alterar uma opção de configuração, atualize a seção correspondente aqui.
