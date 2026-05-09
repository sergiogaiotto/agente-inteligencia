// ════════════════════════════════════════════════════════════════
// Guia dos Módulos Implementados — conteúdo das 14 entries.
// Cada módulo tem 4 abas: fundamento, aplicacao, ativar, usar.
// HTML simples (escapado por Alpine).
// ════════════════════════════════════════════════════════════════
window.MODULE_GUIDE = [

  // ─── Especificação canônica ──────────────────────────────────
  {
    id: 's4',
    section: '§4',
    label: 'Topologia AOBD→AR→SA',
    fundamento: `<p>Três camadas verticais de agentes que dividem responsabilidade:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>AOBD</b> (Orquestrador de Business Domain): interpreta intenção do usuário, consulta o CAR (§6) para decidir delegação.</li>
<li><b>AR</b> (Agente Roteador): decompõe processos complexos em DAGs de workflow.</li>
<li><b>SA</b> (Subagente): executa tarefas atômicas stateless com tools MCP.</li>
</ul>
<p class="mt-2">Comunicação inter-camadas via <b>Protocolo A2A</b> (§7) com envelopes tipados e assinados.</p>`,
    aplicacao: `<p>Use quando precisar separar:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Política / Decisão</b> (AOBD) — qual rota tomar para a intenção do usuário</li>
<li><b>Coordenação</b> (AR) — orquestrar múltiplos passos com dependências</li>
<li><b>Execução</b> (SA) — chamar uma tool, formatar uma resposta atômica</li>
</ul>
<p class="mt-2">Análogo a controlador → orquestrador → worker em microserviços.</p>`,
    ativar: `<p>Topologia é nativa — sempre ativa. Para criar agentes em cada camada:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/agents \\
  -d '{"name":"AOBD-Geral","kind":"aobd","llm_provider":"azure"}'</pre>
<p class="mt-2"><code class="text-[10px] bg-surface-100 px-1 rounded">kind</code> ∈ <code class="text-[10px]">aobd | router | subagent</code>.</p>`,
    usar: `<p>Pelo navegador:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
<li>Acesse <a href="/agents" class="text-brand-500 underline">/agents</a> e crie um agente em cada camada.</li>
<li>Em <a href="/mesh" class="text-brand-500 underline">/mesh</a>, conecte AOBD → AR → SA arrastando.</li>
<li>No <a href="/workspace" class="text-brand-500 underline">/workspace</a>, escolha o pipeline e envie uma mensagem — vai trafegar nos 3 agentes em sequência.</li>
</ol>`
  },

  {
    id: 's5',
    section: '§5',
    label: 'Parser SKILL.md Canônico',
    fundamento: `<p>SKILL.md é o <b>contrato executável</b> do agente — não é doc, é artefato declarativo.</p>
<p class="mt-2">Anatomia: frontmatter YAML (id, version, kind, owner, stability) + 7 seções obrigatórias (Purpose, Activation Criteria, Inputs, Workflow, Tool Bindings, Output Contract, Failure Modes) + 10 opcionais.</p>
<p class="mt-2">Validação estrutural + hash SHA-256 + versionamento semver.</p>`,
    aplicacao: `<p>Defina uma vez, reutilize em N agentes:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Comportamento previsível</b> — workflow declarado, sem prompt rolando livre</li>
<li><b>Tool bindings</b> explícitos — Permitted Toolset = interseção SKILL × MCP registry</li>
<li><b>Failure modes</b> documentados — o que fazer em cada erro conhecido</li>
<li><b>Execution Profile</b> (fast/standard/rigorous) controla iteração e verificação</li>
</ul>`,
    ativar: `<p>Parser é nativo (sempre ativo). Para criar uma SKILL.md:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/skills \\
  -d '{"name":"FAQ-Cliente","raw_content":"---\\nid: faq-cliente\\n..."}' </pre>`,
    usar: `<p>Editor visual:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
<li>Acesse <a href="/skills/new" class="text-brand-500 underline">/skills/new</a>.</li>
<li>Use o Wizard IA (botão "✨ Wizard") ou cole SKILL.md cru no editor.</li>
<li>Aba <b>Preview/Validação</b> mostra anatomia detectada e Execution Profile.</li>
<li>Vincule a um agente em <a href="/agents" class="text-brand-500 underline">/agents</a> via "Skill ID".</li>
</ol>`
  },

  {
    id: 's6',
    section: '§6',
    label: 'CAR — Catálogo Roteadores',
    fundamento: `<p><b>Comparative Activation Registry</b>: catálogo de Agentes Roteadores indexado por <b>intenção</b>, não por endpoint.</p>
<p class="mt-2">Matching híbrido: filtro simbólico (keywords + entidades) + ranking vetorial. Empates resolvidos por <code>stability > custo > taxa de sucesso</code>.</p>
<p class="mt-2">Análogo a service registry em microserviços, mas indexado por semântica.</p>`,
    aplicacao: `<p>Use o CAR para:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Roteamento dinâmico</b> — o AOBD escolhe AR pela intenção em runtime</li>
<li><b>A/B de roteadores</b> — múltiplos AR para mesma intenção, métrica decide</li>
<li><b>Documentação viva</b> — qual AR cobre qual jornada</li>
</ul>`,
    ativar: `<p>Sempre ativo. Adicionar entradas:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/car
{"skill_urn":"faq-cliente@1.0.0","domain":"atendimento",
 "activation_keywords":["faq","duvida","pergunta"],
 "actor_profile":"customer"}</pre>`,
    usar: `<p>Em <a href="/mesh" class="text-brand-500 underline">/mesh</a>, aba "CAR", você lista, cria e edita entradas. O matching ocorre automaticamente quando o AOBD recebe input — não precisa chamar manualmente.</p>`
  },

  {
    id: 's7',
    section: '§7',
    label: 'Protocolo A2A / Envelope',
    fundamento: `<p>Unidade de comunicação entre agentes, tipada e auditável:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><code>envelope_id</code> (ULID), <code>trace_id/span_id</code> (W3C OTel)</li>
<li><code>IntentDescriptor</code> preservado em toda a cadeia</li>
<li><code>skill_ref</code>, contexto tipado, <code>budget</code> (tokens/ms/USD), <code>deadline</code></li>
<li>Assinatura criptográfica</li>
<li><code>ContextDelta</code> append-only para mutações</li>
</ul>`,
    aplicacao: `<p>Onde envelopes brilham:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Multi-agente</b> — passar contexto entre AOBD/AR/SA sem perder rastreabilidade</li>
<li><b>Budget enforcement</b> — corta cadeia se gastos excedem o cap</li>
<li><b>Auditoria</b> — toda mensagem tem hash + assinatura</li>
</ul>`,
    ativar: `<p>Geração de envelope é automática a cada delegação inter-agente. Para inspecionar:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">GET /api/v1/envelopes?limit=20</pre>`,
    usar: `<p>Em <a href="/history" class="text-brand-500 underline">/history</a>, aba "Envelopes" mostra todas as comunicações inter-agente da plataforma com filtros de busca textual.</p>`
  },

  {
    id: 's95',
    section: '§9.5',
    label: 'Harness Avaliação',
    fundamento: `<p>Motor que executa skills contra dataset gold adversarial e produz métricas + gate de release.</p>
<p class="mt-2">Métricas: acurácia, taxa de recusa correta, falso positivo, latência, custo. Gate automático com thresholds configuráveis — regressão acima de N% bloqueia deploy.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Baseline</b> antes de promover release para canary</li>
<li><b>Regressão</b> em mudanças (skill, modelo, prompt, índice)</li>
<li><b>Comparação A/B</b> entre 2 releases</li>
</ul>`,
    ativar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
<li>Crie casos gold em <a href="/harness" class="text-brand-500 underline">/harness</a> aba "Gold Cases".</li>
<li>Crie uma release em <a href="/releases" class="text-brand-500 underline">/releases</a>.</li>
<li>Execute o harness selecionando agente + release + tipo (baseline/regressão).</li>
</ol>`,
    usar: `<p>Endpoint direto:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/harness/run
{"release_id":"...","agent_id":"...","gold_version":"v1","run_type":"baseline"}</pre>
<p class="mt-2">Resultado mostra acurácia + métricas + gate (aprovado/reprovado).</p>`
  },

  {
    id: 's14',
    section: '§14',
    label: 'Evidence Runtime + RAG real',
    fundamento: `<p>Toda recomendação ancorada em evidência. Pipeline:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
<li><b>Retriever</b> híbrido — BM25 (Postgres tsvector + GIN) + vetorial (Qdrant + Azure embeddings)</li>
<li><b>RRF</b> (Reciprocal Rank Fusion, k=60) funde os dois rankings</li>
<li><b>Reranker</b> opcional via LLM (GPT-4o reordena com justificativa)</li>
<li><b>EvidenceChecker</b> verifica consistência, cobertura, conflitos antes da entrega</li>
</ol>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>FAQs e bases regulatórias</b> — respostas com citação obrigatória</li>
<li><b>Atendimento ao cliente</b> — buscar contratos, manuais, política</li>
<li><b>Compliance</b> — recusa controlada se evidência insuficiente</li>
</ul>`,
    ativar: `<p>Sempre ativo. Toggle: <code>RAG_V2_ENABLED=true</code> (default).</p>
<p class="mt-2">Para ingerir um documento:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># 1. Criar knowledge_source
SRC=$(curl -s -X POST /api/v1/knowledge-sources \\
  -d '{"name":"FAQ","authorized":1}' | jq -r .id)

# 2. Ingerir texto cru (chunca + embeda + persiste)
curl -X POST /api/v1/knowledge-sources/$SRC/ingest \\
  -d '{"text":"...","replace":true}'</pre>`,
    usar: `<p>Use no <a href="/workspace" class="text-brand-500 underline">/workspace</a> — quando você manda mensagem, o Retriever busca evidências automaticamente em todas as <code>knowledge_sources</code> com <code>authorized=1</code>.</p>
<p class="mt-2">Diagnóstico:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">GET /api/v1/rag/health
GET /api/v1/knowledge-sources/$SRC/chunks</pre>`
  },

  {
    id: 's15',
    section: '§15',
    label: 'FSM Interação (9 estados)',
    fundamento: `<p>Máquina de estados determinística para cada interação:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">Intake → PolicyCheck → RetrieveEvidence → DraftAnswer
       → VerifyEvidence → Recommend|Refuse|Escalate
       → LogAndClose</pre>
<p class="mt-2"><b>Invariantes</b>: todo caminho termina em LogAndClose; VerifyEvidence é obrigatório; transições são atômicas e auditadas.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Comportamento previsível</b> — não há ramos "acidentais"</li>
<li><b>Auditoria estado-a-estado</b> — cada transição vira linha em <code>audit_log</code></li>
<li><b>Recusa controlada</b> — Refuse é estado de primeira classe</li>
</ul>`,
    ativar: `<p>Nativa em toda interação que passa por <code>execute_interaction()</code>. Sempre ativa.</p>`,
    usar: `<p>No <a href="/workspace" class="text-brand-500 underline">/workspace</a>, ative o "Execution Log" ao vivo: você vê cada transição em tempo real. Em <a href="/history" class="text-brand-500 underline">/history</a> você lê o log completo retroativamente.</p>`
  },

  {
    id: 's16',
    section: '§16',
    label: 'Modelo de Dados (27 tabelas)',
    fundamento: `<p>PostgreSQL 16 + asyncpg como backend único. Schema com 27 tabelas:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Operação</b>: agents, skills, agent_bindings, mesh_connections</li>
<li><b>Execução</b>: interactions, turns, envelopes, traces</li>
<li><b>Evidência</b>: knowledge_sources, evidence_chunks (BM25), evidences</li>
<li><b>Tools</b>: tools, tool_calls</li>
<li><b>Releases</b>: releases, gold_cases, eval_runs, drift_events</li>
<li><b>Auditoria</b>: audit_log (com policy_decisions)</li>
<li><b>Plataforma</b>: users, domains, platform_settings, system_prompts, api_connectors, api_endpoints, api_call_logs, journeys, car_entries</li>
</ul>`,
    aplicacao: `<p>Repository genérico (<code>knowledge_repo</code>, <code>tools_repo</code>, etc.) expõe CRUD assíncrono em cada tabela. Use direto via API REST ou estenda em código novo.</p>`,
    ativar: `<p>Pool asyncpg cria-se em <code>init_db()</code> no startup. Migrações idempotentes via <code>ALTER TABLE ADD COLUMN IF NOT EXISTS</code>.</p>`,
    usar: `<p>Acesso direto ao banco para diagnóstico:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">docker exec -it agente_postgres \\
  psql -U agente agente_inteligencia
\\dt   # lista tabelas
SELECT count(*) FROM audit_log;</pre>`
  },

  {
    id: 's17',
    section: '§17',
    label: 'Observabilidade self-hosted',
    fundamento: `<p>Stack OpenTelemetry → <b>Tempo</b> (traces) + <b>Loki</b> (logs) + <b>Grafana</b> (UI).</p>
<p class="mt-2"><b>Auto-instrumented</b>: FastAPI, asyncpg, httpx, redis, logging (com <code>trace_id/span_id</code> em todo log record).</p>
<p class="mt-2"><b>Spans manuais</b> nos pontos críticos: <code>fsm.transition</code>, <code>evidence.retrieve.{bm25,vector}</code>, <code>evidence.rerank</code>, <code>ingest.*</code>, <code>policy.evaluate</code>.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Debug E2E</b> — clica num span no Tempo e pula direto para os logs filtrados pelo <code>trace_id</code> no Loki</li>
<li><b>Latência por estado FSM</b> — dashboard provisionado mostra p50/p95</li>
<li><b>Custo por interação</b> — atributos OTel capturam tokens/custo</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># 1. .env: OTEL_ENABLED=true
# 2. Subir stack completa
docker compose --profile full up -d</pre>
<p class="mt-2">Recursos extras: ~2.5 GB RAM (Tempo 1G, Loki 1G, Grafana 512M).</p>`,
    usar: `<p>Acesse <b>http://localhost:3000</b> (admin/admin):</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
<li><b>Explore</b> → datasource Tempo → Search por <code>service.name=agente-inteligencia</code></li>
<li>Clique num trace → árvore com todos os spans</li>
<li>No span, "Logs for this span" → pula pro Loki</li>
<li>Dashboard <b>AgenteInteligência → FSM & Logs</b> já provisionado</li>
</ol>`
  },

  {
    id: 's18',
    section: '§18',
    label: 'Version Registry / Drift',
    fundamento: `<p>Cada release é tupla imutável: <code>(modelo + prompt + índice + policy)</code>. Não promove artefato isolado — promove a tupla.</p>
<p class="mt-2">Promoção gated: <code>staging → canary (1%) → production</code>.</p>
<p class="mt-2">Detecção de drift por <b>KS</b> (Kolmogorov-Smirnov) e <b>PSI</b> (dados) e <b>CUSUM</b> (comportamento) contra baseline. Rollback automático em violação de SLO.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Reprodutibilidade</b> — re-rodar uma release passada é determinístico</li>
<li><b>Rollback rápido</b> — voltar para release anterior é 1 comando</li>
<li><b>Quality gate</b> — Harness (§9.5) decide se promove</li>
</ul>`,
    ativar: `<p>Sempre ativo. Crie releases:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/releases
{"version":"v1.2.0","stage":"staging",
 "model_ref":"azure/gpt-4o","prompt_hash":"..."}</pre>`,
    usar: `<p>Em <a href="/releases" class="text-brand-500 underline">/releases</a> você lista, promove e monitora drift events de cada release.</p>`
  },

  // ─── Ondas de produção ───────────────────────────────────────
  {
    id: 'onda1',
    section: 'Onda 1',
    label: 'Segurança fundacional',
    fundamento: `<p>Cobertura OWASP LLM Top 10:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>LLM01 Prompt Injection</b> — guard com score 0..1; bloqueia em ≥0.7</li>
<li><b>LLM04 Model DoS</b> — rate-limit sliding window via Redis (60 req/min default)</li>
<li><b>LLM06 PII Leakage</b> — DLP redacta CPF/CNPJ/email/cartão antes da persistência</li>
<li><b>LLM10 Prompt Leak</b> — em traces, hash + preview do system_prompt em vez do texto cru</li>
<li><b>Auth</b> — bcrypt hash + secrets cifrados (cryptography), CSRF opcional</li>
</ul>`,
    aplicacao: `<p>Sempre ativo desde a Onda 1. Ajusta thresholds via <code>.env</code>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><code>RATE_LIMIT_DEFAULT_PER_MIN=60</code></li>
<li><code>PROMPT_GUARD_BLOCK_THRESHOLD=0.7</code></li>
<li><code>DLP_ENABLED=true</code></li>
<li><code>PROMPT_LEAK_GUARD_ENABLED=true</code></li>
</ul>`,
    ativar: `<p>Já ativo no compose default. Nada precisa ligar.</p>`,
    usar: `<p>Eventos visíveis em <code>audit_log</code>:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">SELECT * FROM audit_log
WHERE action='prompt_injection_blocked'
ORDER BY created_at DESC LIMIT 10;</pre>
<p class="mt-2">Métricas no dashboard: total de tentativas bloqueadas, distribuição por score.</p>`
  },

  {
    id: 'onda4a',
    section: 'Onda 4a',
    label: 'Policy as Code (OPA)',
    fundamento: `<p>Open Policy Agent integrado como <b>PEP/PDP</b>. Substitui o stub legacy de PolicyCheck por motor de políticas Rego versionadas.</p>
<p class="mt-2">3 políticas piloto:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><code>interaction.rego</code> — gate do PolicyCheck (prompt_injection, rate_limit, user)</li>
<li><code>tool_invocation.rego</code> — sensitivity × user.role × trusted_context</li>
<li><code>evidence.rego</code> — clearance vs confidentiality (definido; PEP futuro)</li>
</ul>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Compliance</b> — política versionada em git, audit trail por decisão</li>
<li><b>Tools sensíveis</b> — gate por role antes de cada chamada</li>
<li><b>Failsafe configurável</b> — open (dev) ou closed (prod com dados sensíveis)</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># OPA já sobe junto com o stack
docker compose up -d opa
curl http://localhost:8181/v1/policies | jq '.result | length'
# → 3

# Ligar no app:
echo "OPA_ENABLED=true" >> .env
docker compose up -d --force-recreate app</pre>`,
    usar: `<p>Smoke test direto no OPA:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST http://localhost:8181/v1/data/interaction/allow \\
  -d '{"input":{"prompt_injection":{"score":0.9}}}'
# → {"result":false}</pre>
<p class="mt-2">Cada decisão vira linha em <code>audit_log</code> com <code>entity_type='policy_decision'</code>. Adicione política nova: editar <code>infra/opa/policies/*.rego</code> + <code>docker compose restart opa</code>.</p>`
  },

  {
    id: 'onda4b',
    section: 'Onda 4b',
    label: 'AI Gateway (LiteLLM)',
    fundamento: `<p>Proxy <b>OpenAI-compatible</b> único entre app e providers. Centraliza:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Routing</b> — 7 modelos (Azure, OpenAI, Maritaca, Ollama)</li>
<li><b>Fallback automático</b> — Azure GPT-4o cai → tenta OpenAI GPT-4o</li>
<li><b>LangFuse callback</b> nativo — cada call vira span observável</li>
<li><b>Cost tracking</b> unificado por modelo/chave</li>
<li><b>Defesa em profundidade</b> Python — gateway 5xx → upstream direto</li>
</ul>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Trocar provider sem redeploy</b> — edita <code>infra/litellm/config.yaml</code> + restart container</li>
<li><b>Adicionar Anthropic/Gemini</b> — só editar yaml + env</li>
<li><b>Rate-limit por modelo</b> — config nativa do LiteLLM</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># 1. Master key (uma vez)
echo "LLM_GATEWAY_MASTER_KEY=sk-litellm-$(openssl rand -hex 24)" >> .env

# 2. Subir gateway
docker compose up -d litellm

# 3. Ligar no app
echo "LLM_GATEWAY_ENABLED=true" >> .env
docker compose up -d --force-recreate app</pre>`,
    usar: `<p>Smoke test:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">MK=$(grep ^LLM_GATEWAY_MASTER_KEY .env | cut -d= -f2)
curl http://localhost:4000/v1/chat/completions \\
  -H "Authorization: Bearer $MK" \\
  -d '{"model":"azure/gpt-4o","messages":[{"role":"user","content":"ok"}]}'</pre>
<p class="mt-2">Logs em <code>docker logs -f agente_litellm</code> mostram cada call. Métricas no LangFuse externo.</p>`
  },

  {
    id: 'onda4c',
    section: 'Onda 4c',
    label: 'TLS + Secrets management',
    fundamento: `<p><b>Caddy</b> reverse proxy em paralelo à porta :7000:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
<li>HTTPS automático em prod (Let's Encrypt nativo)</li>
<li>Headers de segurança baseline (X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy)</li>
<li>Compressão gzip/zstd</li>
<li>Logs JSON estruturados</li>
</ul>
<p class="mt-2"><b>Secrets management</b>: script <code>check-secrets-leak.sh</code> escaneia padrões high-confidence (sk-proj-, sk-ant-, pk-lf-, AWS, GitHub).</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1">
<li><b>Produção web</b> — HTTPS público obrigatório</li>
<li><b>Pre-commit hook</b> — script com <code>--staged</code> bloqueia commit de chaves</li>
<li><b>CI</b> — adicionar step de scan</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px"># Modo dev (HTTP only)
TLS_HTTP_PORT_HOST=8080 docker compose up -d caddy
curl http://localhost:8080/api/health

# Modo prod (HTTPS Let's Encrypt)
# .env: TLS_SITE=meudominio.com
#       CADDY_GLOBAL=email admin@meudominio.com
docker compose up -d caddy</pre>`,
    usar: `<p>Scan de secrets:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">./infra/scripts/check-secrets-leak.sh
./infra/scripts/check-secrets-leak.sh --staged</pre>
<p class="mt-2">Doc completa em <code>infra/README.md §13</code> — rotação de chaves por provedor + caminhos de evolução (Sealed Secrets, Vault).</p>`
  }
];
