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
  // §4 — Topologia Maestro → Triagem → Especialista
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's4',
    section: '§4',
    label: 'Topologia Maestro → Triagem → Especialista',
    fundamento: `<p>A plataforma organiza agents em três camadas com responsabilidades distintas — como uma empresa que tem gerente, supervisor e operador.</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Maestro</b> — o "gerente". Coordena várias etapas/agentes do início ao fim de um pedido composto. Decide quem faz o quê e em que ordem, mas NUNCA executa a tarefa final — sempre delega. No agente, é <code>kind=aobd</code>.</li>
  <li><b>Triagem</b> — o "supervisor de fila". Lê o pedido, classifica a intenção e encaminha para o Especialista certo. Não resolve a tarefa — só roteia. No agente, é <code>kind=router</code>.</li>
  <li><b>Especialista</b> — o "operador". Executa uma tarefa atômica: chama uma tool, gera uma resposta, classifica um item. Stateless por design. No agente, é <code>kind=subagent</code>.</li>
</ul>
<p class="mt-2">Comunicação entre camadas é via <b>Protocolo A2A</b> (§7) — envelopes tipados e assinados, com rastreabilidade total.</p>
<p class="mt-2"><b>Quando se importar com isso:</b> só quando tiver mais de 3-4 agents resolvendo coisas relacionadas. Para um agent isolado, ignorar — todo Especialista funciona sozinho.</p>`,
    aplicacao: `<p>A separação em camadas paga conta quando você precisa de:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Decisão dinâmica de rota</b> — o usuário fez 1 pergunta, mas pode cair em 5 fluxos diferentes. Quem decide é o Maestro.</li>
  <li><b>Reuso entre fluxos</b> — o mesmo Especialista "Validar CNPJ" serve para 3 Triagens diferentes. Sem replicar lógica.</li>
  <li><b>Auditoria por nível</b> — quando algo dá errado, você sabe se foi decisão ruim (Maestro), roteamento ruim (Triagem) ou execução ruim (Especialista).</li>
</ul>
<p class="mt-2"><b>Quando NÃO precisa:</b> agent que faz uma coisa só, invocado de um lugar só. Crie 1 Especialista e pronto — sem Maestro, sem Triagem. Topologia é remédio para complexidade, não vitamina.</p>`,
    ativar: `<p>Topologia é nativa da plataforma — sempre disponível. Para criar agents em cada camada:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/agents \\
  -H "Content-Type: application/json" \\
  -d '{
    "name":"Maestro Geral",
    "kind":"aobd",
    "task_type":"reasoning"
  }'</pre>
<p class="mt-2">Valores possíveis para <code class="bg-surface-100 px-1 rounded">kind</code> do agente: <code>aobd</code> (Maestro), <code>router</code> (Triagem), <code>subagent</code> (Especialista). Default é <code>subagent</code>. <b>Atenção:</b> na SKILL.md o Maestro é declarado como <code>kind: orchestrator</code> (não <code>aobd</code>) — são enums distintos para a mesma camada.</p>`,
    usar: `<p>Pelo navegador:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Crie um agent em cada camada em <a href="/agents" class="text-brand-500 underline">/agents</a> (escolha o card Maestro, Triagem ou Especialista).</li>
  <li>Em <a href="/mesh/flow" class="text-brand-500 underline">/mesh/flow</a> (Fluxo de agentes), conecte Maestro → Triagem → Especialista arrastando os nós.</li>
  <li>No <a href="/workspace" class="text-brand-500 underline">/workspace</a>, selecione o pipeline e envie uma mensagem — o trace mostra o trajeto pelas 3 camadas.</li>
</ol>
<p class="mt-2"><b>Dica de debugging:</b> se uma interação parece "pular" um agent, abra o trace em <a href="/observability" class="text-brand-500 underline">/observability</a> — provavelmente o agent está como pass-through (sem skill nem prompt customizado, prompt curto ou genérico) e foi ignorado automaticamente para economizar a chamada de LLM.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §5 — Parser SKILL.md Canônico
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's5',
    section: '§5',
    label: 'Parser SKILL.md',
    fundamento: `<p>SKILL.md é o <b>contrato declarativo</b> de um agent. Não é documentação opcional — é um artefato executável que a plataforma carrega em tempo de ativação e usa em pontos específicos da execução.</p>
<p class="mt-2"><b>Seções obrigatórias</b> (o parser reporta erro se faltarem):</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Frontmatter YAML</b> — id, version (semver), kind, owner, stability</li>
  <li><b>Purpose</b> — uma frase: o que essa skill faz</li>
  <li><b>Activation Criteria</b> — quando esta skill deve ser acionada</li>
  <li><b>Inputs</b> — o que a skill espera receber</li>
  <li><b>Workflow</b> — passo-a-passo (vai pro system prompt)</li>
  <li><b>Tool Bindings</b> — tools que essa skill pode chamar (filtra o Permitted Toolset)</li>
  <li><b>Output Contract</b> — schema da resposta (Verifier valida)</li>
  <li><b>Failure Modes</b> — o que fazer quando algo der errado</li>
</ul>
<p class="mt-2">No frontmatter, <code>kind</code> aceita <code>orchestrator</code> (camada Maestro), <code>router</code> (Triagem) ou <code>subagent</code> (Especialista) — note que aqui o Maestro é <code>orchestrator</code>, diferente do agente, onde é <code>aobd</code>.</p>
<p class="mt-2">Validação estrutural ocorre na criação. Hash SHA-256 garante imutabilidade da versão. Versão semver permite evoluir sem quebrar consumidores.</p>`,
    aplicacao: `<p>Defina uma skill uma vez, reaproveite em vários agents:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Comportamento previsível</b> — workflow declarado em vez de prompt solto no agent. Outros desenvolvedores entendem o que a skill faz sem rodar.</li>
  <li><b>Permitted Toolset explícito</b> — só tools listadas em "Tool Bindings" ficam visíveis ao LLM. Mesmo que outras estejam registradas em /mcp, o agent não tem como descobrir.</li>
  <li><b>Failure modes documentados</b> — não é "se der pau, gera 500". É "se input inválido → retorne JSON de erro estruturado; se tool falhar → tente alternativa Y".</li>
  <li><b>Modo de execução</b> — <code>fast</code> / <code>standard</code> / <code>rigorous</code> controla número de iterações de raciocínio e se há verificação de evidência. Há ainda o modo <code>declarative</code>: a skill executa <code>## API Bindings</code> (HTTP) ou <code>## Data Tables</code> (consulta SQL parametrizada) <b>sem chamar o LLM</b> — determinístico e barato.</li>
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
  <li>Aba <b>Preview / Validação</b> mostra a anatomia detectada + o modo de execução inferido (fast/standard/rigorous/declarative).</li>
  <li>Vincule a um agent em <a href="/agents" class="text-brand-500 underline">/agents</a> pelo campo "Skill Vinculada".</li>
</ol>
<p class="mt-2"><b>Pegadinha comum:</b> registrou uma tool em <a href="/mcp" class="text-brand-500 underline">/mcp</a> mas o agent nunca chama? Verifique se ela está listada em "Tool Bindings" do SKILL.md. Tools fora dessa lista são invisíveis ao LLM.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §6 — CAR (Catálogo de Roteadores)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's6',
    section: '§6',
    label: 'CAR — Catálogo de Triagens',
    fundamento: `<p>CAR é a "lista telefônica" que o Maestro consulta para descobrir qual Agent de Triagem chamar quando recebe uma mensagem.</p>
<p class="mt-2">Diferente de um service registry tradicional (que indexa por endpoint), o CAR indexa por <b>intenção</b>: cada entrada amarra um <code>skill_urn</code> a um <code>domain</code> e a uma lista de <code>activation_keywords</code> (palavras-gatilho).</p>
<p class="mt-2"><b>Como o match acontece hoje</b> (estágio único, determinístico):</p>
<ol class="list-decimal pl-4 mt-2 space-y-1.5">
  <li>Filtra as entradas <code>active</code> do mesmo <code>domain</code>.</li>
  <li>Pontua cada entrada: <b>quantas</b> <code>activation_keywords</code> aparecem no texto da intenção, <b>somado</b> ao <code>success_rate</code> histórico da entrada.</li>
  <li>Vence a de maior pontuação. Sem entradas no domínio, cai no primeiro Agent de Triagem (<code>kind: router</code>) ativo.</li>
</ol>
<p class="mt-2"><b>Nota de fidelidade:</b> a tabela <code>car_entries</code> reserva colunas para evolução futura (<code>embedding_vector</code> para ranking semântico, <code>required_entities</code>, <code>actor_profile</code>, <code>jurisdiction</code>, <code>latency_p95</code>, <code>avg_cost</code>), mas o match atual usa só keywords + <code>success_rate</code>. Não há, hoje, ranking vetorial por paráfrase nem desempate automático por estabilidade/custo.</p>`,
    aplicacao: `<p>Use o CAR para:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Roteamento por intenção</b> — em vez de codar IF/ELSE de roteamento, você registra entradas e deixa o Maestro escolher a Triagem pelas palavras-gatilho.</li>
  <li><b>Documentação viva</b> — listar as entradas do CAR (<code>GET /api/v1/car</code>) mostra exatamente quais jornadas a plataforma cobre, por domínio.</li>
  <li><b>Ajuste de cobertura</b> — uma Triagem nunca é escolhida? Amplie suas <code>activation_keywords</code> ou crie uma entrada nova para o domínio.</li>
</ul>
<p class="mt-2"><b>Não usa o CAR:</b> agent invocado direto por outro sistema via API (ex.: <code>POST /api/v1/pipelines/{id}/invoke</code> ou via Fluxograma selado) — esse caminho não passa pela escolha por intenção.</p>`,
    ativar: `<p>CAR é nativo. Para adicionar uma entrada (só estes campos são aceitos pelo schema):</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/car \\
  -H "Content-Type: application/json" \\
  -d '{
    "skill_urn":"faq-cliente@1.0.0",
    "domain":"atendimento",
    "activation_keywords":["faq","duvida","pergunta"],
    "required_entities":[]
  }'</pre>
<p class="mt-2">Listar: <code>GET /api/v1/car?domain=atendimento</code>. Remover: <code>DELETE /api/v1/car/{id}</code>. (Campos como <code>actor_profile</code> não são aceitos no POST — seriam ignorados.)</p>`,
    usar: `<p>O CAR é gerenciado pela API <code>/api/v1/car</code> e serve de catálogo das jornadas que a plataforma reconhece por domínio.</p>
<p class="mt-2"><b>Boa prática:</b> mantenha as <code>activation_keywords</code> específicas o bastante para não colidir entre domínios, mas largas o bastante para cobrir as variações reais de como o usuário pede a mesma coisa. Como a pontuação soma o <code>success_rate</code> da entrada, entradas que historicamente resolvem bem tendem a ser preferidas em empate de keywords.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §7 — Protocolo A2A / Envelope
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's7',
    section: '§7',
    label: 'Protocolo A2A / Envelope',
    fundamento: `<p>Quando um agent fala com outro, a unidade de comunicação é o <b>envelope A2A</b>: uma estrutura tipada que carrega contexto, identidade e limites — não JSON solto na rede.</p>
<p class="mt-2"><b>Campos principais</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><code>envelope_id</code> (UUID) + <code>trace_id</code>/<code>span_id</code>/<code>parent_span_id</code> (UUIDs de correlação)</li>
  <li><code>IntentDescriptor</code> — a intenção do usuário (domínio, entidades, urgência, ator), preservada ao longo da cadeia</li>
  <li><code>skill_ref</code>/<code>target_skill_urn</code> — qual skill a invocação usa</li>
  <li><code>budget_remaining</code> — limites de <code>tokens</code>, <code>wall_ms</code> e <code>usd</code> (campos carregados no envelope)</li>
  <li><code>deadline</code> — quando a invocação caduca</li>
  <li><code>ContextDelta</code> — mutações append-only (cada agent adiciona, não sobrescreve)</li>
  <li><b>Assinatura</b> — <code>sign()</code> é um digest de correlação; a assinatura forte <b>HMAC-SHA256</b> (<code>sign_hmac</code>/<code>verify_hmac</code>) é usada na <b>federação A2A</b> entre instâncias</li>
</ul>`,
    aplicacao: `<p>Onde o envelope brilha:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Federação cross-instância</b> — no egress, o envelope é assinado com HMAC por peer; no ingress, é verificado e protegido contra replay (nonce) antes de executar.</li>
  <li><b>Contexto preservado</b> — o <code>IntentDescriptor</code> e o <code>ContextDelta</code> mantêm a intenção original e o acúmulo de contexto coerentes entre etapas (Maestro → Triagem → Especialista).</li>
  <li><b>Correlação/auditoria</b> — <code>trace_id</code>/<code>span_id</code> permitem amarrar pedaços de uma mesma jornada.</li>
</ul>
<p class="mt-2"><b>Nota de fidelidade:</b> a assinatura HMAC e a verificação só estão ativas no caminho de <b>federação</b> (ver módulo Federação A2A); na delegação local intra-mesh não há, hoje, enforcement de <code>budget</code> nem assinatura por delegação.</p>`,
    ativar: `<p>O envelope é a estrutura de transporte do protocolo A2A. Para listar envelopes persistidos:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">GET /api/v1/history?entity_type=envelopes&limit=20</pre>
<p class="mt-2">A assinatura forte é exercitada no fluxo de <b>Federação A2A</b> (egress assina, ingress verifica). Não existe um endpoint <code>/api/v1/envelopes</code> dedicado.</p>`,
    usar: `<p>Em <a href="/history" class="text-brand-500 underline">/history</a> a UI lista <b>Interações</b>, <b>Turnos</b> e <b>Auditoria</b>; cada turno mostra o <code>envelope_id</code> associado para você correlacionar a comunicação.</p>
<p class="mt-2"><b>Caso de uso típico:</b> investigar uma jornada. Pegue o <code>trace_id</code>/<code>envelope_id</code> em /history e use a API (<code>/api/v1/history?entity_type=envelopes</code>) para inspecionar os envelopes persistidos. Para comunicação entre instâncias, o módulo Federação A2A é onde os envelopes assinados são produzidos e verificados.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // §9.5 — Harness de Avaliação
  // ═════════════════════════════════════════════════════════════════
  {
    id: 's95',
    section: '§9.5',
    label: 'Harness de Avaliação',
    fundamento: `<p>Harness é o "CI/CD de qualidade" da plataforma. Antes de promover uma release para produção, ele roda o agente contra um conjunto de casos curados (Golden Dataset) e decide, por um gate automático, se passou ou não.</p>
<p class="mt-2"><b>Cada caso (gold_case) tem</b>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><code>input_text</code> — a entrada enviada ao agente (campo obrigatório)</li>
  <li><code>expected_output</code> — texto esperado (match por similaridade) OU <code>expected_pattern</code> (regex Python, tem prioridade)</li>
  <li><code>expected_state</code> — a DECISÃO esperada: <code>Recommend</code>, <code>Refuse</code> ou <code>Escalate</code></li>
  <li><code>case_type</code> — <code>normal</code> ou <code>adversarial</code> (adversariais alimentam a taxa de recusa correta)</li>
  <li><code>category</code> (taxonomia), <code>weight</code> (0.1–10, peso na média ponderada), <code>red_flags</code> (strings que NUNCA podem aparecer — útil p/ vazamento de PII)</li>
</ul>
<p class="mt-2"><b>Métricas que o gate avalia</b>: acurácia ponderada, taxa de recusa correta, falso positivo, latência média, e — quando o Verifier (§14.2) está ligado — média de factuality/completeness/tone, safety_violation_rate, contract_compliance_rate e hallucination_rate. Acima dos thresholds → aprovado; abaixo → reprovado, com o motivo registrado em <code>gate_reason</code>.</p>
<p class="mt-2"><b>Pegadinha do decision-state:</b> o FSM (§15) colapsa a decisão no estado terminal <code>LogAndClose</code>, então o estado cru é sempre LogAndClose. O harness recupera a decisão real (Recommend/Refuse/Escalate) do <code>transition_log</code> antes de comparar com o <code>expected_state</code> — sem isso, todo caso reprovaria e a taxa de recusa zeraria.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Baseline antes de promover</b> — qualquer mudança em skill/modelo/prompt passa pelo harness primeiro (<code>run_type=baseline</code>).</li>
  <li><b>Detecção de regressão</b> — <code>run_type=regression</code> compara contra o baseline COMPLETO mais recente do mesmo release e mesmo <code>gold_version</code>. Provider atualizou o modelo sem avisar? O harness pega antes de quebrar produção.</li>
  <li><b>Comparação A/B objetiva</b> — duas execuções lado a lado via <code>GET /api/v1/eval-runs/compare?a=&b=</code>: deltas por métrica e por categoria, com os casos que mudaram de passou↔falhou (regressões primeiro).</li>
  <li><b>Detecção de leak</b> — <code>red_flags</code> com PII/segredos pega vazamento antes de chegar ao cliente.</li>
</ul>`,
    ativar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Vá para <a href="/harness" class="text-brand-500 underline">/harness</a> → painel "Golden Dataset".</li>
  <li>Adicione casos: <code>input_text</code> + <code>expected_output</code> (ou <code>expected_pattern</code>) + <code>expected_state</code> + <code>case_type</code> + <code>red_flags</code>.</li>
  <li>Crie uma release em <a href="/releases" class="text-brand-500 underline">/releases</a>.</li>
  <li>Execute o harness selecionando agente + release + tipo (<code>baseline</code> / <code>regression</code>).</li>
</ol>
<p class="mt-2">As métricas multi-dimensionais (factuality/completeness/tone/safety) só são produzidas quando <code>harness_use_verifier=true</code> E <code>verifier_v2_enabled=true</code> nas Configurações.</p>`,
    usar: `<p>Via API direto:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/eval-runs/execute
{
  "release_id":"...",
  "agent_id":"...",
  "gold_version":"latest",
  "run_type":"baseline"
}</pre>
<p class="mt-2">Resultado traz acurácia (ponderada e bruta), breakdown por categoria, métricas multi-dim e o gate (<code>approved</code>/<code>rejected</code> + <code>gate_reason</code>).</p>
<p class="mt-2"><b>Dica de calibração:</b> comece com Golden Dataset pequeno (10-20 casos cobrindo as jornadas principais + 5 adversariais cobrindo Refuse/Escalate). Refine os thresholds depois de 2-3 releases. Threshold alto demais e ninguém promove nada; baixo demais e bugs passam.</p>`
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
  <li><b>Retriever híbrido</b> — combina dois mundos: <b>BM25</b> (busca textual clássica via Postgres <code>tsvector</code> 'portuguese' + GIN) + <b>vetorial</b> (busca semântica via <b>pgvector</b>, a extensão vetorial do próprio Postgres, alimentada por embeddings).</li>
  <li><b>Reciprocal Rank Fusion (k=60)</b> — funde os dois rankings em um só.</li>
  <li><b>Reranker LLM (opcional)</b> — reordena os top-K por relevância contextual, com justificativa.</li>
</ol>
<p class="mt-2"><b>Embeddings com fallback (v14.0.0):</b> o provider de embeddings tem uma cadeia de resiliência igual à do LLM. O primário é <code>qwen3</code> (open-weight, via hub interno — reusa URL/chave do OSS source); se o endpoint cair, a plataforma migra para <code>azure</code> (<code>text-embedding-3-small</code>) automaticamente e registra <code>event=embedding.fallback</code> no log (auditoria nunca é silenciada). <b>Atenção:</b> qwen3 gera vetores de 1024 dim e Azure de 1536 — a dimensão ativa segue o provider que de fato respondeu, não o configurado.</p>
<p class="mt-2"><b>Importante:</b> RAG ≠ Verificação. O RAG entrega evidências; quem julga se a resposta usou-as bem é o <b>Verifier</b> (§14.2). Use os dois juntos.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>FAQs e bases regulatórias</b> — respostas com citação obrigatória (sem RAG, o LLM "inventa").</li>
  <li><b>Atendimento ao cliente</b> — buscar contratos, manuais, políticas para responder com precisão.</li>
  <li><b>Pipelines que precisam de grounding</b> — qualquer fluxo que não pode tolerar alucinação.</li>
  <li><b>Tabelas (RAG-Tabela)</b> — a mesma base também aceita planilhas promovidas a tabela DuckDB para consulta SQL parametrizada. Detalhes na página <a href="/rag" class="text-brand-500 underline">/rag</a>.</li>
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
<p class="mt-2"><b>Pegadinha do embedder:</b> o embedder padrão é <code>qwen3</code> (1024 dim); o fallback é <code>azure</code> (1536 dim). Ao TROCAR de provider de embeddings, a dimensão do vetor muda — e as queries novas deixam de casar com os vetores antigos. Sempre <b>re-ingira (re-embede) tudo</b> ao trocar de embedder.</p>`,
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
  <li><b>Evidência</b> — <code>knowledge_sources</code>, <code>evidence_chunks</code>, <code>evidences</code> (BM25 nativo Postgres). Os vetores do RAG residem no próprio Postgres via <b>pgvector</b> (extensão + índice HNSW) — backend único desde a Onda Q.</li>
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
    fundamento: `<p>Dois caminhos de observabilidade, independentes e ambos de primeira classe: o stack <b>OpenTelemetry → Tempo (traces) + Loki (logs) + Grafana (UI)</b>, totalmente self-hosted, e o <b>LangFuse</b> (SaaS ou self-hosted). Você escolhe um, o outro, ou os dois.</p>
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
<p class="mt-2"><b>Caminho LangFuse (alternativo ou complementar):</b> configure as credenciais em /settings → Plataforma → LangFuse. Não é "plano B" do OTEL — é um backend de tracing de primeira classe, que pode rodar sozinho ou junto.</p>`,
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
    fundamento: `<p>Uma <b>release</b> é um pacote imutável de configurações: <code>model_config + prompt_config + index_config + policy_config</code>. Você não promove um artefato isolado — promove a release inteira.</p>
<p class="mt-2"><b>Por que o pacote todo?</b> Skill nova só funciona bem com o índice atualizado. Modelo novo só funciona bem com prompt ajustado. Promover artefato isolado = bug em produção.</p>
<p class="mt-2"><b>Ambientes de promoção</b> (campo <code>environment</code>):</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li><b>staging</b> — visível só para devs/testers</li>
  <li><b>canary</b> — fração do tráfego em produção</li>
  <li><b>production</b> — 100% do tráfego</li>
</ol>
<p class="mt-2"><b>Drift hoje:</b> a plataforma persiste eventos em <code>drift_events</code> e os expõe para leitura. A forma operacional de pegar regressão entre versões é rodar o Harness (§9.5) em <code>run_type=regression</code>, que compara contra o baseline do mesmo release e dataset.</p>
<p class="mt-2"><b>Roadmap:</b> detecção estatística automática (KS/PSI para dados, CUSUM para comportamento) e rollback automático por SLO ainda não estão implementados — por ora o monitoramento é por harness + inspeção dos drift_events.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Reprodutibilidade</b> — a release congela o pacote de configuração usado.</li>
  <li><b>Promoção controlada</b> — passe por canary antes de production.</li>
  <li><b>Quality gate integrado</b> — Harness (§9.5) dá o sinal objetivo para promover, em vez de aprovação manual escondendo regressão.</li>
</ul>`,
    ativar: `<p>Sempre ativo. Criar release:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">POST /api/v1/releases
{
  "name":"Atendimento v1.2",
  "environment":"staging",
  "model_config_data":"{}",
  "prompt_config":"{}",
  "index_config":"{}",
  "policy_config":"{}"
}</pre>`,
    usar: `<p>Em <a href="/releases" class="text-brand-500 underline">/releases</a> você lista releases, promove entre ambientes e consulta drift events.</p>
<p class="mt-2">Promover (move <code>environment</code> e <code>status</code> e grava no <code>audit_log</code>):</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">PUT /api/v1/releases/{id}/promote?target_env=canary</pre>
<p class="mt-2"><b>Workflow recomendado:</b></p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Cria release em staging</li>
  <li>Roda harness (baseline) para estabelecer referência</li>
  <li>Promove para canary</li>
  <li>Observa drift_events e métricas; roda harness em regression</li>
  <li>Se tudo OK, promove para production</li>
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
  <li><b>LLM10 Prompt Leak</b> — em traces e logs, o <code>system_prompt</code> aparece como hash + preview de 60 chars (configurável via <code>prompt_leak_preview_chars</code>), não o texto completo.</li>
  <li><b>Auth</b> — senhas em bcrypt, secrets cifrados com <code>cryptography</code>, CSRF opcional (toggle).</li>
</ul>`,
    aplicacao: `<p>Sempre ativo em produção. Você ajusta thresholds via <code>.env</code>:</p>
<ul class="list-disc pl-4 mt-2 space-y-1">
  <li><code>RATE_LIMIT_DEFAULT_PER_MIN=60</code></li>
  <li><code>PROMPT_GUARD_BLOCK_THRESHOLD=0.7</code></li>
  <li><code>DLP_ENABLED=true</code></li>
  <li><code>PROMPT_LEAK_GUARD_ENABLED=true</code></li>
  <li><code>PROMPT_LEAK_PREVIEW_CHARS=60</code></li>
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
  },

  // ═════════════════════════════════════════════════════════════════
  // Estúdio de Pipelines
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'pipeline_studio',
    section: 'Estúdio',
    label: 'Estúdio de Pipelines',
    fundamento: `<p>Um <b>pipeline</b> é um subgrafo do AI Mesh promovido a <b>entidade de 1ª classe</b>: tem nome, domínio, ciclo de vida (rascunho → publicado → aposentado) e um conjunto SELADO de agentes membros (cada agente pertence a no máximo um pipeline).</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Membership exclusiva</b> — o pipeline define a fronteira de execução; a execução não vaza para o mesh global.</li>
  <li><b>Ciclo de vida</b> — só <code>aposentado</code> bloqueia a invocação (na entrada); rascunho e publicado rodam.</li>
  <li><b>Invocável selado</b> — <code>POST /api/v1/pipelines/{id}/invoke</code> roda só dentro do subgrafo do pipeline.</li>
</ul>
<p class="mt-2">O <b>Fluxo de agentes</b> é o editor único — a antiga página "Topologia de conexões" foi aposentada.</p>`,
    aplicacao: `<p>Use pipelines quando um fluxo de vários agentes precisa virar uma unidade — versionada, publicável e invocável por um endpoint só.</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Produto interno</b> — empacote "Análise de crédito" (triagem → especialistas → consolidação) e exponha como 1 chamada.</li>
  <li><b>Governança</b> — publique no Catálogo como <code>kind=pipeline</code>, passe por revisão Root e ganhe métricas de confiabilidade/custo reais.</li>
  <li><b>Reuso</b> — o mesmo pipeline publicado é invocável por Workspace, API e (com federação) por outras orgs.</li>
</ul>`,
    ativar: `<p>Nativo da plataforma. No Fluxograma, painel "Pipelines" → "Novo". Por API:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/pipelines \\
  -H "Content-Type: application/json" \\
  -d '{"name":"Análise de crédito","domain":"credito"}'
# adicionar membros:
curl -X POST /api/v1/pipelines/{id}/agents -d '{"agent_id":"..."}'</pre>`,
    usar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>No <a href="/mesh/flow" class="text-brand-500 underline">Fluxograma</a>, crie um pipeline, arraste os agentes membros e defina o nó <b>Início</b>.</li>
  <li>Conecte os agentes: <b>Sequencial</b> (um alimenta o outro), <b>Paralelo</b> (todos com o mesmo input), <b>Condicional</b> (roteamento 1-de-N por regra) e <b>Padrão/default</b> (o else, quando nenhuma regra casa). Para regras condicionais você não decora sintaxe: <b>descreva em português</b> (a IA traduz), <b>monte por cards</b> com E/OU, e <b>teste no Simulador</b> antes de salvar.</li>
  <li>Clique em <b>Publicar no Catálogo</b> (cria um rascunho de entry <code>kind=pipeline</code>); aprove e publique pela <a href="/catalog" class="text-brand-500 underline">página do Catálogo</a>.</li>
  <li>Invoque <code>POST /api/v1/pipelines/{id}/invoke</code> com <code>{"message":"..."}</code> — use o botão "cURL do invoke" no Fluxograma para copiar o comando pronto.</li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Federação A2A
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'federation',
    section: 'Federação',
    label: 'Federação A2A',
    fundamento: `<p>Federação conecta dois ou mais Maestros (A2A — Agent-to-Agent) de forma assinada e auditada, no modelo provider/consumer:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Provider</b> — publica um manifest em <code>/.well-known/maestro-federation.json</code> e expõe ingress assinado <code>POST /federation/invoke</code> (HMAC + anti-replay + execução selada).</li>
  <li><b>Consumer</b> — registra peers (segredos cifrados), faz <code>sync</code> para puxar manifest + entries remotas e invoca via <code>/federation/remote/{id}/invoke</code> (guarda SSRF).</li>
</ul>
<p class="mt-2"><b>Desligada por padrão</b>; falha fechada (fail-closed) sem <code>MAESTRO_SECRET_KEY</code>. O custo da chamada remota é atestado pelo peer e limitado (clamp) na origem.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Compartilhar com parceiro</b> — exponha um pipeline como capability federada; o parceiro invoca remotamente, auditado.</li>
  <li><b>Consumir de terceiro</b> — descubra capabilities de outra org e use no seu fluxo; o custo fica na origem.</li>
  <li><b>Mesh distribuído</b> — várias instâncias do Maestro colaborando sem expor o mesh interno.</li>
</ul>`,
    ativar: `<p>Só uma coisa vai no <code>.env</code> — a chave-mestra que protege os segredos de peer:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]"># .env — obrigatório (sem ela, fail-closed: a federação responde 503)
MAESTRO_SECRET_KEY=&lt;chave-forte&gt;</pre>
<p class="mt-2"><b>Ligar/desligar é um toggle de runtime</b> (não env var): vive em <code>platform_settings</code> (DB) e é alternado na página <a href="/federation" class="text-brand-500 underline">/federation</a> — perfil <b>root</b>. Por baixo, o botão chama <code>PUT /api/v1/federation/config</code> (grava <code>federation.enabled</code>). Vem <b>desligada por padrão</b>: enquanto OFF, até o manifesto responde 404 (instância invisível).</p>`,
    usar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Em <a href="/federation" class="text-brand-500 underline">/federation</a> (root), ligue a federação e defina o <b>workspace</b> (namespace dos seus URNs).</li>
  <li><b>Provider</b> — publique um <b>pipeline</b> (published + visibilidade <code>company</code>); ele passa a aparecer no seu manifesto e fica invocável, selado ao snapshot.</li>
  <li><b>Consumer</b> — registre o peer (o segredo cifrado aparece em plaintext só uma vez), rode <b>Sync</b> para espelhar as capabilities remotas e invoque a capability federada.</li>
  <li>Cada chamada é <b>assinada (HMAC)</b>, protegida contra replay e <b>auditada</b> (registro do invoke com peer, URN-alvo e execução).</li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Estação de cURL do invoke (autenticação)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'curl_invoke',
    section: 'Integração',
    label: 'cURL do invoke (auth)',
    fundamento: `<p>A "estação de autenticação" do cURL transforma o snippet de invoke num comando pronto para rodar — sem você sair para Configurações criar uma chave na mão.</p>
<p class="mt-2">A plataforma guarda só o <b>hash</b> da API key (o segredo não é recuperável). Por isso o modo recomendado é <b>Gerar e embutir</b>: cria a chave no momento (único instante do plaintext) e injeta no comando, mascarada na tela.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Integração rápida</b> — copie um cURL funcional para Zapier, n8n, script próprio ou app mobile.</li>
  <li><b>Higiene</b> — chave com nome de origem e expiração (90 dias por padrão); a máscara evita vazar o segredo em prints.</li>
  <li><b>Sem segredo no texto</b> — o modo "Placeholder" mantém <code>SUA_API_KEY</code> para docs/CI (chave via variável de ambiente).</li>
</ul>`,
    ativar: `<p>Nativo. As chaves vivem em Configurações → API Keys; a estação reusa o mesmo endpoint <code>POST /api/v1/api-keys</code>.</p>`,
    usar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>No <a href="/mesh/flow" class="text-brand-500 underline">Fluxograma</a>, abra o menu do nó <b>Início</b> → "cURL do invoke".</li>
  <li>Escolha <b>Gerar e embutir</b> (1 clique cria a chave e cola no comando), <b>Chave existente</b> (cola a sua) ou <b>Placeholder</b>.</li>
  <li>Selecione o shell (Bash/PowerShell/CMD) e clique em <b>Copiar</b> — o comando leva o segredo real (mascarado na tela).</li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Catálogo / Marketplace — publicação, governança, trust e custo
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'catalog',
    section: 'Catálogo',
    label: 'Catálogo / Marketplace',
    fundamento: `<p>O Catálogo é o <b>marketplace interno</b> de IA da empresa — pense num "Play Store corporativo". Registra de forma governada tudo que pode ser descoberto e invocado: agents, skills, recipes (composições declarativas), pipelines (grafos do Fluxograma) e plataformas externas aprovadas (ChatGPT, Cursor, Copilot).</p>
<p class="mt-2">Cada item é uma <b>entry</b> com identidade própria (URN = tipo+nome+versão), dono, versão semver e uma <b>Divulgação de Capacidade</b> (a "etiqueta nutricional": o que faz com os dados).</p>
<p class="mt-2"><b>Tipos</b> (<code class="bg-surface-100 px-1 rounded">kind</code>): <code>agent</code>, <code>skill</code>, <code>recipe</code>, <code>external_platform</code> e <code>pipeline</code>.</p>
<p class="mt-2"><b>Lifecycle</b> de uma entry — como o trâmite de um documento que precisa de visto:</p>
<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li><code>draft</code> — você está editando</li>
  <li><code>submitted</code> — enviou para revisão Root</li>
  <li><code>approved</code> — Root aprovou (já pode publicar)</li>
  <li><code>published</code> — disponível para uso</li>
  <li><code>deprecated</code> — marcado para sair (ainda invocável, com aviso)</li>
  <li><code>archived</code> — fora de uso (terminal)</li>
</ol>
<p class="mt-2">Rejeitar ou pedir mudanças não cria status novo: a entry volta para <code>draft</code> para você corrigir e re-submeter.</p>`,
    aplicacao: `<p>O Catálogo paga conta quando a IA deixa de ser experimento de uma pessoa e vira ativo da empresa:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Descoberta com confiança</b> — quem consome vê a etiqueta nutricional ANTES de invocar (processa PII? chama API externa? qual soberania de dados?).</li>
  <li><b>Governança num ponto único</b> — nada vira <code>published</code> sem passar pela Fila de Revisão Root. Pré-verificações automáticas viram insumo da decisão.</li>
  <li><b>Trust REAL, não declarado</b> — confiabilidade, latência p95 e custo médio são calculados das execuções reais, não de uma promessa do autor.</li>
  <li><b>Compliance pronto para auditoria</b> — o Inventário Regulatório responde "quais entries processam dados de saúde?" e exporta CSV para o DPO.</li>
  <li><b>Chargeback</b> — Custo & Consumo mostra quanto cada área gasta em USD por invocação.</li>
</ul>
<p class="mt-2"><b>Quando NÃO precisa:</b> um agent solto que só você usa em teste — crie em <a href="/agents" class="text-brand-500 underline">/agents</a> e pronto. Catálogo é para o que circula entre pessoas/áreas.</p>`,
    ativar: `<p>O Catálogo é nativo — sempre disponível. Para publicar, use o caminho certo conforme o tipo:</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Agent, skill, recipe ou plataforma externa</b>: wizard de 4 passos em <a href="/catalog/publish" class="text-brand-500 underline">/catalog/publish</a> (Artefato → Metadata → Divulgação → Revisão).</li>
  <li><b>Pipeline</b>: publique direto do <a href="/mesh/flow" class="text-brand-500 underline">/mesh/flow</a> (Fluxograma) — o pipeline vira entry <code>kind=pipeline</code> em draft e segue o mesmo lifecycle.</li>
</ul>
<p class="mt-2">Por API, criar e submeter uma entry:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl -X POST /api/v1/catalog/entries \\
  -H "Content-Type: application/json" \\
  -d '{"name":"Analista Fiscal","kind":"agent","version":"1.0.0",
       "artifact_type":"agent","artifact_id":"<id>","visibility":"company"}'
# depois: PUT /api/v1/catalog/entries/{id}/capability  (divulgação)
#         POST /api/v1/catalog/entries/{id}/submit       (vai para fila Root)</pre>`,
    usar: `<p>Páginas do Catálogo (todas sob o menu Catálogo):</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><a href="/catalog" class="text-brand-500 underline">/catalog</a> — vitrine: navega e abre o detalhe de cada entry.</li>
  <li><a href="/catalog/queue" class="text-brand-500 underline">/catalog/queue</a> — Fila de Revisão (só Root): aprova, rejeita ou pede mudanças.</li>
  <li><a href="/catalog/inventory" class="text-brand-500 underline">/catalog/inventory</a> — Inventário Regulatório (só Root): filtros tristate por flag, export CSV.</li>
  <li><a href="/catalog/stewardship" class="text-brand-500 underline">/catalog/stewardship</a> — Curadoria: entries por área responsável; flaga órfãs, paradas (30+ dias) e baixa confiabilidade.</li>
  <li><a href="/catalog/cost" class="text-brand-500 underline">/catalog/cost</a> — Custo & Consumo: agrega por entry/consumer/área/dia + alertas de anomalia.</li>
</ul>
<p class="mt-2"><b>Trust real:</b> na página de detalhe da entry você vê confiabilidade (execuções completas ÷ finalizadas), latência p95 e custo médio — recalculados pelo motor a cada execução real (sandbox e execuções federadas NÃO contam, para não envenenar o número do dono).</p>
<p class="mt-2"><b>Pegadinha comum:</b> publicou v1.2.0 e quer "corrigir" voltando para v1.1.0? Não dá — versão não regride. Suba a versão (v1.2.1). E recipe/pipeline sem steps/grafo não executa: a pré-verificação pega antes.</p>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Plataforma Externa testável (DAST para IA)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'external_platform',
    section: 'Catálogo',
    label: 'Plataforma Externa (testar + DAST)',
    fundamento: `<p>Uma <b>Plataforma Externa</b> é uma IA de terceiro (um endpoint OpenAI-compatível, por exemplo) catalogada no Maestro como entry <code>kind='external_platform'</code> — para ser <b>governada como qualquer outro ativo</b>: descoberta, conexão, teste, prova e auditoria.</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Conexão selada</b> — base_url, modo (<code>openai_chat</code>/<code>http_ping</code>) e segredo cifrado. Toda chamada passa por guarda SSRF + cap de resposta.</li>
  <li><b>Conformidade "DAST para IA"</b> — uma bateria de probes que confronta o comportamento observado com a <b>Divulgação de Capacidade</b> declarada e gera um <b>selo</b>: <code>conforme</code> / <code>parcial</code> / <code>divergente</code>.</li>
</ul>
<p class="mt-2">Pense num "exame de admissão": antes de deixar uma IA terceira entrar nos seus fluxos, você testa disponibilidade, latência, se a autenticação é mesmo exigida, resistência a prompt-injection/jailbreak, vazamento de system prompt e eco de PII.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Homologar um fornecedor de IA</b> — rode a suíte de conformidade e use o selo como critério de aprovação.</li>
  <li><b>Provar a capacidade</b> — dispare o probe configurado como execução sandbox e veja a resposta real do vendor (com custo atestado no seu billing).</li>
  <li><b>Orquestrar híbrido</b> — encadeie a IA externa como <b>step de um recipe</b>, ao lado de agentes do seu mesh.</li>
</ul>`,
    ativar: `<p>Nativo do Catálogo. Crie uma entry <code>kind='external_platform'</code> e configure a conexão na página de detalhe da entry. Os checks heurísticos (injection/jailbreak/system-prompt) só se aplicam ao modo <code>openai_chat</code>.</p>
<p class="mt-2">Atenção: cada check da suíte faz <b>1 chamada real</b> ao vendor — o custo entra no billing do cliente. Só owner/root disparam.</p>`,
    usar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>No <a href="/catalog" class="text-brand-500 underline">Catálogo</a>, abra a entry da Plataforma Externa.</li>
  <li><b>Descobrir</b> (cole a URL — detecta OpenAI-compatível ou instância Maestro) e <b>Conectar</b> (base_url + auth + segredo).</li>
  <li><b>Testar conexão</b> e <b>Provar Capacidade</b> (execução sandbox).</li>
  <li>Rode a <b>Conformidade (DAST)</b> e leia o selo + os checks (determinísticos vs heurísticos são marcados).</li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Ferramentas MCP (/mcp)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'tools_mcp',
    section: 'Integração',
    label: 'Ferramentas MCP',
    fundamento: `<p>Uma <b>ferramenta</b> é uma função externa que o agente invoca durante a conversa ("valida esse CPF", "consulta o ERP"). O Maestro fala com elas pelo <b>MCP (Model Context Protocol)</b>.</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Transportes</b> — <code>HTTP</code> (JSON-RPC, com suporte a MCP Streamable HTTP/SSE) e <code>stdio</code> (processo local: npx/node/python).</li>
  <li><b>Descoberta</b> — o Maestro chama <code>tools/list</code> no servidor e guarda o schema real de cada ferramenta.</li>
  <li><b>Per-tool (gated)</b> — com <code>MCP_PER_TOOL_ENABLED</code> ligado, cada ferramenta vira uma função própria com o schema real (o LLM chama <code>create_issue</code> direto, sem o intermediário <code>{operation, query}</code>).</li>
</ul>
<p class="mt-2">O <b>Permitted Toolset</b> é a interseção entre o que está registrado em /mcp e o que a skill declara em <code>## Tool Bindings</code>. Auth suportada: API Key, OAuth2 (client credentials) e mTLS — sempre cifrada em repouso.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Dar superpoderes ao agente</b> — busca na web, leitura de docs, escrita em sistemas, consulta a bancos.</li>
  <li><b>Schema preciso</b> — ferramentas de argumentos estruturados (GitHub, filesystem) funcionam bem no modo per-tool.</li>
  <li><b>Auditoria</b> — toda chamada vai para <code>tool_calls</code> (args, resposta, latência, erro).</li>
</ul>`,
    ativar: `<p>Registre o servidor em <a href="/mcp" class="text-brand-500 underline">/mcp</a> e declare a ferramenta na skill (<code>## Tool Bindings</code>). O modo per-tool é um <b>toggle de Configurações</b> (<code>MCP_PER_TOOL_ENABLED</code>, default desligado) — sem ele, o caminho legado <code>{operation, query}</code> é idêntico ao de sempre.</p>`,
    usar: `<ol class="list-decimal pl-4 mt-2 space-y-1">
  <li>Em <a href="/mcp" class="text-brand-500 underline">/mcp</a>, cadastre o servidor MCP (HTTP ou stdio) e teste a conexão — a plataforma lista as ferramentas descobertas.</li>
  <li>Na skill, liste a ferramenta em <b>Tool Bindings</b>; o agente passa a poder chamá-la.</li>
  <li>Opcional: ligue o per-tool em Configurações para expor o schema real ao LLM.</li>
</ol>`
  },

  // ═════════════════════════════════════════════════════════════════
  // Saúde dos Modelos (chip do header)
  // ═════════════════════════════════════════════════════════════════
  {
    id: 'model_health',
    section: 'Plataforma',
    label: 'Saúde dos Modelos (chip do header)',
    fundamento: `<p>O <b>chip de Saúde dos Modelos</b> fica no topo de toda tela e responde uma pergunta simples: <b>o que vai ser usado daqui pra frente, e está tudo de pé?</b></p>
<p class="mt-2">Pense num painel de combustível do carro: ele não dirige por você, mas avisa antes de você ficar na mão. A cada papel de roteamento (Tool calling, Raciocínio, Instruct, Classificação, Geração de skill, Multimodal) e também para os <b>Embeddings</b>, a plataforma faz uma <b>sonda de inferência mínima</b> — completa 1 token no modelo de chat, ou embeda um texto curto — e reporta se respondeu, a latência e o erro quando falha.</p>
<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Verde</b> — tudo o que será usado (chat, roteamento e embeddings) respondeu.</li>
  <li><b>Âmbar (com contador)</b> — algo merece atenção: um <b>fallback</b> ativo (ex.: embeddings caíram do provider configurado para o de contingência) OU algum modelo indisponível. O número mostra quantos estão fora.</li>
</ul>
<p class="mt-2">O chip tem só esses dois estados. Para ver o <b>vermelho</b>, abra o painel: cada linha (por papel + Embeddings) fica verde (ok) ou vermelha (indisponível). Modelos repetidos no roteamento são <b>deduplicados</b> (gpt-oss-120b sondado uma vez, não a cada papel), e as sondas rodam em paralelo com timeout curto (~8s) — um endpoint inacessível não trava o header.</p>`,
    aplicacao: `<ul class="list-disc pl-4 mt-2 space-y-1.5">
  <li><b>Antes de uma demo</b> — bata o olho no chip: se estiver âmbar, você descobre o problema antes do público.</li>
  <li><b>Depois de mexer em Configurações</b> — trocou provider/modelo ou roteamento? O chip confirma que a nova escolha responde de fato.</li>
  <li><b>Diagnóstico de "o agente está lento/estranho"</b> — âmbar por fallback de embeddings explica busca semântica degradada; uma linha vermelha em Tool calling explica agente que não chama ferramenta.</li>
</ul>
<p class="mt-2">O resultado é <b>cacheado por ~5 minutos</b> para não sondar a cada carga de página. O custo é baixíssimo: ~1 token por modelo de chat distinto + 1 embedding por ciclo de cache.</p>`,
    ativar: `<p>Nada a ligar — o chip aparece automaticamente no header de todas as páginas.</p>
<p class="mt-2">Para forçar uma nova sondagem (ignorando o cache), o endpoint aceita <code>?force=true</code>:</p>
<pre class="bg-surface-50 p-2 rounded mt-2 text-[10px]">curl 'http://localhost:7000/api/v1/llm/health?force=true' | jq</pre>`,
    usar: `<p>Clique no chip para abrir o detalhamento por papel: provider/modelo resolvido, status e latência. A linha de <b>Embeddings</b> mostra <code>configured</code> (o que você escolheu) x <code>effective</code> (o que de fato respondeu) — quando diferem, o fallback está ativo.</p>
<p class="mt-2">Endpoint por trás: <code>GET /api/v1/llm/health</code> (em <code>app/routes/dashboard.py</code>), implementado em <code>app/core/model_health.py</code>. O roteamento por papel vem de <b>Configurações → Roteamento LLM</b>; o provider de embeddings, de <b>Configurações → Plataforma → Embedding</b>.</p>`
  }
];
