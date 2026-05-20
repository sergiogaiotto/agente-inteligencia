/**
 * Conteúdo de ajuda da plataforma — Schema v2 (reescrita 2026-05).
 *
 * Tom: profissional friendly, sem emojis. Direto, claro, com exemplos
 * concretos. Cada tela explicada do alto (conceito) ao detalhe (campos
 * + pegadinhas) para que qualquer pessoa entenda sem precisar de
 * documentação externa.
 *
 * Schema:
 *
 *   HELP_CONTENT = {
 *     <pageKey>: {
 *       title:    string,        // título do drawer ("Agentes")
 *       summary:  string,        // 1-2 linhas no header (sem HTML)
 *       sections: Section[],     // ordenadas, renderizadas como tabs
 *       related:  string[]       // pageKeys relacionadas (link no rodapé)
 *     }
 *   }
 *
 *   Section = {
 *     kind: 'concept'        // O que é (analogia + 1 parágrafo)
 *         | 'fundamentos'    // Como funciona por baixo
 *         | 'campos'         // Cada campo da tela
 *         | 'casos_de_uso'   // Cenários práticos
 *         | 'exemplo'        // Passo-a-passo concreto
 *         | 'pegadinhas',    // Armadilhas comuns
 *     title: string,         // título da tab
 *     body?: string,         // HTML (para concept/fundamentos/exemplo)
 *     items?: Item[]         // para campos/casos_de_uso/pegadinhas
 *   }
 *
 *   Item depende do kind:
 *     campos      → { name, body, required?, options?, default?, example? }
 *     casos_de_uso→ { title, body }
 *     pegadinhas  → { title, body, severity? }  // severity: 'info'|'warning'|'danger'
 *
 * Backward compat: páginas não migradas para este schema caem no
 * helpContent legado em base.html (estrutura O que é / Fundamento / Como usar).
 */

window.HELP_CONTENT = {

  // ═════════════════════════════════════════════════════════════════
  // /agents — Agentes (PILOTO da reescrita; referência de tom)
  // ═════════════════════════════════════════════════════════════════
  agents: {
    title: 'Agentes',
    summary: 'Onde você cria e gerencia os agentes da plataforma — os trabalhadores que executam tarefas conversando com modelos de linguagem.',

    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Um <strong>agente</strong> no Maestro é uma configuração executável que combina três coisas: <strong>uma instrução</strong> (system prompt), <strong>um modelo de linguagem</strong> que vai responder e, opcionalmente, <strong>ferramentas</strong> que ele pode usar.</p>
          <p>Pense num agente como um colega de trabalho especialista: você descreve o papel dele em texto (system prompt), escolhe o tipo de raciocínio que ele faz melhor (Tool Calling, Reasoning, etc.) e ele passa a estar disponível para ser invocado — sozinho ou dentro de uma cadeia maior.</p>
          <p>Esta tela é onde você cria, edita, lista, duplica e invoca agentes. Cada agente tem versão própria, podendo evoluir sem afetar quem já consome a versão anterior.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Agentes não vivem soltos — eles fazem parte de uma <strong>topologia em 3 camadas</strong>:</p>
          <ul>
            <li><strong>Subagent (SA)</strong> — o nível operacional. Executa uma tarefa específica (responder dúvida fiscal, classificar e-mail, gerar resumo). Cada SA é especialista num pedaço pequeno.</li>
            <li><strong>Router (AR)</strong> — recebe um pedido genérico e decide qual SA é o mais adequado. Pense num supervisor de fila.</li>
            <li><strong>Orchestrator (AOBD)</strong> — coordena múltiplos AR + SA para tarefas compostas. Pense num gerente de projeto.</li>
          </ul>
          <p>A maioria dos agentes que você cria serão <strong>Subagents</strong>. Roteador e Orquestrador são usados quando há complexidade que justifique — não comece por eles.</p>
          <p>Cada invocação de agent passa por uma <strong>máquina de estados</strong> internamente: intake → policy check → execução → verificação → resposta. Isso garante que toda interação tem rastro de auditoria, métricas de custo, e (quando habilitado) verificação de evidência das respostas.</p>
        `
      },
      {
        kind: 'campos',
        title: 'Campos da tela',
        items: [
          {
            name: 'Nome',
            required: true,
            body: 'Como o agente vai aparecer nas listas e logs. Use um nome descritivo — "Agente Fiscal Restituição" é melhor que "agent01". Mude depois sem problema; o ID interno não muda.',
            example: 'Analista Fiscal — Restituição PF'
          },
          {
            name: 'Descrição',
            required: false,
            body: 'Resumo do que o agente faz, em 1-2 frases. Aparece em listas e ajuda outros usuários a decidir se devem usá-lo. Não é a instrução do agente — para isso existe o System Prompt.',
            example: 'Responde dúvidas sobre restituição de IRPF analisando o extrato e calculando o valor estimado.'
          },
          {
            name: 'Mensagem de processing',
            required: false,
            body: 'Texto curto (até 140 chars) que aparece pro usuário enquanto o agent está pensando. Humaniza a espera. Default genérico funciona, mas customizar transmite mais profissionalismo.',
            example: 'Analisando o extrato e cruzando com as regras fiscais...'
          },
          {
            name: 'Tipo (Camada)',
            required: true,
            options: ['Subagente (SA)', 'Roteador (AR)', 'Orquestrador (AOBD)'],
            default: 'Subagente (SA)',
            body: 'Define o papel do agent na topologia. 95% dos casos = Subagente. Use Roteador quando há vários SAs especialistas e você quer decisão automática de qual usar. Orquestrador é para fluxos compostos com múltiplas etapas.'
          },
          {
            name: 'Domínio',
            required: false,
            body: 'Tags de área que esse agent atende (fiscal, jurídico, financeiro, etc.). Usado para filtragem nas listas e para regras de stewardship — usuários de um domínio podem ter visibilidade restrita aos agents do próprio domínio.'
          },
          {
            name: 'Versão',
            required: true,
            default: '1.0.0',
            body: 'Semver simples (major.minor.patch). Use para sinalizar mudanças: incremente minor quando ajustar prompt, major quando mudar comportamento substancialmente. Permite rastrear qual versão respondeu qual interação.',
            example: '1.2.0 (depois de revisar o prompt e adicionar exemplos)'
          },
          {
            name: 'Skill Vinculada (SKILL.md)',
            required: false,
            body: 'Skills são blocos reutilizáveis com instruções estruturadas em Markdown — purpose, workflow, output_contract, tools, etc. Vincular uma skill é como dar ao agent uma "competência" pré-pronta. Sem skill, o agent funciona só com o system prompt direto.'
          },
          {
            name: 'Tipo de Tarefa',
            required: true,
            options: ['Tool Calling', 'Reasoning', 'Instruct', 'Classification'],
            body: 'Define o perfil cognitivo da tarefa, e a plataforma escolhe o modelo de LLM mais adequado automaticamente. Tool Calling para chamadas de função / fluxos com integração externa. Reasoning para texto que exige raciocínio profundo em PT-BR. Instruct para texto + imagens (multimodal). Classification para gerar labels/categorias.',
            example: 'Para classificar um e-mail como "urgente / normal / spam" → Classification.'
          },
          {
            name: 'Temperatura',
            required: true,
            default: '0.7',
            body: 'Controla a "criatividade" do modelo. 0.0–0.3 = determinístico (mesmo input → mesma saída). 0.4–0.8 = equilibrado. 1.0–2.0 = criativo / variado. Para extração de dados ou classificação use baixa; para brainstorm use alta.'
          },
          {
            name: 'System Prompt',
            required: true,
            body: 'A instrução principal do agent — quem ele é, o que faz, como deve se comportar. Pode ser carregada de um "System Prompt salvo" (templates reutilizáveis). Escreva como se estivesse instruindo um colega novo: papel, contexto, restrições, formato esperado da resposta.',
            example: 'Você é um analista fiscal especializado em IRPF. Sua tarefa é..., siga sempre o formato..., nunca invente valores...'
          },
          {
            name: 'Requer evidência',
            required: false,
            default: 'ligado',
            body: 'Quando ligado, o agent precisa basear cada afirmação factual em uma fonte recuperada do RAG (base de conhecimento) ou de uma ferramenta. Reduz alucinação. Desligue só quando o agent não precisa citar fontes (ex: gerador criativo).'
          },
          {
            name: 'Aceita imagens / documentos',
            required: false,
            body: 'Toggles que controlam quais tipos de anexo o agent processa. Ative só quando faz sentido para o caso de uso — habilitar tudo aumenta complexidade e custo. Se o "Tipo de Tarefa" for Instruct, multimodal é automaticamente preferido.'
          }
        ]
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          {
            title: 'Atendimento automatizado — primeiro filtro',
            body: 'Crie um Subagent "Triagem de chamados" que classifica abertura de tickets em categorias (técnico, comercial, financeiro). Tipo de tarefa = Classification, temperatura baixa, sem skill vinculada. Conecte na sua plataforma de atendimento via API.'
          },
          {
            title: 'Analista que cita fontes',
            body: 'Subagent "Consulta de Política" que responde dúvidas dos colaboradores sobre RH com base em documentos internos. Requer evidência ligado, RAG configurado (em /evidence), system prompt enfatizando "responda apenas com base nos documentos recuperados". Sem alucinação.'
          },
          {
            title: 'Composição via Recipe',
            body: 'Em vez de criar um agent gigante, crie 3 agents pequenos: "Extrator de NF", "Validador de CNPJ", "Resumo Final". Depois, no Catálogo, monte um Recipe que invoca os 3 em sequência (chain). Cada agent é simples, testável, reutilizável.'
          },
          {
            title: 'Roteador inteligente',
            body: 'Quando você tem 5+ Subagents especialistas (fiscal, jurídico, RH, TI, financeiro) e quer que o usuário faça uma pergunta única, crie um Router (AR) que recebe a pergunta, identifica o domínio, e delega ao SA certo.'
          }
        ]
      },
      {
        kind: 'exemplo',
        title: 'Exemplo prático',
        body: `
          <p>Vamos criar do zero um agent que <strong>analisa um e-mail de cliente e classifica em "elogio / reclamação / dúvida"</strong>.</p>
          <ol>
            <li>Clique em <strong>Novo Agente</strong> no canto superior direito.</li>
            <li><strong>Nome:</strong> "Classificador de E-mail — Atendimento"</li>
            <li><strong>Descrição:</strong> "Analisa o texto de um e-mail e retorna a categoria — elogio, reclamação ou dúvida."</li>
            <li><strong>Tipo (Camada):</strong> Subagente (SA).</li>
            <li><strong>Domínio:</strong> "atendimento".</li>
            <li><strong>Tipo de Tarefa:</strong> Classification (a plataforma vai escolher um modelo otimizado para classificação).</li>
            <li><strong>Temperatura:</strong> 0.2 (queremos respostas estáveis).</li>
            <li><strong>System Prompt:</strong></li>
          </ol>
          <pre>Você é um classificador de e-mails de atendimento ao cliente. Dado o texto de um e-mail, retorne APENAS UM dos rótulos abaixo, sem explicação adicional:

- elogio
- reclamacao
- duvida

Critérios:
- elogio = cliente expressa satisfação ou agradece.
- reclamacao = cliente expressa insatisfação, problema, frustração.
- duvida = cliente pergunta algo sem expressar julgamento positivo ou negativo.

Se o e-mail tiver múltiplos tons, escolha o predominante.</pre>
          <ol start="9">
            <li>Deixe <strong>Requer evidência</strong> desligado (classificação simples não precisa).</li>
            <li><strong>Salvar.</strong></li>
            <li>Vá para <strong>Workspace</strong>, selecione esse agent e cole um e-mail de teste. Ele deve devolver uma única palavra.</li>
          </ol>
          <p>Pronto. Em 5 minutos você tem um classificador funcionando, versionado, rastreável e invocável via API.</p>
        `
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          {
            title: 'Agent não é Skill',
            severity: 'info',
            body: 'Skill é o "manual" — descrição estruturada de como fazer algo (em Markdown). Agent é o "trabalhador" — combina skill + modelo + parâmetros. Você pode ter 5 agents diferentes usando a mesma skill, cada um com modelo/temperatura diferente.'
          },
          {
            title: 'Tipo de Tarefa não é Provider',
            severity: 'info',
            body: 'Tipo de Tarefa diz o QUE o agent faz (raciocinar, classificar, etc.). A plataforma escolhe o LLM real baseado nesse tipo, lendo o Roteamento configurado em /settings. Você não escolhe "GPT-4" diretamente no agent — escolhe o tipo de tarefa e o roteamento resolve.'
          },
          {
            title: 'Temperatura alta em classificação = caos',
            severity: 'warning',
            body: 'Se o agent é Classification e a temperatura está 1.0, o mesmo e-mail pode receber rótulos diferentes em chamadas seguidas. Mantenha 0.0–0.3 para extração/classificação. Reserve temperatura alta apenas para tarefas onde diversidade é desejada.'
          },
          {
            title: 'System Prompt sem formato definido',
            severity: 'warning',
            body: 'Se você quer que o agent responda em JSON, diga isso explicitamente no system prompt e dê um exemplo. Sem isso, ele pode responder em texto livre e quebrar quem consome o resultado.'
          },
          {
            title: 'Editar agent em produção',
            severity: 'danger',
            body: 'Mudar o system prompt de um agent que está sendo consumido por outros sistemas pode quebrá-los. Quando a mudança é não-trivial, incremente a versão (1.0.0 → 1.1.0) ou crie um agent novo. Quem precisa do comportamento antigo continua usando a versão anterior.'
          }
        ]
      }
    ],

    related: ['skills', 'workspace', 'catalog', 'settings']
  },

  // ═════════════════════════════════════════════════════════════════
  // / — Dashboard (Visão Geral)
  // ═════════════════════════════════════════════════════════════════
  dashboard: {
    title: 'Dashboard',
    summary: 'Painel principal com a saúde da plataforma: agentes, skills, interações e releases — tudo num lugar só.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>O Dashboard é o "raio-X" da plataforma. Ao entrar, você vê de uma vez quantos agentes estão ativos por camada (AOBD/AR/SA), quantas skills estão registradas, quantas interações foram processadas recentemente, releases em produção e o estado dos conectores de API.</p>
          <p>É a primeira tela depois do login — pensada para que oncall, gerente e dev saibam <strong>em 5 segundos</strong> se algo precisa de atenção.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Cada card consulta diretamente tabelas do PostgreSQL — sem cache pesado, sem dashboards externos. Métricas são <strong>quase-tempo-real</strong> (1-2 segundos de latência).</p>
          <p>Há 3 tipos de informação:</p>
          <ul>
            <li><strong>Contadores</strong>: totais de entidades (agents, skills, releases). Servem como sanity check rápido.</li>
            <li><strong>Métricas operacionais</strong>: interações nas últimas 24h, taxa de erros, latência média. Servem para detectar incidentes.</li>
            <li><strong>Atalhos</strong>: cards de ação rápida ("Novo Agente", "Workspace"). Servem para que você não precise navegar via menu.</li>
          </ul>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Início de turno', body: 'Oncall abre o Dashboard primeiro. Se algum número está fora do baseline mental, mergulha na página específica (Quality, Observability, History) para investigar.' },
          { title: 'Visita de stakeholder', body: 'Mostrar a plataforma para alguém da diretoria? O Dashboard cabe na tela inteira e conta a história sem precisar de slides.' },
          { title: 'Ação rápida', body: 'Vai criar um agent novo? O atalho do Dashboard te leva direto, sem precisar achar o item no menu lateral.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Dashboard não substitui Observability', severity: 'info', body: 'O Dashboard é resumo. Para drill-down em traces, custos, drift de modelo, vá em /observability. Não tente entender um incidente só pelo Dashboard.' },
          { title: 'Métricas são "agora", não histórico', severity: 'warning', body: 'O contador de "interações nas últimas 24h" muda toda hora. Para histórico real e tendências, use /quality e /history.' }
        ]
      }
    ],
    related: ['agents', 'workspace', 'quality', 'observability']
  },

  // ═════════════════════════════════════════════════════════════════
  // /skills — Skills (SKILL.md)
  // ═════════════════════════════════════════════════════════════════
  skills: {
    title: 'Skills',
    summary: 'Onde você define competências reutilizáveis — manifestos em Markdown que dizem ao agent o que fazer, com quais ferramentas e em qual formato responder.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Uma <strong>skill</strong> é um arquivo <code>SKILL.md</code> que descreve uma competência: o propósito, o passo-a-passo, quais ferramentas pode chamar, e em qual formato a resposta deve sair.</p>
          <p>Pense numa skill como o "manual de operação" de uma tarefa. Diferentes agents podem usar a mesma skill — cada um com seu modelo, sua temperatura, seu domínio. A skill garante consistência de comportamento.</p>
          <p>O Maestro parseia o SKILL.md em tempo de carregamento e usa cada seção em pontos diferentes da execução: <code>workflow</code> alimenta o prompt, <code>tool_bindings</code> filtra o toolset, <code>output_contract</code> valida o resultado.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>SKILL.md é um <strong>contrato estruturado</strong>, não um documento livre. Tem frontmatter YAML obrigatório (id, version, kind, owner, stability) + seções canônicas.</p>
          <p>Versão é <strong>semver</strong>: incremente minor quando refinar o workflow, major quando mudar comportamento. Permite que agents continuem usando uma versão anterior estável enquanto você itera na próxima.</p>
          <p>Cada skill declara um <strong>Execution Profile</strong> que controla o rigor da execução:</p>
          <ul>
            <li><code>fast</code> — 1 chamada LLM, sem reflexão. Para tarefas simples e rápidas.</li>
            <li><code>standard</code> — 2 chamadas, reflexão se der erro. Default.</li>
            <li><code>rigorous</code> — 3+ chamadas, reflexão sempre, verificação de evidência. Para domínios sensíveis.</li>
          </ul>
        `
      },
      {
        kind: 'campos',
        title: 'Seções do SKILL.md',
        items: [
          { name: 'Frontmatter (YAML)', required: true, body: 'Cabeçalho YAML no topo. Precisa de id, version, kind (orchestrator/router/subagent), owner, stability (alpha/beta/stable/deprecated).', example: '---\\nid: skill-fiscal-irpf\\nversion: 1.2.0\\nkind: subagent\\nowner: equipe-fiscal\\nstability: stable\\n---' },
          { name: 'Purpose', required: true, body: 'Frase única declarando o que a skill faz. Aparece em listas e ajuda outros agents a encontrar a skill certa.' },
          { name: 'Workflow', required: true, body: 'Passo-a-passo do raciocínio. Pode usar markdown rico — listas, código, headings. É o "corpo" da skill e alimenta o prompt.' },
          { name: 'Tool Bindings', required: false, body: 'Lista de ferramentas (MCP servers) que essa skill pode invocar. Tools FORA dessa lista são invisíveis para o LLM, mesmo registradas no /tools.' },
          { name: 'Output Contract', required: false, body: 'Schema esperado da resposta (JSON Schema ou descrição). O Verifier (§14.2) usa para validar antes de entregar. Falha precoce evita resposta ruim para o usuário.' },
          { name: 'Guardrails', required: false, body: 'Regras de comportamento (não inventar números, sempre citar fonte, recusar X). Aparecem no system prompt e são checadas pelo Verifier.' },
          { name: 'Failure Modes', required: false, body: 'O que fazer quando o input é ruim, falta dado ou tool falha. Documenta o "plano B" do agent.' },
          { name: 'Execution Profile', required: false, default: 'auto-inferido', body: 'fast | standard | rigorous. Se omitido, o Maestro infere baseado em outros campos (presença de tools, contract, etc.).' },
        ]
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Skill compartilhada entre agents', body: 'Você tem 3 agents (atendimento, dev, gerência) que precisam consultar a mesma base de conhecimento de RH. Cria UMA skill "Consulta RH" e vincula nos 3 agents. Cada agent pode ter prompt/modelo diferentes; a skill garante coerência.' },
          { title: 'Wizard IA para começar', body: 'Não sabe por onde começar? Use o Wizard IA — ele faz perguntas e gera o SKILL.md base para você editar. Bom para sair do zero rapidamente.' },
          { title: 'Output JSON estrito', body: 'Skill que precisa retornar JSON estruturado: declara o schema no Output Contract. Verifier valida antes de devolver. Quem consome o resultado nunca recebe JSON quebrado.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Tool não declarada = tool invisível', severity: 'warning', body: 'Se você registrou uma tool em /tools mas esqueceu de listar em "Tool Bindings" do SKILL.md, o LLM nunca vai chamar — porque ele nem sabe que existe. Permitted Toolset é interseção registro × declaração.' },
          { title: 'Execution Profile errado dispara custo', severity: 'warning', body: 'Skill simples marcada como "rigorous" faz 3+ chamadas LLM por interação. Se for uma skill de classificação trivial, vire "fast" e economize tokens.' },
          { title: 'Mudar version sem incrementar quebra agents', severity: 'danger', body: 'Editar uma skill estável (v1.0.0) sem mudar a version pode quebrar agents que esperavam o comportamento antigo. Incremente version sempre que mudar comportamento.' }
        ]
      }
    ],
    related: ['agents', 'tools', 'workspace']
  },

  // ═════════════════════════════════════════════════════════════════
  // /workspace — Workspace (execução de interações)
  // ═════════════════════════════════════════════════════════════════
  workspace: {
    title: 'Workspace',
    summary: 'O "chat" onde você invoca um agent ou pipeline e acompanha cada etapa da execução ao vivo.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>O Workspace é onde a plataforma "acontece" — você escolhe um agent (ou pipeline de agents), envia uma mensagem, e vê a resposta gerada com o raciocínio do modelo, ferramentas chamadas, evidências recuperadas e tempo de cada etapa.</p>
          <p>É a interface mais usada no dia-a-dia. Funciona tanto para testar uma skill nova quanto para uso real (atendimento, análise, geração de conteúdo).</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Cada mensagem passa por uma <strong>máquina de estados de 9 estados</strong> chamada FSM de Interação:</p>
          <ol>
            <li><strong>Intake</strong> — recebe e valida o input</li>
            <li><strong>PolicyCheck</strong> — verifica políticas (PII, conteúdo proibido, escopo)</li>
            <li><strong>RetrieveEvidence</strong> — busca documentos relevantes (se RAG ativo)</li>
            <li><strong>DraftAnswer</strong> — LLM gera a resposta</li>
            <li><strong>VerifyEvidence</strong> — Verifier multi-dimensional avalia (§14.2)</li>
            <li><strong>Recommend / Refuse / Escalate</strong> — decisão final</li>
            <li><strong>LogAndClose</strong> — registra tudo</li>
          </ol>
          <p>Toda transição é atômica e auditada. Mesmo que algo falhe no meio, a interação termina em <code>LogAndClose</code> — não há "fim sem log".</p>
          <p>Respostas em JSON são detectadas automaticamente e renderizadas como cards (não como string crua).</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Teste rápido de skill nova', body: 'Acabou de criar uma skill? Vincula a um agent simples, abre o Workspace, manda 3 mensagens-teste. Em 1 minuto você sabe se está respondendo como esperado.' },
          { title: 'Demo para cliente', body: 'Compartilhe a URL do Workspace + ID do agent. O cliente conversa com a plataforma ao vivo. Cada resposta vem com explicação visual (cards, evidências, tempo).' },
          { title: 'Uso real (atendimento, análise)', body: 'Operador de atendimento usa o Workspace para responder dúvidas complexas — agent faz o pesado, operador revisa e envia.' }
        ]
      },
      {
        kind: 'exemplo',
        title: 'Exemplo prático',
        body: `
          <p>Conferindo se um agent fiscal está dando respostas com fonte:</p>
          <ol>
            <li>Acesse <code>/workspace</code>.</li>
            <li>Selecione o agent "Analista Fiscal — Restituição".</li>
            <li>Envie: <em>"Qual o prazo para retificar a DIRPF de 2024?"</em></li>
            <li>Observe no Execution Log: <code>RetrieveEvidence</code> deve trazer 2-3 chunks da base "Manual IRPF 2024".</li>
            <li>A resposta vem com referências (números entre colchetes), e <code>VerifyEvidence</code> mostra factuality 4-5/5.</li>
            <li>Se você ver <code>unsupported_claims</code> preenchido, o agent inventou algo — investigue a skill.</li>
          </ol>
        `
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Resposta lenta ≠ resposta cara', severity: 'info', body: 'Workspace mostra tempo de execução. Skill rigorous faz 3+ chamadas — vai demorar mais. Skill fast é instantânea. Tempo alto é normal se o profile é rigorous.' },
          { title: 'JSON renderizado como card', severity: 'info', body: 'Se a resposta veio em JSON, vai ser renderizada como cards/tabela automaticamente. Para ver o JSON cru, use o toggle no header da mensagem.' },
          { title: 'Refuse não é bug', severity: 'warning', body: 'Se a FSM termina em "Refuse", o agent decidiu não responder (políticas, segurança, escopo). Não é falha — é a guardrail funcionando. Veja PolicyCheck no log para entender o motivo.' }
        ]
      }
    ],
    related: ['agents', 'skills', 'evidence', 'quality']
  },

  // ═════════════════════════════════════════════════════════════════
  // /mesh — AI Mesh (topologia de pipelines)
  // ═════════════════════════════════════════════════════════════════
  mesh: {
    title: 'AI Mesh',
    summary: 'Visualização e desenho da rede de agents — quem chama quem, em que ordem, sob qual condição.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>O AI Mesh é a "rede" de agents. Quando você precisa que uma tarefa passe por mais de um agent (extrair → validar → resumir), aqui é onde você desenha esse fluxo visualmente.</p>
          <p>Cada nó é um agent. Cada aresta é uma chamada. O Maestro executa o fluxo respeitando ordem e dependências.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Três tipos de conexão entre agents:</p>
          <ul>
            <li><strong>Sequencial</strong> — A → B → C. Output de A vira input de B.</li>
            <li><strong>Paralela (fan-out)</strong> — A → (B + C + D). Os 3 rodam ao mesmo tempo com o mesmo input.</li>
            <li><strong>Condicional</strong> — A → B ou C dependendo do resultado de A.</li>
          </ul>
          <p>Agents <strong>pass-through</strong> (sem skill vinculada e sem prompt customizado) são detectados e <strong>pulados</strong> automaticamente. Não desperdiça LLM call em nó que não faz nada.</p>
          <p>Para roteamento inteligente (decidir QUAL agent chamar baseado em intenção do usuário), o AOBD consulta o <strong>CAR</strong> (Catálogo de Roteadores) — não usa o Mesh diretamente. Mesh é para fluxos definidos pelo dev; CAR é para escolha automática.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Pipeline ETL com IA', body: 'Agent 1 extrai dados de um e-mail. Agent 2 valida CNPJ via tool MCP. Agent 3 gera resumo executivo. Sequencial, definido no Mesh, invocado por uma chamada só.' },
          { title: 'Comparar múltiplos modelos', body: 'Fan-out: mesma pergunta para 3 agents idênticos exceto pelo modelo (gpt-4o, claude, sabia-4). Você vê 3 respostas em paralelo para comparar qualidade.' },
          { title: 'Roteamento por idioma', body: 'Agent classificador detecta o idioma. Se PT → agent A. Se EN → agent B. Condicional baseado no resultado do classificador.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Mesh ≠ CAR ≠ Recipe', severity: 'info', body: 'Mesh = pipeline desenhado visualmente (dev define). CAR = catálogo para roteamento automático (AOBD decide). Recipe = composição declarativa no Catálogo (sem dev, declarativo, publicável). 3 conceitos distintos.' },
          { title: 'Pass-through sumiu da execução', severity: 'warning', body: 'Se um agent "desapareceu" do log, provavelmente está pass-through. Adicione skill ou prompt customizado para ativar.' }
        ]
      }
    ],
    related: ['agents', 'catalog', 'workspace']
  },

  // ═════════════════════════════════════════════════════════════════
  // /tools — Ferramentas MCP
  // ═════════════════════════════════════════════════════════════════
  tools: {
    title: 'Ferramentas MCP',
    summary: 'Catálogo de ferramentas externas que os agents podem chamar — APIs, bancos de dados, validadores, sistemas legados.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Uma <strong>tool</strong> é uma função externa que o agent pode invocar durante uma conversa: "valida esse CPF", "consulta o ERP", "envia esse e-mail", "lê esse banco de dados".</p>
          <p>O Maestro fala com tools via <strong>MCP (Model Context Protocol)</strong> — um protocolo padronizado que abstrai o "como" da chamada. Você registra a tool aqui, declara nas skills que podem usar, e o agent ganha a capacidade.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Toda tool tem 3 componentes:</p>
          <ul>
            <li><strong>MCP Server</strong> — endpoint que expõe operações tipadas (schema definido)</li>
            <li><strong>Registro no Maestro</strong> — nome, descrição, endpoint, classificação de sensibilidade</li>
            <li><strong>Declaração na skill</strong> — listada em "Tool Bindings" do SKILL.md</li>
          </ul>
          <p>O <strong>Permitted Toolset</strong> é a interseção entre tools registradas E declaradas na skill. Tools de fora dessa interseção são invisíveis ao LLM — não tem como ele "descobrir" e chamar por engano.</p>
          <p>Toda chamada de tool gera registro em <code>tool_calls</code>: argumentos enviados, resposta, latência, erro. Auditoria total.</p>
        `
      },
      {
        kind: 'campos',
        title: 'Campos do registro',
        items: [
          { name: 'Nome', required: true, body: 'Identificador único da tool. Aparece para o LLM como nome chamável. Use snake_case curto.', example: 'consulta_cnpj' },
          { name: 'Descrição', required: true, body: 'O que essa tool faz, em 1 frase. CRUCIAL — o LLM lê isso para decidir quando chamar. Seja específico.', example: 'Consulta CNPJ na Receita Federal e retorna razão social, situação cadastral e endereço.' },
          { name: 'Endpoint MCP Server', required: true, body: 'URL do servidor MCP. Pode ser stdio, sse ou http.' },
          { name: 'Classificação de sensibilidade', required: true, body: 'Nível de risco se o agent chama errado. low (consulta pública), medium (dado interno), high (dado pessoal/regulado).' },
          { name: 'Schema de argumentos', required: true, body: 'JSON Schema dos parâmetros esperados. O Maestro valida antes de chamar — argumento errado nem sai do agent.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Descrição genérica = tool não chamada', severity: 'warning', body: 'Se a descrição for "Faz consulta", o LLM não sabe QUE consulta. Seja específico — descreva o que a tool faz, em que sistema, e quando faz sentido chamar.' },
          { title: 'High sensitivity em ambiente errado', severity: 'danger', body: 'Tools high (apaga dados, envia e-mail externo) num agent de desenvolvimento podem causar incidentes. Restrinja por ambiente e gate por aprovação.' }
        ]
      }
    ],
    related: ['skills', 'api_connectors', 'agents']
  },

  // ═════════════════════════════════════════════════════════════════
  // /evidence — RAG (Base de Conhecimento)
  // ═════════════════════════════════════════════════════════════════
  evidence: {
    title: 'RAG (Base de Conhecimento)',
    summary: 'Onde você cadastra os documentos que o agent vai consultar para responder com fonte — manuais, regulamentos, FAQs, contratos.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p><strong>RAG</strong> (Retrieval-Augmented Generation) é a camada que dá "conhecimento de mundo" para o agent. Em vez de o LLM "inventar" do treinamento, ele recebe trechos relevantes de documentos que você cadastrou — e responde com base neles.</p>
          <p>Aqui você cadastra <strong>bases de conhecimento</strong> (manuais, políticas, FAQs), faz a ingestão (chunca + indexa), e o agent passa a poder consultar.</p>
          <p>Importante: <strong>RAG só BUSCA</strong>. Quem julga se a resposta usou bem as fontes é o Verifier (§14.2 — veja /quality).</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Busca <strong>híbrida</strong> que combina dois mundos:</p>
          <ul>
            <li><strong>BM25</strong> — busca textual clássica (palavras-chave, exatidão lexical). Usa <code>tsvector</code> + GIN do Postgres.</li>
            <li><strong>Vetorial</strong> — busca semântica (significado, paráfrases). Usa Qdrant + embeddings (Azure ou Qwen3).</li>
          </ul>
          <p>Os dois rankings são fundidos via <strong>Reciprocal Rank Fusion</strong> (k=60). Opcionalmente, um <strong>Reranker LLM</strong> reordena por relevância contextual. As top-N evidências vão para o LLM gerador.</p>
          <p>O processo: <strong>ingestão</strong> (você sobe doc) → <strong>chunking</strong> (quebra em pedaços) → <strong>embedding</strong> (gera vetores) → <strong>indexação</strong> (BM25 + Qdrant) → consultável em tempo real.</p>
        `
      },
      {
        kind: 'campos',
        title: 'Campos do registro de base',
        items: [
          { name: 'Nome', required: true, body: 'Nome legível da base. Aparece nas configurações dos agents.' },
          { name: 'Tipo', required: true, options: ['manual', 'regulatório', 'contratual', 'FAQ'], body: 'Categoria do conteúdo. Útil para filtragem e gating por agent.' },
          { name: 'Confidencialidade', required: true, options: ['publica', 'interna', 'confidencial'], body: 'Nível de acesso. Bases confidenciais só podem ser consultadas por agents autorizados.' },
          { name: 'Domínio', required: false, body: 'Tag de área (fiscal, jurídico, RH). Agents do mesmo domínio têm preferência.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Ingestão lenta = base grande', severity: 'info', body: 'Documentos grandes (>100 páginas) levam tempo para chunkar + embedar. Não cancele no meio. Acompanhe o progresso.' },
          { title: 'RAG sem Verifier = sem rede de proteção', severity: 'warning', body: 'Se você ativa RAG mas não usa Verifier (§14.2), o agent pode citar trecho errado e ninguém percebe. Ative ambos juntos.' },
          { title: 'Embedding model precisa ser consistente', severity: 'danger', body: 'Se você ingere com Azure embeddings e depois muda para Qwen3, as queries não vão casar com os vetores antigos. Re-ingira tudo ao trocar o embedder.' }
        ]
      }
    ],
    related: ['agents', 'quality', 'workspace', 'settings']
  },

  // ═════════════════════════════════════════════════════════════════
  // /quality — Qualidade (Verifier multi-dimensional)
  // ═════════════════════════════════════════════════════════════════
  quality: {
    title: 'Qualidade',
    summary: 'Métricas de qualidade das respostas — cada interação avaliada em 4 dimensões para detectar drift e alucinação.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Aqui você vê a "nota" que a plataforma dá para cada resposta gerada. Não é o usuário avaliando — é um <strong>juiz LLM independente</strong> + um validador de contrato que verificam, antes de devolver, se a resposta:</p>
          <ul>
            <li>tem suporte em evidências (factualidade)</li>
            <li>cobre o que foi pedido (completude)</li>
            <li>respeita tom e guardrails (aderência ao tom)</li>
            <li>não vazou PII nem violou política (segurança)</li>
          </ul>
          <p>Se algum desses critérios falha, a resposta é marcada — e você consegue ver aqui, agregado e por interação.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Cada interação no Workspace, quando <code>VERIFIER_V2_ENABLED=true</code>, gera uma linha em <code>verifications</code> com 4 dimensões (factuality, completeness, tone_adherence, safety) + reasoning do juiz + claims sem suporte.</p>
          <p>Antes do juiz LLM, um <strong>ContractValidator determinístico</strong> roda — sem custo de tokens — para validar JSON Schema declarado no <code>output_contract</code> da skill. Falha aqui evita gastar com o juiz.</p>
          <p>Métricas agregadas (janela 24h/7d/30d/all) ajudam a detectar drift: se factuality caiu de 4.5 para 3.2 nos últimos 7d, algo mudou (skill, modelo, base de evidência).</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Auditoria de incidente', body: 'Cliente reclamou de resposta errada. Você vai em /quality, busca pela interação, vê a nota — factuality 1/5 com unsupported_claims preenchido. Aí você corrige a skill ou a base de evidência.' },
          { title: 'Detecção de drift', body: 'Acompanhe a métrica agregada semanal. Queda súbita em uma dimensão = algo mudou. Pode ser troca de modelo, atualização de skill, ou mudança em RAG.' },
          { title: 'A/B de juízes', body: 'Quer testar se um juiz menor (mais barato) dá resultados parecidos? Configure VERIFIER_JUDGE_MODEL e compare métricas entre janelas.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Verifier desligado = página vazia', severity: 'info', body: 'Se VERIFIER_V2_ENABLED=false, nada vai aparecer aqui. Ative antes de esperar dados.' },
          { title: 'Self-preference do juiz', severity: 'warning', body: 'Juiz e gerador do mesmo modelo (gpt-4o avaliando gpt-4o) tende a dar nota alta. Use um juiz diferente para reduzir esse viés.' }
        ]
      }
    ],
    related: ['evidence', 'workspace', 'harness', 'observability']
  },

  // ═════════════════════════════════════════════════════════════════
  // /api-connectors — API Connectors
  // ═════════════════════════════════════════════════════════════════
  api_connectors: {
    title: 'API Connectors',
    summary: 'Catálogo de APIs externas que os agents podem chamar via HTTP — com builder visual de requests e histórico.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Aqui você registra <strong>APIs externas</strong> (CRM, ERP, plataforma de e-mail, API de meteorologia, etc.) para que os agents possam invocar via HTTP. Cada API tem base URL, autenticação e endpoints organizados por categoria.</p>
          <p>Inclui um <strong>Request Builder</strong> visual: você escolhe o endpoint, preenche parâmetros, dispara e vê a resposta — útil para testar antes de codar.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>API Connectors complementam o <strong>Tool Registry</strong> (/tools). A diferença:</p>
          <ul>
            <li><strong>/tools (MCP)</strong> — protocolo padronizado, schema tipado, agent chama via descrição declarada na skill.</li>
            <li><strong>/api-connectors (HTTP)</strong> — chamada HTTP direta, mais flexível, melhor para integrações ad-hoc ou testes.</li>
          </ul>
          <p>Suporta 4 tipos de autenticação: <code>none</code>, <code>api_key</code> (header ou query), <code>bearer</code>, <code>basic</code>.</p>
          <p>Health check periódico mostra se cada connector está vivo. Histórico de chamadas registra método, URL, status, latência e body — para auditoria.</p>
        `
      },
      {
        kind: 'campos',
        title: 'Campos do connector',
        items: [
          { name: 'Nome', required: true, body: 'Nome legível. Aparece em listas e no builder.' },
          { name: 'Base URL', required: true, body: 'Raiz da API. Endpoints concatenam path relativo a essa base.', example: 'https://api.viacep.com.br' },
          { name: 'Tipo de auth', required: true, options: ['none', 'api_key', 'bearer', 'basic'], body: 'Como o connector autentica. Determina os campos extras que aparecem.' },
          { name: 'Endpoints', required: false, body: 'Lista de paths organizados em árvore por categoria. Cada endpoint tem método (GET/POST/...) + path + parâmetros.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'API key no body do connector é insegura', severity: 'danger', body: 'Nunca cole api keys em campos não-protegidos. Use o campo "API Key" dedicado — ele criptografa em repouso.' },
          { title: 'Health check 200 ≠ API funcional', severity: 'info', body: 'Health check só verifica que o servidor responde. Para validar que a operação que você precisa funciona, use o Request Builder.' }
        ]
      }
    ],
    related: ['tools', 'agents', 'workspace']
  },

  // ═════════════════════════════════════════════════════════════════
  // /harness — Harness de Avaliação
  // ═════════════════════════════════════════════════════════════════
  harness: {
    title: 'Harness',
    summary: 'Motor de avaliação que roda a skill contra um Golden Dataset e decide se a release vai para produção ou não.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Antes de promover uma release para produção, o Harness roda os <strong>Golden Cases</strong> (casos curados de teste) contra a versão candidata e produz um relatório: acurácia, latência, custo, falha em casos adversariais. Se passa nos thresholds, libera. Se não, bloqueia.</p>
          <p>É o "CI/CD de qualidade" da plataforma. Sem isso, você está apostando que a mudança no prompt não quebrou nada.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Golden Dataset (§9.4) é <strong>versionado</strong>, <strong>estratificado por jornada</strong>, com proporção mínima de <strong>casos adversariais</strong>. Cada caso tem:</p>
          <ul>
            <li><code>category</code> — taxonomia (ex: "consulta-irpf", "spam")</li>
            <li><code>weight</code> — peso na média ponderada (casos críticos pesam mais)</li>
            <li><code>expected_pattern</code> — regex que o output deve casar</li>
            <li><code>red_flags</code> — strings que NÃO podem aparecer (falham o caso)</li>
          </ul>
          <p>Gate automático: se acurácia ponderada ≥ threshold E adversarial recusa ≥ X E latência p95 ≤ Y → aprovado.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Antes de promover release', body: 'Você refinou um prompt. Roda o harness — em 5 min sabe se quebrou os casos críticos.' },
          { title: 'Regressão semanal', body: 'Rotina automatizada roda o harness toda semana contra a versão de produção. Detecta degradação por mudança no modelo do provider (ex: provider atualizou silenciosamente).' },
          { title: 'A/B de modelos', body: 'Mesma skill, 2 releases (modelo A vs B). Compara acurácia, latência, custo. Decisão informada por dados.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Golden pequeno = teste fraco', severity: 'warning', body: 'Harness com 5 casos não diz quase nada. Comece com 20+ casos cobrindo as principais jornadas + 5+ adversariais.' },
          { title: 'Threshold alto demais = ninguém promove nada', severity: 'warning', body: 'Se você exige 95% e ninguém passa, vai promover manualmente — ignorando o gate. Calibre o threshold com dados de produção.' }
        ]
      }
    ],
    related: ['releases', 'quality', 'agents', 'skills']
  },

  // ═════════════════════════════════════════════════════════════════
  // /releases — Version Registry
  // ═════════════════════════════════════════════════════════════════
  releases: {
    title: 'Releases',
    summary: 'Versionamento atômico de configurações — promover, monitorar drift, fazer rollback.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Uma <strong>release</strong> é um pacote imutável de configurações: versão do modelo, versão da skill, versão do índice RAG, política de uso, tudo congelado num snapshot identificado.</p>
          <p>Em vez de promover artefatos isolados (atualizei a skill mas o índice está velho?), você promove a release inteira. Garante consistência.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Estágios de promoção:</p>
          <ul>
            <li><strong>staging</strong> — visível só para devs/testers</li>
            <li><strong>canary</strong> — 1-10% do tráfego em produção</li>
            <li><strong>production</strong> — 100% do tráfego</li>
          </ul>
          <p>Drift é monitorado contra baseline da release anterior (KS, PSI para dados; CUSUM para comportamento). Quando SLOs são violados, rollback é automático.</p>
        `
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Direto para production = roleta russa', severity: 'danger', body: 'Pular canary economiza 10 minutos e pode custar horas de incidente. Sempre passe por canary.' },
          { title: 'Drift sem baseline = drift cego', severity: 'warning', body: 'Primeira release não tem baseline para comparar. Estabeleça com 1-2 semanas de produção antes de comparar.' }
        ]
      }
    ],
    related: ['harness', 'observability', 'quality']
  },

  // ═════════════════════════════════════════════════════════════════
  // /observability — Observabilidade
  // ═════════════════════════════════════════════════════════════════
  observability: {
    title: 'Observabilidade',
    summary: 'Traces, custos e performance — todas as métricas detalhadas em LangFuse ou stack OTEL.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Camada de tracing-first. Cada interação propaga <strong>W3C Trace Context</strong>, gera spans por camada (AOBD → AR → SA), registra prompt efetivo, modelo usado, output, custo em tokens, latência por etapa.</p>
          <p>Use para investigar incidentes em profundidade, comparar custo entre modelos, debug de latência.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Default: <strong>LangFuse</strong> (SaaS ou self-hosted). Cada interação gera um trace com spans hierárquicos. Dashboards canônicos: Domain Health, Skill Drift, Evidence Quality.</p>
          <p>Para self-hosted puro, dá pra exportar via OpenTelemetry para Tempo/Jaeger.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Investigar latência alta', body: 'Interação demorou 30s. Abre o trace, vê que RetrieveEvidence ocupou 25s — base RAG está lenta. Investiga índice.' },
          { title: 'Comparar custo entre modelos', body: 'Dashboard de custo por modelo: descobre que substituir gpt-4o por gpt-4o-mini em 1 skill específica corta 60% do custo sem perda perceptível.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'LangFuse não configurado = sem traces', severity: 'warning', body: 'Cole as credenciais em /settings antes de esperar dados.' }
        ]
      }
    ],
    related: ['quality', 'settings', 'history']
  },

  // ═════════════════════════════════════════════════════════════════
  // /infra — Infraestrutura
  // ═════════════════════════════════════════════════════════════════
  infra: {
    title: 'Infraestrutura',
    summary: 'Estado dos componentes da plataforma — banco, cache, vetores, observabilidade — em uma tela só.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>A Infraestrutura mostra a saúde dos serviços de baixo nível que a plataforma usa: PostgreSQL (dados), Redis (rate-limit + cache), Qdrant (vetores do RAG), e o stack de observabilidade quando ativo.</p>
          <p>É o lugar para investigar "por que está lento?" ou "por que essa página não carregou?" — antes de mergulhar em traces específicos no /observability.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Os componentes monitorados:</p>
          <ul>
            <li><strong>PostgreSQL</strong> — backend único de persistência. Tudo passa por aqui: agents, skills, interactions, audit_log, catalog, etc. Se cair, a plataforma toda fica indisponível.</li>
            <li><strong>Redis</strong> — usado para rate-limit (sliding window) e cache leve de routing. Falha = rate-limit desligado (failsafe open).</li>
            <li><strong>Qdrant</strong> — vector DB para o RAG. Falha = busca de evidência cai em BM25-only (degrada com graça).</li>
            <li><strong>OpenTelemetry / LangFuse</strong> — exportação de traces. Quando configurado e ativo, traces aparecem em Tempo/Grafana ou LangFuse.</li>
          </ul>
          <p>Status verificado em tempo real via probes leves (latência de query, ping, health endpoint). Quando algum componente está degradado, o card pisca em laranja/vermelho.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Diagnóstico inicial de incidente', body: 'Site lento. Antes de pedir trace, vai em Infra: vê PostgreSQL com latência 2s/query. Foco vira tuning do banco, não da app.' },
          { title: 'Confirmar setup', body: 'Acabou de configurar OTEL? Abre Infra para confirmar que o exporter está conectado e enviando spans.' },
          { title: 'Capacity planning', body: 'Acompanhar tamanho do banco, uso de Redis, vetores indexados em Qdrant. Quando um deles passa do limite saudável, é hora de escalar.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Componente "offline" pode ser config', severity: 'info', body: 'Se LangFuse aparece offline, primeiro checa se as credenciais estão preenchidas em /settings. "Offline" pode significar "nunca configurado".' },
          { title: 'Qdrant down ≠ plataforma down', severity: 'warning', body: 'Quando Qdrant cai, a plataforma continua funcionando mas o RAG perde a parte vetorial. Busca passa a ser BM25-only. Investigue antes que o cliente perceba qualidade pior.' }
        ]
      }
    ],
    related: ['observability', 'settings', 'history']
  },

  // ═════════════════════════════════════════════════════════════════
  // /history — Histórico
  // ═════════════════════════════════════════════════════════════════
  history: {
    title: 'Histórico',
    summary: 'Log unificado de tudo que aconteceu — interações, turnos, envelopes A2A, auditoria.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Consulta unificada de eventos persistidos. Use para investigação, auditoria, reproduzir um bug, ou rastrear quem fez o quê.</p>
          <p>Diferentes das outras telas que mostram <strong>agregados</strong>, aqui você vê <strong>linhas individuais</strong> — uma por interação, turno, envelope ou ação auditada.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Tudo é persistido em PostgreSQL, append-only (auditoria é imutável). Tabelas principais: <code>interactions</code>, <code>turns</code>, <code>envelopes</code>, <code>audit_log</code>.</p>
          <p>Cada linha mantém <code>trace_id</code> para correlação cruzada com /observability.</p>
        `
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Volume cresce rápido', severity: 'info', body: 'Em produção, audit_log e interactions crescem em GB/mês. Configure retenção e archive antigo se necessário.' }
        ]
      }
    ],
    related: ['observability', 'workspace', 'quality']
  },

  // ═════════════════════════════════════════════════════════════════
  // /settings — Configurações
  // ═════════════════════════════════════════════════════════════════
  settings: {
    title: 'Configurações',
    summary: 'Credenciais de provedores, roteamento de LLM, modelo primário, prompts salvos, usuários e API keys — tudo em um lugar.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Central de configurações da plataforma. Dividida em 5 abas: <strong>Plataforma</strong> (credenciais de providers + modelo primário + GPT-OSS + Qwen3 embedding), <strong>Roteamento LLM</strong> (mapa task_type → modelo), <strong>System Prompts</strong> (templates reutilizáveis), <strong>Usuários</strong> (gestão de contas, root), <strong>API Keys</strong> (chaves para acesso externo).</p>
          <p>Mudanças são aplicadas em runtime — sem restart. <code>apply_settings_to_env()</code> invalida caches e re-resolve.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Settings persistem em <code>platform_settings</code> (PostgreSQL key-value). Cada chave UI mapeia para uma env var canônica (azure_key → AZURE_OPENAI_API_KEY, etc.). Boot da app lê env vars; ao salvar via UI, banco sobrescreve env em runtime.</p>
          <p>Ordem de precedência de modelo (4 níveis):</p>
          <ol>
            <li><strong>task_type</strong> declarado no agent → Roteamento LLM resolve</li>
            <li><strong>snapshot</strong> do agent (provider/model setados na criação)</li>
            <li><strong>Modelo Primário</strong> (fallback global)</li>
            <li><strong>azure/gpt-4o</strong> hardcoded (último recurso)</li>
          </ol>
        `
      },
      {
        kind: 'campos',
        title: 'O que você configura em cada aba',
        items: [
          { name: 'Plataforma > Modelo Primário', body: 'Provider + modelo padrão da plataforma — usado quando agent não declara task_type nem snapshot próprio. Definir aqui é mais limpo que editar cada agent.', example: 'gpt-oss-120b + openai/gpt-oss-120b' },
          { name: 'Plataforma > Azure OpenAI', body: 'API key, endpoint, api_version, deployments de chat e embeddings. Provider primário do projeto.', required: false },
          { name: 'Plataforma > Maritaca / Ollama / GPT-OSS', body: 'Outros providers OpenAI-compatible. Cada um com URL/key/model próprios. GPT-OSS suporta 2 sizes (20B/120B) com endpoints separados.' },
          { name: 'Plataforma > Embedding', body: 'Selector Azure | Qwen3. Qwen3 reusa URL do OSS-20B ou OSS-120B (só muda path).' },
          { name: 'Roteamento LLM', body: 'Mapa: tool_calling/reasoning/instruct/classification → provider/modelo. Define qual LLM cada "tipo de tarefa" usa. Configurar uma vez e todos os agents com task_type ficam alinhados.' },
          { name: 'System Prompts', body: 'Templates de system prompts reutilizáveis. Quando criar um agent, você pode escolher um template salvo em vez de escrever do zero.' },
          { name: 'Usuários', body: 'Gestão de contas (root only). Roles: root (admin total), comum (uso normal), admin (gestão sem credenciais).' },
          { name: 'API Keys', body: 'Geração de chaves de API para acesso externo. Cada chave tem nome, escopo e data de expiração.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Mudar Roteamento muda todos os agents com task_type', severity: 'warning', body: 'Configuração em "Roteamento LLM" afeta todos os agents que usam aquele task_type. Não é específica de um agent. Mude com cuidado em produção.' },
          { title: 'Modelo Primário só vale para agents SEM task_type', severity: 'info', body: 'Se o agent declara task_type, o Roteamento ganha. Primário é só fallback para agents legacy ou sem declaração.' },
          { title: 'API key pública é vazamento', severity: 'danger', body: 'Não cole API keys em código frontend, repositórios públicos, ou logs. Use o gerador em "API Keys" e proteja como senha.' }
        ]
      }
    ],
    related: ['agents', 'observability', 'evidence']
  },

  // ═════════════════════════════════════════════════════════════════
  // /catalog — Catálogo (Marketplace corporativo)
  // ═════════════════════════════════════════════════════════════════
  catalog: {
    title: 'Catálogo',
    summary: 'Marketplace interno de agents, skills, recipes e plataformas externas — com governança, capability disclosure e versionamento.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>O Catálogo é onde a empresa registra <strong>todas</strong> as soluções de IA disponíveis: agents internos, skills, recipes (composições), e até plataformas externas aprovadas (ChatGPT, Cursor, Copilot).</p>
          <p>Cada entry tem dono, versão, etiqueta nutricional (o que faz, com quais dados), e passa por revisão Root antes de ser publicada. Pense num "Play Store corporativo" de IA.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Lifecycle da entry:</p>
          <ol>
            <li><strong>draft</strong> — você criou, ainda em edição</li>
            <li><strong>submitted</strong> — enviou para revisão Root</li>
            <li><strong>approved</strong> — Root aprovou (pode publicar)</li>
            <li><strong>published</strong> — disponível para uso</li>
            <li><strong>deprecated</strong> — marcado para remoção</li>
            <li><strong>archived</strong> — fora de uso</li>
          </ol>
          <p>Tipos (<code>kind</code>): <code>agent</code>, <code>skill</code>, <code>recipe</code> (composição declarativa), <code>external_platform</code> (ChatGPT/etc).</p>
          <p><strong>Capability Disclosure</strong> (etiqueta nutricional R6.3) é obrigatório: 12 flags + soberania de dados + retenção. Quem consome o agent sabe exatamente o que ele faz com os dados.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Publicar agent para a empresa toda', body: 'Você criou um agent que valida cálculo de hora extra. Cria entry kind=agent, declara capability disclosure (processa dados pessoais, não treina, retém 30 dias), submete. Root aprova. Agora qualquer área pode invocar.' },
          { title: 'Recipe que encadeia 3 agents', body: 'Em vez de um agent gigante, cria 3 pequenos no Catálogo (Extrator, Validador, Resumo) e um Recipe que invoca em sequência. Reutilizável.' },
          { title: 'Cadastrar ChatGPT Plus', body: 'A empresa aprovou ChatGPT Plus para uso geral? Cria entry kind=external_platform, declara vendor + contrato vigente + casos de uso aprovados. Inventário regulatório fica completo.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Capability Disclosure incompleta = submit reprovado', severity: 'warning', body: 'Pré-check obriga disclosure. Se você marcar "stores_input" mas não preencher retention_days, Root rejeita.' },
          { title: 'Versão não pode regredir', severity: 'danger', body: 'Você publicou v1.2.0. Não pode publicar v1.1.0 depois. Sempre incremente.' },
          { title: 'Recipe sem steps = não executa', severity: 'info', body: 'Criou entry kind=recipe mas esqueceu de declarar os steps? Pré-check vai pegar. Use a aba "Recipe Steps" da página de detalhe.' }
        ]
      }
    ],
    related: ['agents', 'skills', 'catalog_cost']
  },

  // ═════════════════════════════════════════════════════════════════
  // /catalog/cost — Custo & Consumo
  // ═════════════════════════════════════════════════════════════════
  catalog_cost: {
    title: 'Custo & Consumo',
    summary: 'Painel de custo real por invocação — agregado por entry, consumer, departamento ou dia, com alertas automáticos de anomalia.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Aqui você vê quanto cada agent/recipe está custando em USD, por quem está sendo invocado, em qual departamento, em que dia. Base para <strong>chargeback interno</strong> (cada área paga o que usa) e para <strong>controle de orçamento</strong>.</p>
          <p>Custo real é calculado por <code>tokens × pricing do modelo</code> — sem placeholder, sem estimativa.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Cada invocação de recipe (ou registro manual via API) grava uma row em <code>catalog_costs</code>: entry_id, consumer_user_id, consumer_department, cost_usd, tokens_used, latency_ms, invoked_at.</p>
          <p>Agregação em runtime por group_by (entry, consumer, department, day). Filtro por janela de data, entry, consumer, departamento.</p>
          <p>Scope automático: Root vê tudo; demais veem só o próprio consumo. Configurável via dropdown.</p>
          <p><strong>Anomalias</strong> são detectadas em background: pico (hoje ≥ 3× média 7d) e limite global (hoje > $100). Banner vermelho aparece automaticamente quando há anomalia ativa.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Fechamento mensal', body: 'FinOps abre /catalog/cost, filtra mês passado, agrupa por department, exporta CSV. Repasse de custos pronto.' },
          { title: 'Detectar runaway agent', body: 'Banner vermelho de anomalia aparece. Pico de 5× a média = algum agent rodando em loop. Drill-down identifica o culpado em minutos.' },
          { title: 'Decisão de modelo', body: 'Filtra um agent específico, vê o custo médio por invocação. Se está caro, troca o task_type para um modelo mais barato e compara nas semanas seguintes.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Sandbox não conta', severity: 'info', body: 'Execuções marcadas is_sandbox=true (botão 🧪 Sandbox) NÃO gravam em catalog_costs. Bom para testes — não polui chargeback.' },
          { title: 'Modelo desconhecido = cost_usd=0', severity: 'warning', body: 'Se o provider/model do agent não está na tabela de pricing (app/core/llm_pricing.py), o custo é gravado como 0 + WARNING no log. Atualize a tabela quando provider novo aparecer.' },
          { title: 'Custo de tool não conta aqui', severity: 'info', body: 'catalog_costs é só LLM. Custo de tools externas (API calls cobradas) entra em outro lugar — verifique a integração específica.' }
        ]
      }
    ],
    related: ['catalog', 'observability', 'settings']
  }
};
