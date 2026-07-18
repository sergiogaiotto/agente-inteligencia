"""Configuração central da aplicação."""

import logging

from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # ── App ──
    app_name: str = "Maestro"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    secret_key: str = "change-me"

    # ── Database (PostgreSQL) ──
    # Formato: postgresql://user:password@host:port/database
    # Em docker-compose: postgresql://agente:agente@postgres:5432/agente_inteligencia
    database_url: str = "postgresql://agente:agente@localhost:5432/agente_inteligencia"
    # Pool asyncpg. Defaults endurecidos p/ carga concorrente de API externa:
    # com max=10 (antigo) invokes concorrentes + tasks async saturavam o pool e
    # novas requests ESPERAVAM por conexão (hang). min/max e o command_timeout
    # (teto por query — evita conexão presa indefinidamente) são env-tunáveis.
    database_pool_min: int = 5
    database_pool_max: int = 20
    database_command_timeout: int = 60
    # Migrações idempotentes fail-fast (33.5.0): True → o boot ABORTA se QUALQUER
    # migração falhar (crash-loop até corrigir) em vez do fail-open atual (WARNING
    # + segue). Default False = comportamento atual (retrocompat). Env: DATABASE_MIGRATIONS_STRICT.
    database_migrations_strict: bool = False

    # ── Cache / Redis (memória de contexto) ──
    redis_url: str = "redis://localhost:6379/0"

    # ── Vector DB ──
    # Onda Q (2026-05-30): backend único pgvector (Postgres com pgvector
    # extension). Qdrant removido. Settings qdrant_url/api_key/collection
    # + rag_vector_backend removidas — não há mais escolha de backend.

    # ── LLM provider primário ──
    # azure | openai | maritaca | ollama
    default_llm_provider: str = "azure"

    # ── Azure OpenAI (provedor principal) ──
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-02-15-preview"
    azure_openai_chat_deployment: str = "gpt-4o"
    azure_openai_embeddings_deployment: str = "text-embedding-3-small"

    # ── OpenAI (fallback) ──
    # Onda 7 Wave 5: OPENAI_API_KEY pública foi virada alias de Azure (provider
    # "openai" resolve pra Azure). Mantido como retrocompat de agentes legacy.
    #
    # MUDANÇA 2026-05-29 PR #194 (user pediu): OpenAI público REAL reintroduzido
    # como provider separado `openai_public`. Não substitui o alias antigo —
    # convive. Usado quando operador quer chamar api.openai.com diretamente
    # (ex: rotear skill_generation pro gpt-4o público em vez do Azure pra
    # comparar latência/custo).
    openai_public_api_key: str = ""
    openai_public_base_url: str = "https://api.openai.com/v1"
    openai_public_model: str = "gpt-4o"

    # ── Maritaca AI ──
    maritaca_api_key: str = ""
    maritaca_api_url: str = "https://chat.maritaca.ai/api"
    maritaca_model: str = "sabia-3"

    # ── Ollama (local) ──
    ollama_api_url: str = "http://187.77.46.137:11434"
    ollama_api_key: str = ""
    ollama_model: str = "hf.co/Althayr/Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest"

    # ── GPT-OSS (open-weight via endpoint OpenAI-compatible) ──
    # 2 modelos com URL/key próprias — provedor interno serve cada um em
    # endpoint dedicado. "not-needed" como api_key é válido (proxy autentica
    # de outra forma, ex: rede interna).
    oss120b_url: str = ""
    oss120b_model: str = "openai/gpt-oss-120b"
    oss120b_api_key: str = ""
    oss20b_url: str = ""
    oss20b_model: str = "openai/gpt-oss-20b"
    oss20b_api_key: str = ""
    llm_timeout_seconds: int = 300

    # ── Modelo Primário (fallback global) ──
    # Usado quando agent não tem task_type (Roteamento LLM da Onda 7) NEM
    # snapshot próprio de llm_provider/model. Quando definido, substitui o
    # default histórico — agents legacy sem primary caem em gpt-oss-120b.
    primary_provider: str = ""  # ex: "gpt-oss-120b" | "azure" | "maritaca" | "ollama"
    primary_model: str = ""     # ex: "openai/gpt-oss-120b" | "gpt-4o" | "sabia-4"

    # ── Idioma de resposta (fallback global) ──
    # Aplicado quando agent.response_language está vazio. Engine prepende
    # instrução explícita no system_prompt — LLM responde no idioma escolhido
    # mesmo quando contexto/evidências estão em outros idiomas (caso típico:
    # busca Tavily retorna inglês, mas resposta sai em pt-BR).
    # Formato: tag IETF BCP-47 ("pt-BR", "en-US", "es-ES"). UI mostra label
    # humano via _LANGUAGE_LABELS em llm_providers (mapeamento futuro).
    default_response_language: str = "pt-BR"

    # ── CORS (P0 — frontends externos no browser) ──
    # Allowlist de origens (CSV) autorizadas a consumir a API cross-origin. VAZIO
    # = CORS OFF (comportamento atual; browsers cross-origin bloqueados). Lido
    # DINAMICAMENTE pelo middleware → mudar na UI vale sem restart. NUNCA usar '*'
    # (a app autentica por cookie de sessão; refletir origem arbitrária com
    # credenciais = CSRF-via-CORS). Ex.: "https://app.cliente.com,https://x.io".
    cors_allowed_origins: str = ""
    # ── Contenção de privilégio da API Key (P0) ──
    # Quando True, um principal autenticado por X-API-Key/Bearer só alcança a
    # SUPERFÍCIE PÚBLICA (invoke + descoberta) — todo o resto 403. Default OFF
    # (comportamento atual). O bloqueio das rotas de ESCALAÇÃO/segredo (criar/gerir
    # api-keys, settings, users) é SEMPRE aplicado, independente deste toggle.
    api_key_public_surface_only: bool = False
    # Quando True, um principal-via-API-Key só invoca pipelines PUBLICADOS (o
    # contrato SELADO); rascunho/aposentado → 403. Sessão de UI (cookie) continua
    # invocando rascunhos p/ testar. Default OFF (comportamento atual).
    api_key_invoke_published_only: bool = False
    # ── Quota de custo por API Key (F6) ──
    # Quando True, cada invoke via X-API-Key/Bearer é DEBITADO no ledger de custo
    # da key (soma do cost_usd REAL dos steps) e, se a key tiver orçamento definido
    # (api_keys.cost_budget_usd), novos invokes são BLOQUEADOS com 402 assim que o
    # gasto da janela corrente (dia/mês/acumulado) atinge o teto. Default OFF
    # (comportamento atual: sem débito, sem bloqueio). Keys SEM orçamento nunca são
    # bloqueadas — só têm o gasto registrado (observabilidade) quando o toggle ON.
    # NB: gpt-oss custa 0 (só Azure/OpenAI têm cost_usd>0) — o débito reflete isso.
    api_key_cost_budget_enabled: bool = False

    # ── Embedding provider (Qwen3 | Azure) ──
    # Default: Qwen3 (open-weight via hub interno). Reusa URL/key do OSS source
    # escolhido (oss20b ou oss120b), só muda o path. Endpoint efetivo:
    # <scheme>://<host_do_OSS>/<qwen3_path>  →  ex: https://hub-gpus.claro.com.br/embed06b/v1
    embedding_provider: str = "qwen3"  # 'qwen3' | 'azure'
    qwen3_source: str = "oss120b"      # 'oss120b' | 'oss20b'
    qwen3_path: str = "embed06b/v1"
    qwen3_model: str = "Qwen/Qwen3-Embedding-0.6B"
    # Densidade do vetor (Matryoshka): truncamento server-side da dim do output.
    # 0 = não envia o parâmetro (usa default do modelo: 1024 para Qwen3-Embedding-0.6B).
    # Mudar exige re-embedar a collection do Qdrant — a dim precisa bater entre
    # write e read. Trocar em produção sem plano quebra a busca.
    qwen3_dimensions: int = 0

    # ── Observabilidade ──
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ── DeepAgent Harness ──
    deepagent_enabled: bool = True
    deepagent_max_iterations: int = 25
    deepagent_timeout: int = 120

    # ── Rate-limit (Onda 1) ──
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    # Limites por janela. *_per_min é interpretado como "por janela".
    # 60 era baixo demais: TODA leitura da UI (lista/detalhe/topologia) cai neste
    # balde e cada página dispara vários fetches (o Fluxo de agentes faz ~7:
    # topology+layout+groups+conditional-vars+pipelines+llm-health…). Navegar
    # rápido por ~10 menus estourava os 60/60s → 429 → a tela ficava EM BRANCO
    # (o fetch de dados falhava e o front engolia o erro). Leituras são baratas;
    # a proteção anti-DoS real está nos baldes de LLM (workspace) e login, que
    # seguem apertados. 300/60s = 5 req/s por usuário: absorve navegação humana
    # intensa e ainda limita abuso automatizado.
    rate_limit_default_per_min: int = 300      # rotas API genéricas (leituras baratas)
    rate_limit_workspace_per_min: int = 20     # rotas que disparam LLM
    rate_limit_auth_per_min: int = 10          # /login (anti-brute-force)
    # Cap de tokens por interação — proteção LLM04 contra runaway loops
    interaction_max_tokens: int = 80000
    # Tamanho máximo de upload (MB) — anti-DoS de memória/disco (CWE-400).
    # O handler lê em chunks e aborta com 413 ao exceder.
    max_upload_mb: int = 25
    # Teto GLOBAL do corpo de requisição (MB) — anti-DoS de memória (API-6).
    # Rejeita com 413 por Content-Length ANTES de ler/parsear o corpo (sem esse
    # teto, um POST de corpo ilimitado no /invoke era buferizado inteiro → OOM).
    # Precisa acomodar o MAIOR corpo legítimo: anexos inline no /invoke =
    # 5 × 10MB raw ≈ 67MB base64 (ver _MAX_ATTACHMENT_BYTES em routes/agents.py)
    # e uploads multipart (max_upload_mb). Default 100MB dá margem; baixe se a
    # instalação não usa anexos grandes.
    max_request_body_mb: int = 100

    # ── Auth hardening (Onda 1) ──
    # bcrypt sempre ativo; SHA256 legado validado e migrado no próximo login.
    # CSRF default OFF para não quebrar frontend antes de adaptado — ligar
    # quando o JS adicionar `X-CSRF-Token` em todos POST/PUT/DELETE.
    csrf_required: bool = False
    cookie_secure: bool = False                # True em produção HTTPS
    cookie_samesite: str = "lax"               # "lax" | "strict" | "none"
    session_max_age_seconds: int = 7 * 24 * 3600
    # ── Proxy confiável p/ X-Forwarded-For (SEC-05) ──
    # CSV de IPs/CIDRs dos reverse proxies legítimos (ex.: Caddy/Traefik). A
    # resolução do IP do cliente (rate-limit) só CONFIA no header X-Forwarded-For
    # quando o peer DIRETO está nesta lista — senão usa o IP do peer. Impede que
    # um cliente forje XFF e ganhe um balde de rate-limit novo por IP (bypass de
    # brute-force/DoS). VAZIO = default seguro: confia em ranges privados/loopback
    # (o caso reverse-proxy típico em rede Docker). Defina p/ restringir a IPs exatos.
    trusted_proxies: str = ""

    # ── DLP / PII redaction (Onda 1) ──
    dlp_enabled: bool = True
    # Se True, aplica redaction também ANTES de enviar prompt ao LLM (perde
    # contexto de identificadores reais). Default False — só redacta na
    # persistência (cumpre LLM06 sem prejudicar a UX).
    dlp_redact_before_llm: bool = False

    # ── Prompt injection guard (Onda 1, LLM01) ──
    prompt_guard_enabled: bool = True
    # Score 0..1: bloqueia interação inteira se >= block_threshold
    prompt_guard_block_threshold: float = 0.7
    # Score 0..1: registra warning em audit_log mas deixa passar
    prompt_guard_warn_threshold: float = 0.4

    # ── Prompt leak guard (Onda 1, LLM10) ──
    # Em traces de retorno, mostra apenas hash + preview do system_prompt em vez
    # do texto cru. Admin pode obter o original via rota dedicada (futuro).
    prompt_leak_guard_enabled: bool = True
    prompt_leak_preview_chars: int = 60

    # ── Grounded-by-default (2026-06-06) ──
    # Princípio global: o conhecimento paramétrico do modelo NUNCA é usado para
    # compor respostas — o agente responde SÓ com base em evidências (anexos,
    # RAG, resultados de tools). Quando True (default), o engine (1) injeta uma
    # diretiva estrita de grounding no system prompt e (2) faz o VerifyEvidence
    # RECUSAR respostas sem nenhuma evidência (anexo/RAG/tool/pipeline). Escape
    # hatch por agente: allow_general_knowledge=1 (ex: brainstorming). Override
    # global via env GROUNDING_STRICT=false ou Settings UI. Ver engine.py
    # (_build_grounding_directive + _grounding_guard).
    grounding_strict: bool = True

    # ── Verifier v2 (judge multi-dimensional + ContractValidator) ──
    # Promove EvidenceChecker (Onda 0) a 1ª classe, separando RAG de Verification.
    # OFF por default → comportamento legacy preservado (_LegacyVerifier roda no lugar).
    verifier_v2_enabled: bool = False
    # #684 (Fatia F): quando True, o Verifier emite sinais de decisão a partir do
    # rascunho — `policy_refusal` (o agente RECUSOU: dado de terceiro, injection,
    # política) e `needs_escalation` (o agente ESCALOU: NOC/gerência/supervisão) —
    # e a FSM transiciona para Refuse/Escalate em vez de deixar a recusa/escala
    # "invisível" em Recommend. OFF por default → comportamento de PRODUÇÃO
    # inalterado (os sinais ficam False; ligar só muda o mapeamento de estado).
    verifier_signals_drive_fsm: bool = False
    # Modelo do juiz. Anti-self-preference: idealmente um provider ≠ do gerador.
    # Formato "<provider>/<model>" ou apenas <model> (assume azure).
    verifier_judge_model: str = "azure/gpt-4o"
    # Thresholds de aprovação por dimensão (escala 0-5). 3.0 = 60% proficiência.
    verifier_factuality_threshold: float = 3.0
    verifier_completeness_threshold: float = 3.0
    verifier_tone_threshold: float = 3.0
    # Cap de tokens da resposta do juiz. ~600 cobre 4 dimensões + claims sem cortar.
    verifier_max_tokens: int = 800

    # ── Contract retry on failure (Wave atual) ──
    # Quando ContractValidator marca compliant=false, Verifier re-chama o LLM
    # 1x com instrução de correção (incluindo os erros específicos). Custo:
    # 1 chamada LLM extra na falha. Ganho: muitas violações de formato são
    # triviais (vírgula sobrando, chave faltando) — o retry corrige sem
    # operador intervir. Default ON (qualidade > custo); desligue em casos
    # extremos de orçamento apertado.
    verifier_contract_retry_enabled: bool = True
    # Cap de tokens da resposta do retry. Maior que o do judge (800) porque
    # aqui o LLM regenera o draft completo, não só uma avaliação.
    verifier_contract_retry_max_tokens: int = 2000

    # ── Verifier production mode (async sampling) ──
    # Quando True E verifier_v2_enabled True, o branch verifier do engine não
    # bloqueia mais a resposta: sample_rate% das interações são julgadas em
    # background. Resposta segue com heurística rasa (evidence_score). Útil em
    # produção — 100% sync é caro (1 LLM call extra) e lento (+2-4s).
    # Defaults conservadores: OFF até ligar explicitamente.
    verifier_production_async: bool = False
    verifier_production_sample_rate: float = 0.10  # 10% das interações
    verifier_max_concurrent_jobs: int = 20  # backpressure: drop acima disso
    # Fila de juiz DURÁVEL (Onda 6): nº máx. de tentativas de um verifier_job
    # antes de virar 'dead' (dead-letter). O boot-resume re-despacha os pending
    # até este teto. Env: VERIFIER_JOB_MAX_ATTEMPTS.
    verifier_job_max_attempts: int = 3

    # ── Harness multi-dim gate (§9.5 + §14.2) ──
    # Quando True, run_evaluation re-julga cada caso via Verifier (profile=rigorous)
    # e gate combina accuracy/refusal/FP com avg_factuality/safety/contract.
    # Toggle off → harness opera no modo legacy (proxy raso por shape).
    harness_use_verifier: bool = True
    harness_min_accuracy: float = 0.80
    harness_min_avg_factuality: float = 3.5
    harness_min_avg_completeness: float = 3.0
    harness_min_avg_tone: float = 3.0
    harness_max_safety_violation_rate: float = 0.05
    harness_min_contract_compliance: float = 0.95
    harness_max_hallucination_rate: float = 0.10
    harness_max_dim_regression_pct: float = 5.0
    # Pacote C3 (33.20.0): regressão de ACURÁCIA vs baseline — era o último
    # gate hardcoded (GATE_THRESHOLDS) enquanto os irmãos já eram settings.
    harness_max_regression_pct: float = 5.0
    # test_phrases → harness (36.5.0): quando True, Frase-Prova reprovada em
    # run de PIPELINE reprova o gate do run. Default OFF: informativo apenas
    # (nota no gate_reason) — as frases provam a REGRA de roteamento
    # (determinístico), não o comportamento do LLM.
    harness_phrases_gate: bool = False

    # ── Harness assíncrono + custo no ledger (43.0.0, PR2 do arco Otimização) ──
    # Job durável do harness: POST /eval-runs/execute → 202 + eval_run 'queued'
    # (a linha de eval_runs É o job: claim atômico, execução fora do request,
    # polling em GET /eval-runs/{id}). Default OFF (superfície nova, gated):
    # OFF mantém o caminho síncrono EXATO de hoje e congela o despacho da fila
    # (kill-switch — higiene de boot continua rodando).
    harness_async_enabled: bool = False
    # Runs de harness simultâneos neste processo (cap PRÓPRIO, não compartilha
    # o do invoke): um run já serializa N casos de LLM; 1 é o default seguro
    # para o provider/circuit-breaker.
    harness_jobs_max_concurrent: int = 1
    # Deadline por run assíncrono (minutos): run pendurado é cancelado e vira
    # 'timeout' (os custos por caso já registrados sobrevivem — são off-path).
    harness_job_timeout_minutes: int = 60
    # Teto de custo LLM por run (US$): checado ENTRE casos (mid-run), somando
    # invoke + juiz + RAGAS. Estourou → aborto gracioso: status
    # 'budget_exceeded', métricas PARCIAIS persistidas com aviso e gate
    # 'skipped' (convenção sem-falsa-confiança). 0 = sem teto (default).
    harness_budget_usd_per_run: float = 0.0
    # Retenção das interações SINTÉTICAS do harness (interactions.origin=
    # 'harness'), em DIAS: purga na carona do reaper (mesmo caminho da
    # retenção LGPD — scrub das verifications preserva a linha analítica).
    # 0 = desligado (default). Independe de interactions_retention_days.
    harness_synthetic_retention_days: int = 0

    # ── Loop reflexivo do otimizador (49.0.0, PR4b — fecha o arco) ──
    # Habilita POST /optimizer/optimize → 202 + job durável do loop GEPA.
    # Default OFF (dispara MUITOS runs de LLM; gated como toda superfície
    # nova). OFF também congela o despacho da fila (kill-switch).
    optimizer_loop_enabled: bool = False
    # Rodadas máximas do loop (cada rodada: propor filhos → avaliar no treino).
    optimizer_max_rounds: int = 4
    # Paciência do early-stop: para após N rodadas sem MELHORA do melhor score
    # (o tail de otimização overfitta — resultado negativo replicado no plano).
    optimizer_patience: int = 2
    # Teto de custo LLM por loop (US$); 0 = sem teto. Checado entre variantes.
    optimizer_default_budget_usd: float = 0.0
    # Deadline de parede por loop (minutos): o worker cancela no estouro.
    optimizer_job_timeout_minutes: int = 120
    # Loops simultâneos no processo (cap próprio — cada loop já serializa
    # muitos runs de LLM; 1 é o default seguro).
    optimizer_jobs_max_concurrent: int = 1

    # ── RAGAS com gabarito (ground truth) — 33.12.0 ──
    # context_recall + answer_correctness exigem a resposta-padrão do gold E uma
    # chamada LLM-judge extra POR MÉTRICA (custo). Gated default-OFF: quando
    # False, o RAGAS fica só nas 4 heurísticas sem gabarito (zero LLM extra).
    # Comportamento; NÃO-selado (o .env vale como fallback). Env:
    # RAGAS_GROUND_TRUTH_ENABLED. Lido em run_evaluation (harness) — único lugar
    # com gold garantido (produção não tem gabarito).
    ragas_ground_truth_enabled: bool = False

    # ── Tuning de performance do invoke (25.2.0) ──
    # Cache de topologia/schema por processo/requisição: elimina a amplificação
    # de queries do execute_pipeline (~250 round-trips → dezenas). Correção-
    # neutro (só evita re-consultar dado imutável). Default ON; toggle na aba
    # Parâmetros como válvula de rollback.
    query_topology_cache_enabled: bool = True
    # Roteamento rápido (26.0.0): MASTER global. Habilita pular a chamada LLM do
    # agente ENTRY (router) de um pipeline quando as arestas de saída são 100%
    # determinísticas (só args selados + pergunta, nunca o output do router).
    # Sozinho não muda nada — cada pipeline ainda precisa optar (coluna
    # pipelines.fast_routing). Default OFF (mudança de comportamento gated).
    fast_routing_enabled: bool = False

    # ── Invoke assíncrono 202 (Onda 6, 34.0.0) ──
    # Habilita POST /pipelines/{id}/invoke/async → 202 + job durável (invoke_jobs)
    # + polling em GET /pipelines/{id}/jobs/{job_id}. Default OFF (superfície de
    # contrato NOVA, gated como toda mudança de comportamento); quando OFF a rota
    # responde 403 invoke_async_disabled. O caminho síncrono NUNCA muda.
    invoke_async_enabled: bool = False
    # Retenção de jobs TERMINAIS (completed/failed/lost) — o reaper apaga linhas
    # mais velhas que isto (horas). Jobs queued/running nunca são apagados.
    invoke_jobs_retention_hours: int = 72
    # Execuções de invoke simultâneas neste processo. Excedente fica 'queued' e
    # o reaper despacha quando abrir vaga (invoke é caro — cap baixo protege
    # pool/LLM; o gate de orçamento por key continua valendo por job).
    invoke_jobs_max_concurrent: int = 4
    # Deadline por job (35.4.0): execute_pipeline cancelado no estouro — um job
    # pendurado não ocupa vaga do cap p/ sempre (o reaper não mata task viva).
    invoke_job_timeout_minutes: int = 30

    # ── Retenção de conversas / LGPD (35.8.0, arco LGPD-1) ──
    # Purga por IDADE de interactions antigas (cascade turns/tool_calls/binding)
    # + scrub do TEXTO das verifications órfãs (preserva a linha analítica do
    # juiz p/ /quality e drift) + varre invoke_jobs/api_call_logs/verifier_jobs
    # pelos mesmos ids. invocation_costs/ledgers FICAM (FinOps, só números).
    # 0 = DESLIGADO (default — flags OFF-by-default; o dono liga com a política).
    interactions_retention_days: int = 0

    # ── Câmbio USD→BRL do Cockpit/TCO (35.12.0, FIN do roadmap) ──
    # O default do campo "Câmbio" do Cockpit (o operador ainda pode sobrepor na
    # tela por simulação). Era 5.30 HARDCODED no template — agora o operador
    # pina a taxa da política de custos da empresa em runtime.
    fx_usd_brl: float = 5.30

    # ── Circuit-breaker do egress LLM (cross-worker via Redis) — 33.1.0 ──
    # Contém o raio de um provider caído: após N falhas de ALCANCE consecutivas
    # (rede/timeout/URL ausente — via is_llm_unreachable), o circuito ABRE e as
    # chamadas seguintes são curto-circuitadas (não pagam o timeout de ~120-300s
    # que exauria o pool 5/20). Passado o cooldown, sondas half-open testam a
    # recuperação. Estado compartilhado entre workers via redis_url (fallback
    # in-process por-worker quando o Redis cai). Comportamento; NÃO-selado
    # (o .env vale como fallback). Ver app/core/llm_breaker.py.
    circuit_breaker_enabled: bool = True    # master; False = passthrough total
    cb_failure_threshold: int = 5           # falhas de alcance p/ abrir (fleet-total no Redis)
    cb_cooldown_seconds: int = 30           # tempo aberto antes do half-open
    cb_half_open_max_probes: int = 1        # sondas concorrentes em half-open

    # Esforço de raciocínio das GERAÇÕES do Wizard (SKILL.md + agente). O gate por
    # MODELO vive em get_provider: 'high' só CHEGA ao modelo que aceita (gpt-oss
    # sempre; Azure/OpenAI só o1/o3/o4/gpt-5 — gpt-4o/gpt-4.1 descartam sem erro,
    # sem 400). 'high'|'medium'|'low' ou '' (desligado). Default 'high' = o
    # comportamento anterior (constante hardcoded no wizard). Editável na aba
    # Parâmetros, runtime sem restart.
    wizard_reasoning_effort: str = "high"

    # ── Policy Engine (Onda 4a — OPA Policy as Code) ──
    # Quando True, decisões sensíveis (PolicyCheck, tool invocation) consultam o
    # OPA em opa_url e seguem a decisão. Auditoria via audit_log.
    # Default OFF: comportamento idêntico ao de hoje, zero risco.
    opa_enabled: bool = False
    opa_url: str = "http://opa:8181"
    # Failsafe-open: se OPA offline, allow=true com warning + audit. Default em dev.
    # Trocar para False em produção com dados sensíveis (failsafe-closed = nega na falha).
    opa_failsafe_open: bool = True
    # Timeout curto: OPA local é ~1ms. Acima disso é problema, e app não pode esperar.
    opa_timeout_seconds: float = 2.0

    # ── RAG v2 (Onda 3 — Qdrant + embeddings reais) ──
    # Toggle global. Quando False, retriever cai no fallback antigo (busca textual
    # em metadados de knowledge_sources). Quando True E há chunks ingeridos, usa
    # busca híbrida BM25 (Postgres tsvector) + vetorial (Qdrant), fundidos via RRF.
    rag_v2_enabled: bool = True
    # Tokens por chunk e overlap entre chunks consecutivos. 500/50 é sweet spot
    # para text-embedding-3-small. Aumentar exige mais contexto/custo no LLM final.
    rag_chunk_size_tokens: int = 500
    rag_chunk_overlap_tokens: int = 50
    # Top-N de cada perna antes da fusão. RRF então reduz para top_n do retriever (default 5).
    rag_top_n_vector: int = 20
    rag_top_n_bm25: int = 20
    # Constante k do Reciprocal Rank Fusion. 60 é o default da literatura.
    rag_rrf_k: int = 60
    # Quando True: pós-RRF, manda os top-N para o LLM reordenar com justificativa.
    # Trade-off: +500ms latência, +$0.0005/query, mas qualidade superior.
    # Quando False: usa heurística de overlap de termos (mais rápido, menos preciso).
    rag_rerank_with_llm: bool = True
    # Encoding do tiktoken — cl100k_base cobre GPT-4 / GPT-3.5 / text-embedding-3-*.
    rag_tiktoken_encoding: str = "cl100k_base"
    # Evidence ACL (64.0.0): quando True, o retriever filtra evidências pelo "no read
    # up" da evidence.rego (clearance do usuário vs confidentiality da fonte). Default
    # OFF = comportamento de hoje (sem filtro, zero regressão). Independe de opa_enabled
    # (a evidence.rego é avaliada direto — este é o seu próprio toggle).
    evidence_acl_enabled: bool = False

    # ── Observabilidade self-hosted (Onda 2 — OTel + Tempo + Loki + Grafana) ──
    # Default OFF: instrumentação só liga quando `OTEL_ENABLED=true` no .env e
    # o profile `full` do docker-compose estiver ativo (`docker compose --profile full up`).
    # Quando OFF, init_otel() é no-op e nenhuma dep OTel é exercitada em runtime.
    otel_enabled: bool = False
    otel_service_name: str = "agente-inteligencia"
    otel_service_version: str = "2.0.0"
    # Endpoint OTLP gRPC do Tempo (4317 é a porta padrão OTLP/gRPC).
    # Em docker-compose: tempo:4317. Local fora do compose: localhost:4317.
    otel_exporter_otlp_endpoint: str = "http://tempo:4317"
    # parentbased_always_on (default em dev) | parentbased_traceidratio (prod com OTEL_TRACES_SAMPLER_ARG=0.1)
    otel_traces_sampler: str = "parentbased_always_on"
    # Endpoint Loki (não usado pelo app — Promtail tail dos logs do Docker; mantido para futura
    # integração de log handler nativo, se quisermos emitir logs direto via push API).
    loki_endpoint: str = "http://loki:3100"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """SSOT: o arquivo .env NUNCA fornece credenciais/seleção de modelo.

        Pedido do operador (2026-06-06): todos os modelos da plataforma —
        provedores (Azure, OpenAI público, Maritaca, Ollama, GPT-OSS 120b/20b),
        embedding (Qwen3/Azure), Modelo Primário e Langfuse — devem usar
        EXCLUSIVAMENTE as chaves/acessos da tela de Configurações (persistidos
        em platform_settings e aplicados a os.environ por apply_settings_to_env).
        O .env passa a ser ignorado para esses campos.

        Implementação: filtra as chaves seladas (_SEALED_ENV_VARS) da fonte
        dotenv. Assim, mesmo que a chave exista no arquivo .env, ela não entra
        em Settings — cai no default da classe quando os.environ não a tiver.

        Precedência (inalterada): init > env (os.environ, escrito pela tela via
        apply_settings_to_env) > dotenv(FILTRADO) > secrets > defaults. Campos
        fora do escopo (infra, flags de segurança, default_llm_provider,
        grounding_strict, idioma) continuam lendo o .env normalmente.
        """
        def sealed_dotenv_settings():
            raw = dotenv_settings()
            return {
                key: value
                for key, value in raw.items()
                if key.upper() not in _SEALED_ENV_VARS
            }

        return (init_settings, env_settings, sealed_dotenv_settings, file_secret_settings)


# ═══════════════════════════════════════════════════════════════
# UI override → env vars
# ═══════════════════════════════════════════════════════════════
# Settings persistidas em platform_settings (settings_store) sobrescrevem
# os valores do .env em runtime. Estratégia: lê banco → popula os.environ
# → invalida lru_cache de get_settings() → invalida singleton de embedder.
# Próximas chamadas de get_settings() leem os env vars atualizados.
#
# Chamado em 2 momentos:
#  - lifespan startup do FastAPI (após init_db)
#  - PUT /settings (após set_many)
#
# Regra por escopo (SSOT de modelos, 2026-06-06):
#  - Chaves SELADAS (_SEALED_ENV_VARS: provedores, embedding, primário, Langfuse):
#    valor no banco → escreve em os.environ; banco vazio → REMOVE de os.environ
#    (apaga resíduo injetado pelo docker env_file) pra cair no default da classe.
#    O .env nunca alimenta essas chaves.
#  - Demais chaves (grounding_strict, default_response_language): valor no banco
#    sobrescreve; ausência NÃO mexe em os.environ — preserva o .env como fallback.
# ═══════════════════════════════════════════════════════════════

# Mapa chave-do-banco → nome-da-env-var. Pydantic é case-insensitive,
# então AZURE_OPENAI_API_KEY lê do mesmo lugar que azure_openai_api_key.
_UI_TO_ENV_MAP = {
    # Azure OpenAI (provedor primário)
    "azure_key":                  "AZURE_OPENAI_API_KEY",
    "azure_endpoint":             "AZURE_OPENAI_ENDPOINT",
    "azure_api_version":          "AZURE_OPENAI_API_VERSION",
    "azure_chat_deployment":      "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "azure_embeddings_deployment":"AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT",
    # Maritaca AI
    "maritaca_key":  "MARITACA_API_KEY",
    "maritaca_url":  "MARITACA_API_URL",
    "maritaca_model":"MARITACA_MODEL",
    # Ollama
    "ollama_url":    "OLLAMA_API_URL",
    "ollama_model":  "OLLAMA_MODEL",
    # OpenAI público (api.openai.com) — PR #194
    "openai_public_api_key":  "OPENAI_PUBLIC_API_KEY",
    "openai_public_base_url": "OPENAI_PUBLIC_BASE_URL",
    "openai_public_model":    "OPENAI_PUBLIC_MODEL",
    # GPT-OSS (open-weight via endpoint OpenAI-compatible)
    "oss120b_url":     "OSS120B_URL",
    "oss120b_model":   "OSS120B_MODEL",
    "oss120b_api_key": "OSS120B_API_KEY",
    "oss20b_url":      "OSS20B_URL",
    "oss20b_model":    "OSS20B_MODEL",
    "oss20b_api_key":  "OSS20B_API_KEY",
    "llm_timeout_seconds": "LLM_TIMEOUT_SECONDS",
    # Modelo Primário (fallback global)
    "primary_provider": "PRIMARY_PROVIDER",
    "primary_model":    "PRIMARY_MODEL",
    # Idioma de resposta global (BCP-47: pt-BR, en-US, ...)
    "default_response_language": "DEFAULT_RESPONSE_LANGUAGE",
    # CORS: allowlist de origens (CSV). Contenção da API Key: toggle de superfície pública.
    "cors_allowed_origins": "CORS_ALLOWED_ORIGINS",
    "api_key_public_surface_only": "API_KEY_PUBLIC_SURFACE_ONLY",
    "api_key_invoke_published_only": "API_KEY_INVOKE_PUBLISHED_ONLY",
    "api_key_cost_budget_enabled": "API_KEY_COST_BUDGET_ENABLED",
    # Timezone da plataforma (IANA: America/Sao_Paulo = GMT-3 Brasília, padrão).
    # Aplicado a os.environ['TZ'] + time.tzset() e exposto à UI (window.PLATFORM_TZ).
    "timezone": "TZ",
    # Grounded-by-default: 'true'/'false'. Desliga a recusa global de respostas
    # sem evidência (não recomendado — fura o princípio anti-alucinação).
    "grounding_strict": "GROUNDING_STRICT",
    # Guarda de injeção & DLP (IA Responsável 59.0.0): editáveis pela UI de
    # governança → aplicadas ao env em runtime (sem restart), como grounding_strict.
    "prompt_guard_enabled": "PROMPT_GUARD_ENABLED",
    "prompt_guard_block_threshold": "PROMPT_GUARD_BLOCK_THRESHOLD",
    "prompt_guard_warn_threshold": "PROMPT_GUARD_WARN_THRESHOLD",
    "dlp_enabled": "DLP_ENABLED",
    "dlp_redact_before_llm": "DLP_REDACT_BEFORE_LLM",
    # Policy-as-code (OPA) — cockpit de governança (62.0.0): toggle + failsafe +
    # timeout editáveis pela UI de IA Responsável → aplicados ao env em runtime
    # (sem restart), como grounding_strict. SEM esta entrada o PUT persiste no
    # banco mas NÃO aplica ao processo (apply_settings_to_env só varre este mapa).
    # NÃO-seladas (ver _NON_MODEL_UI_KEYS): o .env segue valendo como fallback.
    # opa_url NÃO entra: é infra, fica no .env/default.
    "opa_enabled": "OPA_ENABLED",
    "opa_failsafe_open": "OPA_FAILSAFE_OPEN",
    "opa_timeout_seconds": "OPA_TIMEOUT_SECONDS",
    # Evidence ACL (64.0.0): filtra evidências por clearance × confidencialidade
    # (evidence.rego). Editável pela UI de governança; NÃO-selada (ver abaixo).
    "evidence_acl_enabled": "EVIDENCE_ACL_ENABLED",
    # MCP per-tool (D): 'true'/'false'. Liga o modo em que cada tool MCP vira sua
    # própria função com o inputSchema real (vs o legado {operation, query}).
    # Default OFF; lido a cada chamada por runtime.per_tool_enabled().
    "mcp_per_tool_enabled": "MCP_PER_TOOL_ENABLED",
    # Tier 2 — text-to-SQL governado (RAG-Tabela): 'true'/'false'. Liga a bancada
    # "Perguntar à Tabela" (IA compila pergunta→consulta estruturada, humano cura).
    # Default OFF; lido a cada chamada por data_tables.runtime.text_to_sql_enabled().
    "text_to_sql_enabled": "TEXT_TO_SQL_ENABLED",
    # Embedding (Qwen3 reusa URL/key do OSS source)
    "embedding_provider": "EMBEDDING_PROVIDER",
    "qwen3_source":       "QWEN3_SOURCE",
    "qwen3_path":         "QWEN3_PATH",
    "qwen3_model":        "QWEN3_MODEL",
    "qwen3_dimensions":   "QWEN3_DIMENSIONS",
    # LangFuse (observabilidade SaaS opcional)
    "langfuse_public":"LANGFUSE_PUBLIC_KEY",
    "langfuse_secret":"LANGFUSE_SECRET_KEY",
    "langfuse_host":  "LANGFUSE_HOST",
    # NOTA: openai_key/openai_model não mapeiam — provider 'openai' virou
    # alias de Azure na Onda 7 Wave 5. Mantidos no settings_store apenas
    # pra retrocompat de UI (card OpenAI continua mostrando os campos).
    #
    # ── Módulo Parâmetros (25.1.0): Verifier/juiz + gates do Harness ──
    # Editáveis em Configurações → Parâmetros (root/admin). Efeito em runtime
    # SEM restart: o verifier relê get_settings() a cada julgamento e o PUT
    # /settings chama apply_settings_to_env + cache_clear (padrão F6 do MCP
    # per-tool). O modelo do juiz NÃO está aqui — é o papel `judge` do
    # Roteamento LLM (card "LLM como Juiz").
    "verifier_v2_enabled": "VERIFIER_V2_ENABLED",
    "verifier_signals_drive_fsm": "VERIFIER_SIGNALS_DRIVE_FSM",
    "verifier_factuality_threshold": "VERIFIER_FACTUALITY_THRESHOLD",
    "verifier_completeness_threshold": "VERIFIER_COMPLETENESS_THRESHOLD",
    "verifier_tone_threshold": "VERIFIER_TONE_THRESHOLD",
    "verifier_max_tokens": "VERIFIER_MAX_TOKENS",
    "verifier_contract_retry_enabled": "VERIFIER_CONTRACT_RETRY_ENABLED",
    "verifier_contract_retry_max_tokens": "VERIFIER_CONTRACT_RETRY_MAX_TOKENS",
    "verifier_production_async": "VERIFIER_PRODUCTION_ASYNC",
    "verifier_production_sample_rate": "VERIFIER_PRODUCTION_SAMPLE_RATE",
    "verifier_max_concurrent_jobs": "VERIFIER_MAX_CONCURRENT_JOBS",
    "harness_use_verifier": "HARNESS_USE_VERIFIER",
    "harness_min_accuracy": "HARNESS_MIN_ACCURACY",
    "harness_min_avg_factuality": "HARNESS_MIN_AVG_FACTUALITY",
    "harness_min_avg_completeness": "HARNESS_MIN_AVG_COMPLETENESS",
    "harness_min_avg_tone": "HARNESS_MIN_AVG_TONE",
    "harness_max_safety_violation_rate": "HARNESS_MAX_SAFETY_VIOLATION_RATE",
    "harness_min_contract_compliance": "HARNESS_MIN_CONTRACT_COMPLIANCE",
    "harness_max_hallucination_rate": "HARNESS_MAX_HALLUCINATION_RATE",
    "harness_max_dim_regression_pct": "HARNESS_MAX_DIM_REGRESSION_PCT",
    "harness_max_regression_pct": "HARNESS_MAX_REGRESSION_PCT",
    "harness_phrases_gate": "HARNESS_PHRASES_GATE",
    # Harness assíncrono + custo (43.0.0) — comportamento, não-selado.
    "harness_async_enabled": "HARNESS_ASYNC_ENABLED",
    "harness_jobs_max_concurrent": "HARNESS_JOBS_MAX_CONCURRENT",
    "harness_job_timeout_minutes": "HARNESS_JOB_TIMEOUT_MINUTES",
    "harness_budget_usd_per_run": "HARNESS_BUDGET_USD_PER_RUN",
    "harness_synthetic_retention_days": "HARNESS_SYNTHETIC_RETENTION_DAYS",
    # Loop reflexivo do otimizador (49.0.0) — comportamento, não-selado.
    "optimizer_loop_enabled": "OPTIMIZER_LOOP_ENABLED",
    "optimizer_max_rounds": "OPTIMIZER_MAX_ROUNDS",
    "optimizer_patience": "OPTIMIZER_PATIENCE",
    "optimizer_default_budget_usd": "OPTIMIZER_DEFAULT_BUDGET_USD",
    "optimizer_job_timeout_minutes": "OPTIMIZER_JOB_TIMEOUT_MINUTES",
    "optimizer_jobs_max_concurrent": "OPTIMIZER_JOBS_MAX_CONCURRENT",
    "ragas_ground_truth_enabled": "RAGAS_GROUND_TRUTH_ENABLED",
    # Tuning de performance (25.2.0)
    "query_topology_cache_enabled": "QUERY_TOPOLOGY_CACHE_ENABLED",
    "fast_routing_enabled": "FAST_ROUTING_ENABLED",
    # Invoke assíncrono 202 (34.0.0) — comportamento, não-selado.
    "invoke_async_enabled": "INVOKE_ASYNC_ENABLED",
    "invoke_jobs_retention_hours": "INVOKE_JOBS_RETENTION_HOURS",
    "invoke_jobs_max_concurrent": "INVOKE_JOBS_MAX_CONCURRENT",
    "invoke_job_timeout_minutes": "INVOKE_JOB_TIMEOUT_MINUTES",
    "interactions_retention_days": "INTERACTIONS_RETENTION_DAYS",
    "fx_usd_brl": "FX_USD_BRL",
    # Circuit-breaker do egress LLM (33.1.0) — comportamento, não-selado.
    "circuit_breaker_enabled": "CIRCUIT_BREAKER_ENABLED",
    "cb_failure_threshold": "CB_FAILURE_THRESHOLD",
    "cb_cooldown_seconds": "CB_COOLDOWN_SECONDS",
    "cb_half_open_max_probes": "CB_HALF_OPEN_MAX_PROBES",
    # Esforço de raciocínio das gerações do Wizard (skill/agente) — gate por
    # modelo em get_provider. 'high'|'medium'|'low'|'' (desligado). Default 'high'.
    "wizard_reasoning_effort": "WIZARD_REASONING_EFFORT",
}

# Chaves do módulo Parâmetros — usadas pelo endpoint GET /settings/parameters
# (valores efetivos p/ a aba) e pelos testes de contrato do mapa.
PARAMETER_UI_KEYS = (
    "verifier_v2_enabled",
    "verifier_signals_drive_fsm",
    "verifier_factuality_threshold",
    "verifier_completeness_threshold",
    "verifier_tone_threshold",
    "verifier_max_tokens",
    "verifier_contract_retry_enabled",
    "verifier_contract_retry_max_tokens",
    "verifier_production_async",
    "verifier_production_sample_rate",
    "verifier_max_concurrent_jobs",
    "harness_use_verifier",
    "harness_min_accuracy",
    "harness_min_avg_factuality",
    "harness_min_avg_completeness",
    "harness_min_avg_tone",
    "harness_max_safety_violation_rate",
    "harness_min_contract_compliance",
    "harness_max_hallucination_rate",
    "harness_max_dim_regression_pct",
    "harness_max_regression_pct",
    "harness_phrases_gate",
    "harness_async_enabled",
    "harness_jobs_max_concurrent",
    "harness_job_timeout_minutes",
    "harness_budget_usd_per_run",
    "harness_synthetic_retention_days",
    "optimizer_loop_enabled",
    "optimizer_max_rounds",
    "optimizer_patience",
    "optimizer_default_budget_usd",
    "optimizer_job_timeout_minutes",
    "optimizer_jobs_max_concurrent",
    "ragas_ground_truth_enabled",
    "query_topology_cache_enabled",
    "fast_routing_enabled",
    "invoke_async_enabled",
    "invoke_jobs_retention_hours",
    "invoke_jobs_max_concurrent",
    "invoke_job_timeout_minutes",
    "interactions_retention_days",
    "fx_usd_brl",
    "wizard_reasoning_effort",
)


# ═══════════════════════════════════════════════════════════════
# SSOT de modelos: env vars SELADAS (o .env é ignorado para elas)
# ═══════════════════════════════════════════════════════════════
# Pedido do operador (2026-06-06): a tela de Configurações é a ÚNICA fonte de
# verdade para credenciais/seleção de modelo. Estas env vars vêm só do banco
# (platform_settings → os.environ via apply_settings_to_env) ou do default da
# classe — NUNCA do .env.
#
# Subconjunto NÃO-modelo de _UI_TO_ENV_MAP: chaves que continuam podendo vir do
# .env porque não são credencial/seleção de modelo. Tudo o mais no mapa é selado.
_NON_MODEL_UI_KEYS = {
    "grounding_strict",          # flag de comportamento anti-alucinação
    "default_response_language", # idioma de resposta global (BCP-47)
    "cors_allowed_origins",      # allowlist CORS (não é credencial/modelo)
    "api_key_public_surface_only",  # toggle de contenção da API Key
    "api_key_invoke_published_only",  # toggle published-only p/ invoke via key
    "api_key_cost_budget_enabled",  # toggle de quota de custo por API Key (F6)
    "mcp_per_tool_enabled",      # flag do modo per-tool MCP (default OFF)
    "text_to_sql_enabled",       # flag do Tier 2 text-to-SQL governado (default OFF)
    "timezone",                  # timezone da plataforma (IANA); default Brasília
    # Policy-as-code (OPA) — flags de comportamento/infra, NÃO credencial/modelo.
    # Precisam ser NÃO-seladas para que o .env continue valendo como fallback de
    # boot: selá-las reverteria silenciosamente uma implantação que hoje liga o
    # OPA (ou fecha o failsafe = fail-closed) via .env para os defaults da classe
    # no upgrade — um downgrade de segurança silencioso. O cockpit/DB ainda
    # sobrescreve quando há valor (via apply_settings_to_env). opa_url idem.
    "opa_enabled",
    "opa_failsafe_open",
    "opa_timeout_seconds",
    "evidence_acl_enabled",      # flag do "no read up" de evidência (default OFF)
    # Circuit-breaker do egress LLM (33.1.0) — flags de comportamento, não
    # credencial/modelo → o .env vale como fallback quando o banco não tem valor.
    "circuit_breaker_enabled",
    "cb_failure_threshold",
    "cb_cooldown_seconds",
    "cb_half_open_max_probes",
    # Módulo Parâmetros (25.1.0): thresholds/flags do Verifier e harness NÃO
    # são credencial/seleção de modelo — o .env continua valendo como
    # fallback quando o banco não tem valor (retrocompat de instalações que
    # já configuravam por env).
    *PARAMETER_UI_KEYS,
}

# Cobre: Azure, OpenAI público, Maritaca, Ollama, GPT-OSS 120b/20b, embedding
# (Qwen3/Azure), Modelo Primário (provider/model + timeout) e Langfuse.
# Usado em 2 lugares: (1) Settings.settings_customise_sources filtra estas chaves
# da fonte dotenv; (2) apply_settings_to_env remove resíduos do .env de os.environ
# quando o banco não tem valor — forçando o default da classe.
_SEALED_ENV_VARS = frozenset(
    env_name
    for ui_key, env_name in _UI_TO_ENV_MAP.items()
    if ui_key not in _NON_MODEL_UI_KEYS
)


async def apply_settings_to_env() -> int:
    """Aplica as settings do banco (tela de Configurações) a os.environ.

    SSOT de modelos (2026-06-06): para as chaves SELADAS (_SEALED_ENV_VARS),
    esta função é AUTORITATIVA sobre os.environ:
      - valor não-vazio no banco → escreve em os.environ (a tela vence);
      - banco vazio/ausente → REMOVE de os.environ qualquer resíduo (ex: o
        docker injeta o .env inteiro via env_file no boot) pra que Settings caia
        no default da classe — o .env nunca alimenta essas chaves.
    Para as demais chaves do mapa (não-modelo: grounding_strict, idioma), mantém
    o comportamento legado: só sobrescreve quando há valor; ausência preserva o
    .env como fallback de boot.

    Invalida caches downstream (get_settings.lru_cache, _embedder singleton) pra
    que a próxima leitura pegue os valores novos sem restart.

    Retorna o número de chaves aplicadas (escritas). 0 se banco indisponível.
    """
    import os
    try:
        # Import tardio pra evitar ciclo (database importa get_settings).
        from app.core.database import settings_store
        data = await settings_store.get_all()
    except Exception:
        # Banco offline ou tabela ainda não criada (init_db não rodou). Não dá
        # pra selar sem o banco — loga pra troubleshooting e mantém o boot.
        logger.warning(
            "event=settings.apply_skipped reason=store_unavailable "
            "detail='settings_store.get_all() falhou; seal de modelos NÃO aplicado'",
            exc_info=True,
        )
        return 0

    applied = 0
    removed = 0
    for ui_key, env_name in _UI_TO_ENV_MAP.items():
        val = data.get(ui_key)
        if val is not None and str(val).strip():
            os.environ[env_name] = str(val).strip()
            applied += 1
        elif ui_key in data:
            # Operador salvou a chave EXPLICITAMENTE vazia (ex.: zerar
            # cors_allowed_origins para DESLIGAR o CORS). Honra a intenção
            # limpando o env — sem isto, valor vazio era ignorado e o env
            # antigo persistia no processo (a UI não conseguia desligar).
            if os.environ.pop(env_name, None) is not None:
                removed += 1
        elif env_name in _SEALED_ENV_VARS:
            # Selada e AUSENTE do banco → remove resíduo do .env de os.environ
            # (injetado pelo docker env_file) pra cair no default da classe.
            if os.environ.pop(env_name, None) is not None:
                removed += 1

    logger.info(
        "event=settings.model_seal applied=%d removed=%d sealed_total=%d",
        applied, removed, len(_SEALED_ENV_VARS),
    )

    # Timezone da plataforma: reflete TZ no time local do processo (datetime.now,
    # strftime). time.tzset() só existe em Unix (Docker); no-op em Windows (dev).
    try:
        import time as _time
        if hasattr(_time, "tzset"):
            _time.tzset()
    except Exception:
        pass

    # Overrides de pricing editáveis (TCO auditável): carrega a tabela de preços
    # de LLM do banco na camada de overrides do llm_pricing — runtime, sem deploy.
    try:
        import json as _json
        from app.core import llm_pricing as _lp
        _raw = data.get("llm_pricing_overrides")
        n = _lp.set_pricing_overrides(_json.loads(_raw) if _raw else {})
        if n:
            logger.info("event=pricing.overrides_loaded count=%d", n)
    except Exception:
        logger.warning("event=pricing.overrides_load_failed", exc_info=True)

    # Invalida cache pra próxima chamada de get_settings() rebuild com novas envs
    get_settings.cache_clear()

    # Invalida singleton do embedder + provider efetivo (instância existente
    # está com creds/provider antigos; o efetivo é re-resolvido no próximo embed).
    try:
        from app.evidence import embedder as _emb
        _emb._embedder = None
        _emb._effective_provider = None
    except Exception:
        pass

    return applied


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    # Defesa contra config errada: rate alto + async ligado sinaliza no log.
    # @lru_cache garante que isso só roda uma vez por processo.
    if s.verifier_production_async and s.verifier_production_sample_rate > 0.5:
        import logging
        logging.getLogger(__name__).warning(
            f"VERIFIER_PRODUCTION_SAMPLE_RATE={s.verifier_production_sample_rate} "
            "está alto (>50%). Custo de LLM extra pode ser proibitivo. "
            "Considere reduzir se isso não for intencional."
        )
    return s


# ═══════════════════════════════════════════════════════════════
# Postura de segurança de PRODUÇÃO — fail-fast no boot (SEC-02, 32.0.1)
# ═══════════════════════════════════════════════════════════════
# Princípio: falhe-FECHADO, nunca falhe-ABERTO. Em produção, um default
# inseguro é catastrófico e silencioso:
#   - secret_key == "change-me": a chave que ASSINA o cookie de sessão
#     (auth.sign_session → URLSafeTimedSerializer) é PÚBLICA (está no repo) →
#     qualquer um produz um token válido para qualquer user_id, inclusive root
#     = account takeover (CWE-798).
#   - MAESTRO_SECRET_KEY ausente: crypto._get_fernet cai num fallback
#     DETERMINÍSTICO conhecido → segredos at-rest (api-connectors) viram
#     recuperáveis por qualquer um com o dump (CWE-321).
#   - cookie_secure == False: o cookie de sessão trafega sem a flag Secure
#     (interceptável em HTTP claro — CWE-614).
#
# Severidade define a AÇÃO (falhe-fechado onde é catastrófico e sem caso de uso
# legítimo; avise onde bloquear quebraria um fluxo legítimo):
#   - secret_key / MAESTRO_SECRET_KEY → HARD-FAIL (raise): comprometimento
#     direto (takeover / segredos recuperáveis), nenhum motivo legítimo em prod.
#   - cookie_secure=False → WARNING (não bloqueia): tem caso de uso legítimo —
#     o MESMO app_env=production é usado no debug local por http://127.0.0.1
#     (onde um cookie Secure simplesmente não seria enviado, quebrando o login).
#     Bloquear o boot aqui derrubaria dev/VPS por uma questão de menor severidade;
#     logamos um WARNING acionável em vez disso.
# O guard SÓ age quando app_env é produção; em dev/staging é no-op. NÃO substitui
# a ROTAÇÃO das chaves: uma chave que já vazou com o default continua comprometida
# — rotacionar SECRET_KEY/MAESTRO_SECRET_KEY é pré-requisito operacional.

_PRODUCTION_ENVS = frozenset({"production", "prod"})

# Placeholder que o ``.env.example`` DISTRIBUI para o SECRET_KEY. Quem copia o
# template e esquece de trocar bootaria com uma chave que está PÚBLICA no repo
# (cookie de sessão forjável — o mesmo takeover do 'change-me'). Fica amarrado
# ao ``.env.example`` pelo meta-teste ``test_env_example_placeholder_e_barrado``:
# trocar o placeholder no template sem atualizar aqui quebra o teste.
_ENV_EXAMPLE_SECRET_PLACEHOLDER = "troque-isto-por-uma-chave-aleatoria-de-64-caracteres"

# SECRET_KEYs conhecidos-públicos → inseguros por definição em produção.
_INSECURE_SECRET_KEYS = frozenset({"", "change-me", _ENV_EXAMPLE_SECRET_PLACEHOLDER})


def is_production(settings: "Settings | None" = None) -> bool:
    """True quando o app roda em produção (app_env in {production, prod})."""
    s = settings if settings is not None else get_settings()
    return s.app_env.strip().lower() in _PRODUCTION_ENVS


class InsecureProductionConfigError(RuntimeError):
    """Boot barrado: configuração insegura detectada com app_env=produção."""


def assert_secure_production_posture(settings: "Settings | None" = None) -> None:
    """Falha-fecha o boot nos defaults CATASTRÓFICOS de produção; avisa nos demais.

    No-op fora de produção. Em produção:
      - HARD-FAIL (raise ``InsecureProductionConfigError``): SECRET_KEY no default
        público e MAESTRO_SECRET_KEY ausente. Coleta os dois numa exceção só
        (conserto num restart), cada linha nomeando a env var a corrigir.
      - WARNING (não bloqueia): COOKIE_SECURE=false — bloquear quebraria o debug
        local por http sob o mesmo app_env=production.
    """
    import os

    s = settings if settings is not None else get_settings()
    if not is_production(s):
        return

    # WARNING (não bloqueia): cookie sem flag Secure. Menor severidade + caso de
    # uso legítimo (http://127.0.0.1 local). Loga acionável e segue o boot.
    if not s.cookie_secure:
        logger.warning(
            "event=security.cookie_insecure_prod detail='COOKIE_SECURE=false em "
            "produção — o cookie de sessão vai sem a flag Secure (interceptável em "
            "HTTP claro). Defina COOKIE_SECURE=true onde o app é servido por HTTPS.'"
        )

    # HARD-FAIL: comprometimento direto, sem caso de uso legítimo em produção.
    fatal: list[str] = []
    if s.secret_key.strip() in _INSECURE_SECRET_KEYS:
        fatal.append(
            "SECRET_KEY num default público conhecido ('change-me' ou o "
            "placeholder do .env.example) — o cookie de sessão é forjável "
            "(account takeover). Defina um SECRET_KEY aleatório e único."
        )
    if not os.environ.get("MAESTRO_SECRET_KEY", "").strip():
        fatal.append(
            "MAESTRO_SECRET_KEY ausente — a cifra de segredos at-rest cai em "
            "fallback determinístico inseguro. Defina um MAESTRO_SECRET_KEY."
        )

    if fatal:
        raise InsecureProductionConfigError(
            "Boot BARRADO: app_env=produção com configuração insegura.\n  - "
            + "\n  - ".join(fatal)
            + "\n(Guard SEC-02 — falhe-fechado. Corrija/rotacione e reinicie.)"
        )
