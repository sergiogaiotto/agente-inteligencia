// ════════════════════════════════════════════════════════════════
// Guia dos Módulos — fundamentos, aplicação, ativação e uso.
//
// Cada módulo tem 4 abas (HTML, escapado pelo Alpine):
//   - fundamento: o que é + como funciona por baixo
//   - aplicacao:  quando faz sentido usar + casos práticos
//   - ativar:     como ligar (config, env, comandos)
//   - usar:       como operar pelo browser ou API
//
// Tom: profissional friendly, sem emojis, com analogias concretas
// e exemplos práticos. Reescrita 2026-05.
// ════════════════════════════════════════════════════════════════
window.MODULE_GUIDE = [

  // ═════════════════════════════════════════════════════════════════
  // §4 — Topologia AOBD → AR → SA
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's4',
    section: '§4',
    label: 'Topologia AOBD → AR → SA',
    fundamento: `<p>A plataforma organiza agents em três camadas com responsabilidades distintas — como uma empresa que tem gerente, supervisor e operador.</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>AOBD (Orchestrator)</b> — o "gerente". Recebe uma pergunta vinda do usuário, interpreta a intenção e decide qual fluxo executar. Consulta o CAR (§6) para escolher.</li>
  <li><b>AR (Router)</b> — o "supervisor". Decompõe um fluxo em etapas, controla dependências entre subagents, gerencia o DAG de execução.</li>
  <li><b>SA (Subagent)</b> — o "operador". Executa uma tarefa atômica: chama uma tool, gera uma resposta, classifica um item. Stateless por design.</li>
</ul>
<p class="mt-2">Comunicação entre camadas é via <b>Protocolo A2A</b> (§7) — envelopes tipados e assinados, com rastreabilidade total.</p>
<p class="mt-2"><b>Quando se importar com isso:</b> só quando tiver mais de 3-4 agents resolvendo coisas relacionadas. Para um agent isolado, ignorar — todo SA funciona sozinho.</p>`,
    aplicacao: `<p>A separação em camadas paga conta quando você precisa de:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Decisão dinâmica de rota</b> — usuário fez 1 pergunta, mas pode cair em 5 fluxos diferentes. Quem decide é o AOBD.</li>
  <li><b>Reuso entre fluxos</b> — o mesmo SA "Validar CNPJ" serve para 3 ARs diferentes. Sem replicar lógica.</li>
  <li><b>Auditoria por nível</b> — quando algo dá errado, você sabe se foi decisão ruim (AOBD), orquestração ruim (AR) ou execução ruim (SA).</li>
</ul>
<p class="mt-2"><b>Quando NÃO precisa:</b> agent que faz uma coisa só, invocado de um lugar só. Crie 1 SA e pronto — sem AOBD, sem AR. Topologia é remédio para complexidade, não vitamina.</p>`,
    ativar: `<p>Topologia é nativa da plataforma — sempre disponível. Para criar agents em cada camada:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/agents \\
  -H "Content-Type: application/json" \\
  -d '{
    "name":"AOBD Geral",
    "kind":"aobd",
    "llm_provider":"azure"
  }'</pre>
<p class="mt-2">Valores possíveis para <code class="bg-surface-100 px-1 rounded">kind</code>: <code>aobd</code>, <code>router</code>, <code>subagent</code>. Default é <code>subagent</code>.</p>`,
    usar: `<p>Pelo navegador:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Crie um agent em cada camada em <a href="/agents" class="text-brand-500 underline">/agents</a>.</li>
  <li>Em <a href="/mesh" class="text-brand-500 underline">/mesh</a>, conecte AOBD → AR → SA arrastando os nós.</li>
  <li>No <a href="/workspace" class="text-brand-500 underline">/workspace</a>, selecione o pipeline e envie uma mensagem — o trace mostra o trajeto pelas 3 camadas.</li>
</ol>
<p class="mt-2"><b>Dica de debugging:</b> se uma interação parece "pular" um agent, abra o trace em <a href="/observability" class="text-brand-500 underline">/observability</a> — provavelmente o agent está como pass-through (sem skill nem prompt customizado) e foi ignorado automaticamente para economizar LLM call.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §5 — Parser SKILL.md Canônico
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's5',
    section: '§5',
    label: 'Parser SKILL.md',
    fundamento: `<p>SKILL.md é o <b>contrato declarativo</b> de um agent. Não é documentação opcional — é um artefato executável que a plataforma carrega em tempo de ativação e usa em pontos específicos da execução.</p>
<p class="mt-2"><b>Anatomia mínima</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Frontmatter YAML</b> — id, version (semver), kind, owner, stability</li>
  <li><b>Purpose</b> — uma frase: o que essa skill faz</li>
  <li><b>Workflow</b> — passo-a-passo (vai pro system prompt)</li>
  <li><b>Tool Bindings</b> — tools que essa skill pode chamar (filtra o Permitted Toolset)</li>
  <li><b>Output Contract</b> — schema da resposta (Verifier valida)</li>
  <li><b>Failure Modes</b> — o que fazer quando algo der errado</li>
</ul>
<p class="mt-2">Validação estrutural ocorre na criação. Hash SHA-256 garante imutabilidade da versão. Versão semver permite evoluir sem quebrar consumidores.</p>`,
    aplicacao: `<p>Defina uma skill uma vez, reaproveite em vários agents:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Comportamento previsível</b> — workflow declarado em vez de prompt solto no agent. Outros desenvolvedores entendem o que a skill faz sem rodar.</li>
  <li><b>Permitted Toolset explícito</b> — só tools listadas em "Tool Bindings" ficam visíveis ao LLM. Mesmo que outras estejam registradas em /tools, o agent não tem como descobrir.</li>
  <li><b>Failure modes documentados</b> — não é "se der pau, gera 500". É "se input inválido → retorne JSON de erro estruturado; se tool falhar → tente alternativa Y".</li>
  <li><b>Execution Profile</b> — fast/standard/rigorous controla número de iterações de raciocínio e se há verificação de evidência.</li>
</ul>`,
    ativar: `<p>Parser é nativo — sempre ativo. Para criar uma skill via API:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/skills \\
  -H "Content-Type: application/json" \\
  -d '{
    "name":"FAQ Cliente",
    "raw_content":"---\\nid: faq-cliente\\nversion: 1.0.0\\nkind: subagent\\n..."
  }'</pre>
<p class="mt-2"><b>Atalho:</b> em vez de escrever raw_content na mão, use o Wizard IA no editor visual (próxima aba).</p>`,
    usar: `<p>No editor visual:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Vá para <a href="/skills/new" class="text-brand-500 underline">/skills/new</a>.</li>
  <li>Clique no botão "Wizard" — a IA faz perguntas e gera o esqueleto da SKILL.md.</li>
  <li>Refine no editor de texto.</li>
  <li>Aba <b>Preview / Validação</b> mostra a anatomia detectada + o Execution Profile inferido.</li>
  <li>Vincule a um agent em <a href="/agents" class="text-brand-500 underline">/agents</a> pelo campo "Skill Vinculada".</li>
</ol>
<p class="mt-2"><b>Pegadinha comum:</b> registrou uma tool em /tools mas o agent nunca chama? Verifique se ela está listada em "Tool Bindings" do SKILL.md. Tools fora dessa lista são invisíveis ao LLM.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §6 — CAR (Catálogo de Roteadores)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's6',
    section: '§6',
    label: 'CAR — Catálogo de Roteadores',
    fundamento: `<p>CAR é a "lista telefônica" que o AOBD consulta para descobrir qual Agent Roteador chamar quando recebe uma mensagem.</p>
<p class="mt-2">Diferente de um service registry tradicional (que indexa por endpoint), o CAR indexa por <b>intenção</b>: keywords + entidades reconhecidas + perfil do ator + jornada.</p>
<p class="mt-2"><b>Matching híbrido</b>:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1.5">
  <li><b>Filtro simbólico</b> — keywords e entidades. Rápido, determinístico, descarta o que claramente não bate.</li>
  <li><b>Ranking vetorial</b> — sobre os candidatos sobreviventes. Captura paráfrase ("quero abrir um chamado" vs "preciso de ajuda").</li>
</ol>
<p class="mt-2">Empates são quebrados por: <code>stability</code> &gt; <code>custo</code> &gt; <code>taxa de sucesso</code>.</p>`,
    aplicacao: `<p>Use o CAR para:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Roteamento dinâmico</b> — usuário envia uma mensagem, AOBD escolhe o AR certo sem você precisar codar a regra em IF/ELSE.</li>
  <li><b>A/B de roteadores</b> — dois ARs cobrindo a mesma intenção; a plataforma mede e o melhor "vence".</li>
  <li><b>Documentação viva</b> — listar as entradas do CAR mostra exatamente quais jornadas a plataforma cobre.</li>
</ul>
<p class="mt-2"><b>Não usa o CAR:</b> agent isolado invocado por outro sistema via API direto — não passa pelo AOBD, então não consulta o CAR.</p>`,
    ativar: `<p>CAR é nativo e sempre ativo. Para adicionar uma entrada:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/car \\
  -H "Content-Type: application/json" \\
  -d '{
    "skill_urn":"faq-cliente@1.0.0",
    "domain":"atendimento",
    "activation_keywords":["faq","duvida","pergunta"],
    "actor_profile":"customer"
  }'</pre>`,
    usar: `<p>Em <a href="/mesh" class="text-brand-500 underline">/mesh</a>, na aba "CAR", você lista, cria e edita entradas. O matching é automático — quando AOBD recebe uma mensagem, consulta o CAR sozinho.</p>
<p class="mt-2"><b>Dica:</b> se um AR esperado nunca está sendo escolhido, abra o trace em /observability e procure pelo span <code>car.match</code> — ele mostra os candidatos avaliados e os scores. Se o seu nem aparece, falta entrada no CAR ou as keywords estão estreitas demais.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §7 — Protocolo A2A / Envelope
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's7',
    section: '§7',
    label: 'Protocolo A2A / Envelope',
    fundamento: `<p>Quando um agent fala com outro, não é via JSON solto na rede — é via <b>envelope A2A</b>, uma estrutura tipada que carrega contexto, identidade e orçamento.</p>
<p class="mt-2"><b>Campos principais</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><code>envelope_id</code> (ULID), <code>trace_id</code>/<code>span_id</code> (W3C Trace Context)</li>
  <li><code>IntentDescriptor</code> — a intenção do usuário, preservada em toda a cadeia (mesmo quando o LLM gera resposta de um SA fundo na pilha)</li>
  <li><code>skill_ref</code> — qual skill a invocação está usando</li>
  <li><code>budget</code> — limites de tokens, ms e USD que essa cadeia pode gastar</li>
  <li><code>deadline</code> — quando a invocação caduca</li>
  <li><b>Assinatura criptográfica</b> — agent receptor verifica integridade</li>
  <li><code>ContextDelta</code> — mutações append-only (cada agent adiciona, não sobrescreve)</li>
</ul>`,
    aplicacao: `<p>Envelopes brilham em:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Multi-agent debugging</b> — você consegue reconstruir exatamente o que cada agent recebeu e o que devolveu, sem juntar pedaços de logs.</li>
  <li><b>Budget enforcement</b> — se a cadeia AOBD → AR → SA estourar o budget configurado, a próxima delegação é negada.</li>
  <li><b>Auditoria forte</b> — assinaturas + hash garantem que ninguém alterou a comunicação após o fato.</li>
</ul>`,
    ativar: `<p>Envelope é gerado automaticamente em toda delegação inter-agente. Para inspecionar via API:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">GET /api/v1/envelopes?limit=20</pre>
<p class="mt-2">Filtros disponíveis: <code>trace_id</code>, <code>agent_id</code>, janela de tempo.</p>`,
    usar: `<p>Em <a href="/history" class="text-brand-500 underline">/history</a>, aba "Envelopes" — lista todas as comunicações entre agents com busca textual e filtros.</p>
<p class="mt-2"><b>Caso de uso típico:</b> reclamação de cliente. Você pega o trace_id da interação em /history → filtra envelopes → lê a cadeia inteira. Cada delegação está lá, com input/output, modelo usado, tempo gasto.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §9.5 — Harness de Avaliação
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's95',
    section: '§9.5',
    label: 'Harness de Avaliação',
    fundamento: `<p>Harness é o "CI/CD de qualidade" da plataforma. Antes de promover uma release para produção, ele roda a skill contra um conjunto de casos curados (Golden Dataset) e decide se passou ou não.</p>
<p class="mt-2"><b>Métricas que o Harness produz</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Acurácia ponderada</b> — média dos casos, com pesos (casos críticos contam mais)</li>
  <li><b>Taxa de recusa correta</b> — casos adversariais que DEVEM ser recusados</li>
  <li><b>Falso positivo</b> — casos que DEVERIAM passar mas foram recusados</li>
  <li><b>Latência p50/p95</b> — distribuição de tempo de resposta</li>
  <li><b>Custo em USD</b> — quanto custou rodar o conjunto inteiro</li>
</ul>
<p class="mt-2"><b>Cada caso suporta</b>: <code>category</code> (taxonomia), <code>weight</code> (peso 0.1–10), <code>expected_pattern</code> (regex Python), <code>red_flags</code> (strings que NUNCA podem aparecer — útil para detectar vazamento de PII).</p>
<p class="mt-2">Gate automático: thresholds configuráveis. Acima → release aprovada. Abaixo → bloqueada.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Baseline antes de promover</b> — qualquer mudança em skill/modelo/prompt passa pelo harness primeiro.</li>
  <li><b>Detecção de regressão silenciosa</b> — provider atualizou o modelo sem avisar? O harness pega antes de quebrar produção.</li>
  <li><b>Comparação A/B objetiva</b> — 2 releases, mesmo Golden Dataset, números falam. Sem "achismo".</li>
  <li><b>Detecção de leak</b> — <code>red_flags</code> com strings de PII ou segredos pega vazamento antes de chegar ao cliente.</li>
</ul>`,
    ativar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Vá para <a href="/harness" class="text-brand-500 underline">/harness</a> → painel "Golden Dataset".</li>
  <li>Adicione casos: input + expected_output (ou expected_pattern em regex) + red_flags.</li>
  <li>Crie uma release em <a href="/releases" class="text-brand-500 underline">/releases</a>.</li>
  <li>Execute o harness selecionando agent + release + tipo (baseline / regressão).</li>
</ol>`,
    usar: `<p>Via API direto:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/harness/run
{
  "release_id":"...",
  "agent_id":"...",
  "gold_version":"v1",
  "run_type":"baseline"
}</pre>
<p class="mt-2">Resultado traz acurácia, breakdown por categoria, métricas e o gate (aprovado/reprovado).</p>
<p class="mt-2"><b>Dica de calibração:</b> comece com Golden Dataset pequeno (10-20 casos cobrindo as jornadas principais + 5 adversariais). Refine os thresholds depois de 2-3 releases. Threshold alto demais e ninguém promove nada; baixo demais e bugs passam.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §14 — RAG (Retriever + Reranker)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's14',
    section: '§14',
    label: 'RAG (Retriever + Reranker)',
    fundamento: `<p>RAG (Retrieval-Augmented Generation) é a camada que <b>busca</b> documentos relevantes para alimentar o LLM com contexto factual.</p>
<p class="mt-2"><b>Pipeline de busca</b>:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1.5">
  <li><b>Retriever híbrido</b> — combina dois mundos: <b>BM25</b> (busca textual clássica via Postgres <code>tsvector</code> + GIN) + <b>vetorial</b> (busca semântica via Qdrant + embeddings).</li>
  <li><b>Reciprocal Rank Fusion (k=60)</b> — funde os dois rankings em um só.</li>
  <li><b>Reranker LLM (opcional)</b> — reordena os top-K por relevância contextual, com justificativa.</li>
</ol>
<p class="mt-2"><b>Importante:</b> RAG ≠ Verificação. O RAG entrega evidências; quem julga se a resposta usou-as bem é o <b>Verifier</b> (§14.2). Use os dois juntos.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>FAQs e bases regulatórias</b> — respostas com citação obrigatória (sem RAG, o LLM "inventa").</li>
  <li><b>Atendimento ao cliente</b> — buscar contratos, manuais, políticas para responder com precisão.</li>
  <li><b>Pipelines que precisam de grounding</b> — qualquer fluxo que não pode tolerar alucinação.</li>
</ul>
<p class="mt-2"><b>Não usar:</b> tarefas puramente criativas (escrever um e-mail genérico, gerar slogan). RAG adiciona custo e latência sem benefício.</p>`,
    ativar: `<p>RAG é nativo. Toggle global: <code>RAG_V2_ENABLED=true</code> (default).</p>
<p class="mt-2">Para ingerir um documento:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># 1. Criar knowledge_source
SRC=$(curl -s -X POST /api/v1/knowledge-sources \\
  -d '{"name":"FAQ","authorized":1}' | jq -r .id)

# 2. Ingerir texto (chunca + embeda + persiste)
curl -X POST /api/v1/knowledge-sources/$SRC/ingest \\
  -d '{"text":"...","replace":true}'</pre>
<p class="mt-2"><b>Pegadinha do embedder:</b> se você ingere com Azure embeddings e depois muda para Qwen3 nas configurações, as queries antigas não casam mais com os vetores. Re-ingira tudo ao trocar de embedder.</p>`,
    usar: `<p>Use direto no <a href="/workspace" class="text-brand-500 underline">/workspace</a>: ao mandar mensagem, o Retriever busca em todas as <code>knowledge_sources</code> com <code>authorized=1</code> automaticamente.</p>
<p class="mt-2">Diagnóstico:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">GET /api/v1/rag/health
GET /api/v1/knowledge-sources/$SRC/chunks</pre>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §14.2 — Verifier
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's142',
    section: '§14.2',
    label: 'Verifier — Judge Multi-Dimensional',
    fundamento: `<p>O Verifier é o "controle de qualidade" automático que avalia toda resposta gerada <b>antes</b> dela chegar no usuário.</p>
<p class="mt-2"><b>Quatro dimensões avaliadas</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>factuality (0-5)</b> — as afirmações do draft têm suporte nas evidências recuperadas?</li>
  <li><b>completeness (0-5)</b> — cobre todos os pontos da pergunta?</li>
  <li><b>tone_adherence (0-5)</b> — respeita o tom esperado + guardrails da skill?</li>
  <li><b>safety (0|1)</b> — sem PII vazada, sem dados internos expostos, sem violação de política?</li>
</ul>
<p class="mt-2"><b>Plus ContractValidator</b> — antes de o juiz LLM rodar, um validador determinístico checa se a resposta bate com o <code>output_contract</code> declarado na skill. Falha precoce evita gastar com o juiz.</p>
<p class="mt-2">Cada execução vira uma linha em <code>verifications</code> — material para detecção de drift (§18) e harness (§9.5).</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Detecção granular de alucinação</b> — o juiz identifica e lista os <code>unsupported_claims</code>. Você sabe exatamente qual frase do draft não tem suporte.</li>
  <li><b>Compliance de formato</b> — output_contract validado deterministicamente. Se a skill diz "responda em JSON", o Verifier garante.</li>
  <li><b>Quality gate em produção</b> — score por dimensão alimenta o sistema de drift. Se factuality cai semana a semana, alguma coisa mudou (skill, modelo, base de evidência).</li>
  <li><b>Sinal estruturado para Harness</b> — promoção de release pode exigir não só "passou nos casos" mas "passou nos casos com factuality ≥ 4".</li>
</ul>`,
    ativar: `<p>Toggle: <code>VERIFIER_V2_ENABLED=true</code> no <code>.env</code>.</p>
<p class="mt-2">Configuração completa:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">VERIFIER_V2_ENABLED=true
VERIFIER_JUDGE_MODEL=azure/gpt-4o
VERIFIER_FACTUALITY_THRESHOLD=3.0
VERIFIER_COMPLETENESS_THRESHOLD=3.0
VERIFIER_TONE_THRESHOLD=3.0</pre>
<p class="mt-2">Default OFF para retrocompat. Quando OFF, pipeline cai no <code>_LegacyVerifier</code> (binário, sem dimensões).</p>`,
    usar: `<p>Cada interação no /workspace gera 1 linha em <code>verifications</code> com scores. Para investigar:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">SELECT factuality_score, completeness_score,
       tone_score, safety_score,
       contract_compliant, ok, confidence, judge_model
FROM verifications
ORDER BY created_at DESC LIMIT 10;</pre>
<p class="mt-2">Painel visual em <a href="/quality" class="text-brand-500 underline">/quality</a>: agregados por janela (24h/7d/30d) + drill-down em interações com baixa nota.</p>
<p class="mt-2"><b>Pegadinha de self-preference:</b> juiz e gerador do mesmo modelo (gpt-4o avaliando gpt-4o) tende a dar nota alta. Para reduzir viés, use um juiz diferente do gerador.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §15 — FSM de Interação (9 estados)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's15',
    section: '§15',
    label: 'FSM de Interação (9 estados)',
    fundamento: `<p>Toda interação na plataforma passa por uma máquina de estados determinística. Não há "ramos acidentais" — cada transição é explícita, atômica e auditada.</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">Intake → PolicyCheck → RetrieveEvidence → DraftAnswer
       → VerifyEvidence → Recommend | Refuse | Escalate
       → LogAndClose</pre>
<p class="mt-2"><b>Invariantes que a plataforma garante</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li>Todo caminho termina em <code>LogAndClose</code> — não há "fim sem log"</li>
  <li><code>VerifyEvidence</code> é obrigatório — não é opcional pular</li>
  <li>Transições são atômicas (DB transaction) — meia-transição não existe</li>
  <li>Cada transição vira uma linha em <code>audit_log</code></li>
</ul>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Comportamento previsível</b> — você sempre sabe por onde a interação passou.</li>
  <li><b>Auditoria estado-a-estado</b> — pegar uma interação problemática e ver exatamente onde virou ruim.</li>
  <li><b>Recusa controlada</b> — Refuse é estado de primeira classe (não é exception). Quando o agent recusa, fica claro o motivo.</li>
  <li><b>Escalation visível</b> — Escalate marca explicitamente "isso precisa de humano" — sem isso, escalações ficam invisíveis.</li>
</ul>`,
    ativar: `<p>FSM é nativa. Toda chamada em <code>execute_interaction()</code> passa por ela — sempre ativa, sem toggle.</p>`,
    usar: `<p>Em <a href="/workspace" class="text-brand-500 underline">/workspace</a>, ative o "Execution Log" ao vivo para ver cada transição em tempo real.</p>
<p class="mt-2">Para análise retroativa, em <a href="/history" class="text-brand-500 underline">/history</a> você vê o log completo de qualquer interação passada — filtrado por trace_id, agent, ou texto.</p>
<p class="mt-2"><b>Interpretando estados finais:</b></p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><code>Recommend</code> — agent respondeu normalmente</li>
  <li><code>Refuse</code> — agent decidiu não responder (políticas, escopo, segurança) — não é bug</li>
  <li><code>Escalate</code> — agent reconheceu limites próprios, marcou para humano</li>
</ul>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §16 — Modelo de Dados (27 tabelas)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's16',
    section: '§16',
    label: 'Modelo de Dados',
    fundamento: `<p>PostgreSQL 16 + asyncpg é o backend único da plataforma. Schema em 7 grupos:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Operação</b> — <code>agents</code>, <code>skills</code>, <code>agent_bindings</code>, <code>mesh_connections</code></li>
  <li><b>Execução</b> — <code>interactions</code>, <code>turns</code>, <code>envelopes</code>, <code>traces</code></li>
  <li><b>Evidência</b> — <code>knowledge_sources</code>, <code>evidence_chunks</code>, <code>evidences</code> (BM25 nativo Postgres)</li>
  <li><b>Tools</b> — <code>tools</code>, <code>tool_calls</code></li>
  <li><b>Releases</b> — <code>releases</code>, <code>gold_cases</code>, <code>eval_runs</code>, <code>drift_events</code></li>
  <li><b>Auditoria</b> — <code>audit_log</code> (append-only)</li>
  <li><b>Plataforma</b> — <code>users</code>, <code>domains</code>, <code>platform_settings</code>, <code>system_prompts</code>, <code>api_connectors</code>, <code>api_endpoints</code>, <code>api_call_logs</code>, <code>journeys</code>, <code>car_entries</code>, <code>catalog_*</code></li>
</ul>
<p class="mt-2">Todas as tabelas são acessadas via <code>Repository</code> genérico assíncrono (<code>agents_repo</code>, <code>tools_repo</code>, etc.). Tabelas com PK diferente de <code>id</code> têm helpers especializados em <code>queries.py</code>.</p>`,
    aplicacao: `<p>Use direto via REST quando o frontend não cobre o que você precisa, ou estenda em código novo aproveitando o Repository genérico (CRUD pronto).</p>
<p class="mt-2"><b>Para alguns casos vale SQL cru:</b></p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li>Investigação de incidente (queries ad-hoc no <code>audit_log</code>)</li>
  <li>Relatórios analíticos com JOIN em várias tabelas</li>
  <li>Migrations de dado (script Python via Repository)</li>
</ul>`,
    ativar: `<p>Pool asyncpg é criado em <code>init_db()</code> no startup do FastAPI. Migrações são idempotentes via <code>ALTER TABLE ADD COLUMN IF NOT EXISTS</code> — seguro re-rodar.</p>`,
    usar: `<p>Acesso direto via psql para diagnóstico:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">docker exec -it agente_postgres \\
  psql -U agente agente_inteligencia

\\dt              # lista tabelas
\\d catalog_recipe_executions  # estrutura de uma tabela
SELECT count(*) FROM audit_log
WHERE created_at &gt; now() - interval '1 day';</pre>
<p class="mt-2"><b>Cuidado:</b> <code>audit_log</code> e <code>interactions</code> crescem em GB/mês em produção. Configure retenção e archive antigo se necessário.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §17 — Observabilidade self-hosted
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's17',
    section: '§17',
    label: 'Observabilidade Self-Hosted',
    fundamento: `<p>Stack <b>OpenTelemetry → Tempo (traces) + Loki (logs) + Grafana (UI)</b>. Tudo self-hosted, sem dependência de SaaS.</p>
<p class="mt-2"><b>Auto-instrumentação</b>: FastAPI, asyncpg, httpx, redis, logging — toda chamada gera span automaticamente. Todo log carrega <code>trace_id</code> e <code>span_id</code> para correlação cruzada.</p>
<p class="mt-2"><b>Spans manuais</b> nos pontos críticos:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><code>fsm.transition</code> — cada estado da FSM (§15)</li>
  <li><code>evidence.retrieve.bm25</code> / <code>.vector</code> — etapas do RAG</li>
  <li><code>evidence.rerank</code> — reranker LLM</li>
  <li><code>ingest.*</code> — chunking, embedding, persistência</li>
  <li><code>policy.evaluate</code> — decisões do OPA</li>
</ul>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Debug end-to-end</b> — clica num span no Tempo e pula direto para os logs filtrados pelo trace_id no Loki. Sem alt-tab entre 3 ferramentas.</li>
  <li><b>Latência por estado FSM</b> — dashboard provisionado mostra p50/p95 por etapa. "Por que está lento?" tem resposta visual.</li>
  <li><b>Custo por interação</b> — atributos OTel capturam tokens e custo. Comparações entre modelos viram gráfico.</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># 1. Toggle no .env
OTEL_ENABLED=true

# 2. Subir a stack completa
docker compose --profile full up -d</pre>
<p class="mt-2">Custo de infra: ~2.5 GB RAM adicional (Tempo 1G, Loki 1G, Grafana 512M).</p>
<p class="mt-2"><b>Alternativa SaaS:</b> se preferir LangFuse em vez de stack OTEL, configure as credenciais em /settings → Plataforma → LangFuse.</p>`,
    usar: `<p>Acesse <b>http://localhost:3000</b> (login: admin/admin):</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li><b>Explore</b> → datasource Tempo → busca por <code>service.name=agente-inteligencia</code></li>
  <li>Clique num trace → árvore com todos os spans hierárquicos</li>
  <li>Num span, opção "Logs for this span" → pula pro Loki com filtro já aplicado</li>
  <li>Dashboard pré-provisionado: <b>AgenteInteligência → FSM &amp; Logs</b></li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §18 — Version Registry / Drift
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's18',
    section: '§18',
    label: 'Version Registry / Drift',
    fundamento: `<p>Uma <b>release</b> é uma tupla imutável: <code>(modelo + prompt + índice + policy)</code>. Você não promove um artefato isolado — promove a tupla inteira.</p>
<p class="mt-2"><b>Por que tupla?</b> Skill nova só funciona bem com o índice atualizado. Modelo novo só funciona bem com prompt ajustado. Promover artefato isolado = bug em produção.</p>
<p class="mt-2"><b>Estágios de promoção</b>:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li><b>staging</b> — visível só para devs/testers</li>
  <li><b>canary</b> — 1-10% do tráfego em produção</li>
  <li><b>production</b> — 100% do tráfego</li>
</ol>
<p class="mt-2"><b>Detecção de drift</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><b>KS</b> (Kolmogorov-Smirnov) e <b>PSI</b> para drift de dados de entrada</li>
  <li><b>CUSUM</b> para drift de comportamento (scores do Verifier caindo)</li>
</ul>
<p class="mt-2">Quando SLOs são violados, rollback é automático.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Reprodutibilidade</b> — rodar uma interação histórica com a release da época dá resultado determinístico.</li>
  <li><b>Rollback rápido</b> — 1 comando volta para a release anterior, sem precisar regenerar artefatos.</li>
  <li><b>Quality gate integrado</b> — Harness (§9.5) decide a promoção. Sem aprovação manual escondendo regressão.</li>
</ul>`,
    ativar: `<p>Sempre ativo. Criar release:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/releases
{
  "version":"v1.2.0",
  "stage":"staging",
  "model_ref":"azure/gpt-4o",
  "prompt_hash":"..."
}</pre>`,
    usar: `<p>Em <a href="/releases" class="text-brand-500 underline">/releases</a> você lista releases, promove entre estágios, e monitora drift events.</p>
<p class="mt-2"><b>Workflow recomendado:</b></p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Cria release em staging</li>
  <li>Roda harness para estabelecer baseline</li>
  <li>Promove para canary (1% tráfego)</li>
  <li>Observa por 24-48h — drift events e SLOs</li>
  <li>Se tudo OK, promove para production (100%)</li>
</ol>
<p class="mt-2"><b>Cuidado:</b> pular canary "porque é uma mudança pequena" é onde incidentes começam. Sempre passe por canary.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Onda 1 — Segurança fundacional
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'onda1',
    section: 'Onda 1',
    label: 'Segurança Fundacional',
    fundamento: `<p>Camada que cobre os principais riscos do <b>OWASP LLM Top 10</b>. Ativada na primeira onda de produção, com mitigações automatizadas:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>LLM01 Prompt Injection</b> — guard com score 0..1. Bloqueia quando ≥ 0.7 (configurável).</li>
  <li><b>LLM04 Model DoS</b> — rate-limit em sliding window via Redis. Default 60 req/min por user.</li>
  <li><b>LLM06 PII Leakage</b> — DLP detecta e redacta CPF, CNPJ, e-mail, cartão antes da persistência em <code>audit_log</code> e <code>interactions</code>.</li>
  <li><b>LLM10 Prompt Leak</b> — em traces e logs, o <code>system_prompt</code> aparece como hash + preview de 80 chars (não o texto completo).</li>
  <li><b>Auth</b> — senhas em bcrypt, secrets cifrados com <code>cryptography</code>, CSRF opcional (toggle).</li>
</ul>`,
    aplicacao: `<p>Sempre ativo em produção. Você ajusta thresholds via <code>.env</code>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><code>RATE_LIMIT_DEFAULT_PER_MIN=60</code></li>
  <li><code>PROMPT_GUARD_BLOCK_THRESHOLD=0.7</code></li>
  <li><code>DLP_ENABLED=true</code></li>
  <li><code>PROMPT_LEAK_GUARD_ENABLED=true</code></li>
</ul>
<p class="mt-2"><b>Quando ajustar:</b> ambiente de dev pode relaxar rate-limit; produção com dados regulados deve aumentar o threshold do prompt guard.</p>`,
    ativar: `<p>Tudo ativo no <code>docker compose</code> default — nada para ligar manualmente.</p>
<p class="mt-2">Para desativar uma camada específica (não recomendado em prod), edite <code>.env</code> e reinicie o app.</p>`,
    usar: `<p>Eventos vão para <code>audit_log</code>. Consultas úteis:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">-- Tentativas de prompt injection bloqueadas (24h)
SELECT * FROM audit_log
WHERE action='prompt_injection_blocked'
  AND created_at &gt; now() - interval '24 hours'
ORDER BY created_at DESC;

-- Top users em rate-limit
SELECT actor, count(*) FROM audit_log
WHERE action='rate_limit_exceeded'
GROUP BY actor ORDER BY count DESC;</pre>
<p class="mt-2">Métricas agregadas no dashboard de observabilidade: distribuição de scores do prompt guard, taxa de bloqueio, redactions por categoria.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Onda 4a — Policy as Code (OPA)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'onda4a',
    section: 'Onda 4a',
    label: 'Policy as Code (OPA)',
    fundamento: `<p><b>Open Policy Agent</b> integrado como PEP/PDP (Policy Enforcement / Decision Point). Substitui o stub legacy de PolicyCheck por um motor de políticas em <b>Rego</b>, versionadas em git.</p>
<p class="mt-2"><b>Três políticas piloto</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><code>interaction.rego</code> — gate do PolicyCheck (prompt_injection, rate_limit, user). Roda em toda interação.</li>
  <li><code>tool_invocation.rego</code> — sensitivity × user.role × trusted_context. Gate por chamada de tool.</li>
  <li><code>evidence.rego</code> — clearance vs confidentiality (declarado; PEP fica para onda futura).</li>
</ul>
<p class="mt-2"><b>Por que Rego em vez de IF/ELSE em Python?</b> Política versionada em git, audit trail por decisão, mesma política reutilizada em vários pontos do fluxo, mudança de regra sem rebuild da app.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Compliance</b> — auditor pede "mostre quem pode chamar essa tool" → política em git é a resposta canônica.</li>
  <li><b>Tools sensíveis</b> — DELETE, escrita em sistema externo, envio de e-mail — gate por role + contexto antes da chamada.</li>
  <li><b>Failsafe configurável</b> — modo <code>open</code> em dev (continua se OPA cair), modo <code>closed</code> em prod com dados regulados (bloqueia se OPA cair).</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># OPA já sobe no compose
docker compose up -d opa

# Verifica que as 3 políticas estão lá
curl http://localhost:8181/v1/policies | jq '.result | length'
# → 3

# Liga o gate no app
echo "OPA_ENABLED=true" >> .env
docker compose up -d --force-recreate app</pre>`,
    usar: `<p>Smoke test direto no OPA:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST http://localhost:8181/v1/data/interaction/allow \\
  -d '{"input":{"prompt_injection":{"score":0.9}}}'
# → {"result":false}</pre>
<p class="mt-2">Cada decisão gera linha em <code>audit_log</code> com <code>entity_type='policy_decision'</code> — auditoria fica completa.</p>
<p class="mt-2"><b>Adicionar política nova:</b></p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Edite arquivo em <code>infra/opa/policies/*.rego</code></li>
  <li><code>docker compose restart opa</code></li>
  <li>Política versionada via git — historicamente rastreável</li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Onda 4c — TLS + Secrets management
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'onda4c',
    section: 'Onda 4c',
    label: 'TLS + Secrets Management',
    fundamento: `<p>Duas dores resolvidas: <b>tráfego em HTTPS</b> (sem vergonha em produção pública) e <b>secrets fora do git</b> (sem chave de API exposta em repo).</p>
<p class="mt-2"><b>Caddy</b> como reverse proxy em paralelo à porta :7000:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li>HTTPS automático em prod (Let's Encrypt nativo, sem certbot manual)</li>
  <li>Headers de segurança baseline: X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy</li>
  <li>Compressão gzip/zstd</li>
  <li>Logs estruturados em JSON</li>
</ul>
<p class="mt-2"><b>Secrets management</b>: script <code>check-secrets-leak.sh</code> escaneia padrões high-confidence — sk-proj-, sk-ant-, pk-lf-, chaves AWS, tokens GitHub. Pre-commit hook bloqueia commit de chaves.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Produção web</b> — HTTPS público obrigatório. Caddy resolve tudo (cert + renovação + headers).</li>
  <li><b>Pre-commit hook</b> — <code>./check-secrets-leak.sh --staged</code> roda antes do commit e bloqueia se detectar chave. Saúde do repo cresce.</li>
  <li><b>CI/CD</b> — adicione step de scan no pipeline. Sem isso, mais cedo ou mais tarde alguém vai commitar uma chave.</li>
</ul>`,
    ativar: `<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># Modo dev (HTTP local)
TLS_HTTP_PORT_HOST=8080 docker compose up -d caddy
curl http://localhost:8080/api/health

# Modo prod (HTTPS Let's Encrypt automático)
# .env:
#   TLS_SITE=meudominio.com
#   CADDY_GLOBAL=email admin@meudominio.com
docker compose up -d caddy</pre>`,
    usar: `<p>Scan de secrets:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># Scan completo do working tree
./infra/scripts/check-secrets-leak.sh

# Scan só do staged (uso em pre-commit hook)
./infra/scripts/check-secrets-leak.sh --staged</pre>
<p class="mt-2">Documentação completa em <code>infra/README.md §13</code>: rotação de chaves por provider, caminhos de evolução (Sealed Secrets, HashiCorp Vault).</p>
<p class="mt-2"><b>Pegadinha clássica:</b> chave que vazou no git history continua acessível mesmo depois de <code>git rm</code>. Sempre revogue a chave no provider, gere nova, e em seguida considere reescrever o histórico (ou aceitar que a chave antiga está queimada).</p>`
  }
];
