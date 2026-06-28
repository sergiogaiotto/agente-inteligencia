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
          <p>Agentes não vivem soltos — eles fazem parte de uma <strong>rede em 3 camadas</strong>:</p>
          <ul>
            <li><strong>Especialista</strong> — o nível operacional. Executa uma tarefa específica (responder dúvida fiscal, classificar e-mail, gerar resumo). Cada Especialista cuida de um pedaço pequeno.</li>
            <li><strong>Triagem</strong> — recebe um pedido genérico e decide qual Especialista é o mais adequado. Pense num supervisor de fila.</li>
            <li><strong>Maestro</strong> — coordena múltiplas Triagens + Especialistas para tarefas compostas. Pense num gerente de projeto.</li>
          </ul>
          <p>A maioria dos agentes que você cria serão <strong>Especialistas</strong>. Triagem e Maestro são usados quando há complexidade que justifique — não comece por eles.</p>
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
            options: ['Especialista', 'Triagem', 'Maestro'],
            default: 'Especialista',
            body: 'Define o papel do agent na topologia. 95% dos casos = Especialista. Use Triagem quando há vários Especialistas e você quer decisão automática de qual usar. Maestro é para fluxos compostos com múltiplas etapas.'
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
            name: 'Consultar bases de conhecimento (RAG / Tabelas)',
            required: false,
            default: 'ligado',
            body: 'Quando ligado, o agent BUSCA nas bases vinculadas (RAG/Tabelas em /rag) antes de responder. Desligue para agents sem base própria — classificadores ou criativos. Atenção: este toggle controla o RETRIEVAL, não a recusa. A recusa por falta de evidência é a política global "Exigir evidências" em /settings.'
          },
          {
            name: 'Permitir conhecimento geral do LLM',
            required: false,
            default: 'desligado',
            body: 'Escape hatch do princípio grounded-by-default. Por padrão (desligado), o agent responde EXCLUSIVAMENTE com base em evidências (RAG/Tabelas, anexos, contexto de pipeline ou resultado de ferramentas) — sem nenhuma fonte, ele recusa em vez de inventar. Ative apenas para agents criativos/generalistas que PODEM usar o conhecimento paramétrico do modelo.'
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
            body: 'Crie um Especialista "Triagem de chamados" que classifica abertura de tickets em categorias (técnico, comercial, financeiro). Tipo de tarefa = Classification, temperatura baixa, sem skill vinculada. Conecte na sua plataforma de atendimento via API.'
          },
          {
            title: 'Analista que cita fontes',
            body: 'Especialista "Consulta de Política" que responde dúvidas dos colaboradores sobre RH com base em documentos internos. Ative "Consultar bases de conhecimento (RAG/Tabelas)", configure a base em /rag, e escreva um system prompt enfatizando "responda apenas com base nos documentos recuperados". Com a política global "Exigir evidências" ligada (/settings) e sem "Permitir conhecimento geral", o agent recusa em vez de inventar.'
          },
          {
            title: 'Composição via Recipe',
            body: 'Em vez de criar um agent gigante, crie 3 agents pequenos: "Extrator de NF", "Validador de CNPJ", "Resumo Final". Depois, no Catálogo, monte um Recipe que invoca os 3 em sequência (chain). Cada agent é simples, testável, reutilizável.'
          },
          {
            title: 'Triagem inteligente',
            body: 'Quando você tem 5+ Especialistas (fiscal, jurídico, RH, TI, financeiro) e quer que o usuário faça uma pergunta única, crie uma Triagem que recebe a pergunta, identifica o domínio, e delega ao Especialista certo.'
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
            <li><strong>Tipo (Camada):</strong> Especialista.</li>
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
          <p>O Dashboard é o "raio-X" da plataforma. Ao entrar, você vê de uma vez quantos agentes estão ativos por camada (Maestro/Triagem/Especialista), quantas skills estão registradas, quantas interações foram processadas recentemente, releases em produção e o estado dos conectores de API.</p>
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
          { name: 'Activation Criteria', required: true, body: 'Quando esta skill deve ser acionada (a "porta de entrada"). O parser exige esta seção — sem ela, a validação falha.' },
          { name: 'Inputs', required: true, body: 'O que a skill espera receber. Seção obrigatória no parser. Quando declara o schema dos argumentos, ele tem prioridade sobre o que vem descoberto das tools.' },
          { name: 'Workflow', required: true, body: 'Passo-a-passo do raciocínio. Pode usar markdown rico — listas, código, headings. É o "corpo" da skill e alimenta o prompt.' },
          { name: 'Tool Bindings', required: true, body: 'Lista de ferramentas (MCP servers) que essa skill pode invocar. Tools FORA dessa lista são invisíveis para o LLM, mesmo registradas no /mcp.' },
          { name: 'Output Contract', required: true, body: 'Schema esperado da resposta (JSON Schema ou descrição). O Verifier (§14.2) usa para validar antes de entregar. Falha precoce evita resposta ruim para o usuário.' },
          { name: 'Guardrails', required: false, body: 'Regras de comportamento (não inventar números, sempre citar fonte, recusar X). Aparecem no system prompt e são checadas pelo Verifier.' },
          { name: 'Failure Modes', required: true, body: 'O que fazer quando o input é ruim, falta dado ou tool falha. Documenta o "plano B" do agent.' },
          { name: 'Execution Profile', required: false, default: 'auto-inferido', body: 'fast | standard | rigorous | declarative. Se omitido, o Maestro infere baseado em outros campos. O modo declarative roda ## API Bindings (HTTP) ou ## Data Tables (SQL parametrizado) sem chamar o LLM.' },
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
          { title: 'Tool não declarada = tool invisível', severity: 'warning', body: 'Se você registrou uma tool em /mcp mas esqueceu de listar em "Tool Bindings" do SKILL.md, o LLM nunca vai chamar — porque ele nem sabe que existe. Permitted Toolset é interseção registro × declaração.' },
          { title: 'Execution Profile errado dispara custo', severity: 'warning', body: 'Skill simples marcada como "rigorous" faz 3+ chamadas LLM por interação. Se for uma skill de classificação trivial, vire "fast" e economize tokens.' },
          { title: 'kind da SKILL ≠ kind do AGENTE', severity: 'info', body: 'No frontmatter da SKILL.md, a camada Maestro é declarada como kind: orchestrator. Mas o AGENTE (em /agents) usa kind: aobd para a mesma camada — o enum aceito pela API do agente é aobd | router | subagent. Não copie "orchestrator" para o agente: a validação rejeita. router (Triagem) e subagent (Especialista) são iguais nos dois.' },
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
    summary: 'O "chat" onde você invoca um agent, pipeline ou recipe publicado e acompanha cada etapa da execução ao vivo.',
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
  // /mesh — AI Mesh (Fluxo de agentes + Estúdio de Pipelines)
  // ═════════════════════════════════════════════════════════════════
  mesh: {
    title: 'AI Mesh — Fluxo de agentes',
    summary: 'Editor visual único da rede de agents (o Fluxograma): quem chama quem, em que ordem e sob qual condição. Também é onde você monta pipelines e os publica no Catálogo.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>O AI Mesh é a "rede" de agents. O <strong>Fluxo de agentes</strong> (<code>/mesh/flow</code>) é o editor ÚNICO dessa rede — a antiga página "Topologia de conexões" foi aposentada. Quando uma tarefa precisa passar por mais de um agent (extrair → validar → resumir), aqui é onde você desenha o fluxo visualmente.</p>
          <p>Cada nó é um agent. Cada aresta é uma chamada. Você também agrupa nós em <strong>pipelines</strong> (entidade de 1ª classe, com ciclo de vida) e os publica no Catálogo como <code>kind=pipeline</code> — invocáveis selados via <code>POST /api/v1/pipelines/{id}/invoke</code>.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Quatro tipos de conexão entre agents:</p>
          <ul>
            <li><strong>Sequencial</strong> — A → B → C. O output de A vira contexto de B.</li>
            <li><strong>Paralela (fan-out)</strong> — A → (B + C + D). Os destinos rodam TODOS ao mesmo tempo com o mesmo input (não é escolha 1-de-N).</li>
            <li><strong>Condicional</strong> — A → B só se uma regra casar. É o roteamento 1-de-N: o destino roda conforme a pergunta do usuário ou a resposta do upstream.</li>
            <li><strong>Padrão (default)</strong> — o destino "else": roda quando NENHUMA regra condicional do fan-out casou. Combine um <code>default</code> com várias <code>conditional</code> para garantir que sempre haja um caminho.</li>
          </ul>
          <p>Cada conexão também escolhe <strong>quanto contexto passa adiante</strong>: <em>Herdar</em> (output inteiro, padrão), <em>Filtrar/scoped</em> (transforma o output antes de repassar — economiza tokens) ou <em>Isolar</em> (o próximo agent recebe só a pergunta original).</p>
          <p>Agents <strong>pass-through</strong> (sem skill vinculada e sem prompt customizado) são detectados e <strong>pulados</strong> automaticamente. Não desperdiça chamada de LLM em nó que não faz nada.</p>
          <p>Para roteamento automático (decidir QUAL agent chamar pela intenção do usuário), o <strong>Maestro</strong> consulta o <strong>CAR</strong> (Catálogo de Roteadores) — não usa o Mesh diretamente. Mesh é para fluxos que você desenha; CAR é para a escolha automática.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Regras condicionais sem decorar sintaxe',
        body: `
          <p>Uma conexão <strong>condicional</strong> só dispara quando a regra casa. A regra é uma expressão (Jinja) sobre variáveis como <code>input_lower</code> (a pergunta), <code>output_lower</code> (a resposta do agent anterior), <code>has_document</code> (veio um anexo?) ou <code>is_refuse</code> (a decisão foi recusar?). Você não precisa decorar nada — há três caminhos para a MESMA regra:</p>
          <ul>
            <li><strong>Descreva em português</strong> — escreva "se mencionar pix ou anexar um documento" e a IA traduz para a expressão. O sistema PROVA: rejeita variável inexistente e testa se a regra avalia sem erro antes de oferecer.</li>
            <li><strong>Monte por cards (Galeria)</strong> — escolha cartões prontos (palavra na pergunta, tipo de anexo, decisão tomada) e combine com <strong>E / OU</strong> e parênteses, sem escrever código.</li>
            <li><strong>Teste no Simulador</strong> — antes de salvar, informe uma pergunta, uma resposta simulada, anexos e a decisão, e veja na hora se a regra <em>casa</em> ou <em>não casa</em>. O erro aparece para você corrigir (fail-closed), em vez de quebrar só na produção.</li>
          </ul>
          <p>A lista completa de variáveis disponíveis (com explicação em português) é a mesma que o construtor mostra ao lado do campo.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Pipeline ETL com IA (publicável)', body: 'No Fluxograma: Agent 1 extrai dados de um e-mail → Agent 2 valida CNPJ via tool MCP → Agent 3 gera resumo executivo. Salve como pipeline, publique no Catálogo e invoque por uma chamada só: POST /api/v1/pipelines/{id}/invoke.' },
          { title: 'Comparar múltiplos modelos', body: 'Fan-out: a mesma pergunta para três agents idênticos exceto pelo modelo (ex.: gpt-4o, claude e um modelo local). Você vê as respostas em paralelo para comparar qualidade.' },
          { title: 'Roteamento por anexo, em português', body: 'Conexão condicional para o agent de documentos com a regra descrita como "quando o usuário anexar um documento" — a IA traduz para has_document. Adicione uma conexão default para o agent genérico cobrir o caso sem anexo.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Mesh / Pipeline ≠ CAR ≠ Recipe', severity: 'info', body: 'Fluxograma/Pipeline = fluxo desenhado visualmente (você define), publicável no Catálogo como kind=pipeline. CAR = catálogo para roteamento automático (o Maestro decide). Recipe = composição declarativa no Catálogo. Conceitos distintos.' },
          { title: 'Sempre teste a regra no Simulador', severity: 'warning', body: 'O Simulador honra pergunta, anexos E a decisão (Recommend/Refuse/Escalate) — não só o texto da resposta. Uma regra sobre a pergunta (input_lower) ou sobre anexo (has_document) que parece errada pode estar certa: confirme no Simulador antes de salvar.' },
          { title: 'Condicional sem default vira buraco', severity: 'warning', body: 'Num fan-out 1-de-N, se nenhuma regra condicional casar e não houver conexão default, nenhum destino roda. Adicione um destino default como rede de segurança.' },
          { title: 'Pass-through sumiu da execução', severity: 'info', body: 'Se um agent "desapareceu" do log, provavelmente está pass-through (sem skill nem prompt). Adicione skill ou prompt customizado para ativar.' }
        ]
      }
    ],
    related: ['agents', 'catalog', 'workspace']
  },

  // ═════════════════════════════════════════════════════════════════
  // /federation — Federação A2A
  // ═════════════════════════════════════════════════════════════════
  federation: {
    title: 'Federação A2A',
    summary: 'Compartilhe pipelines entre organizações: o provider publica manifest + ingress assinado; o consumer puxa peers e invoca remoto de forma assinada e auditada.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Federação conecta dois ou mais Maestros (A2A — Agent-to-Agent). Uma organização <strong>provider</strong> expõe capacidades selecionadas; uma <strong>consumer</strong> as descobre e invoca remotamente — toda chamada é <strong>assinada (HMAC), protegida contra replay e auditada</strong>.</p>
          <p>Vem <strong>desligada por padrão</strong> e falha fechada (fail-closed) sem <code>MAESTRO_SECRET_KEY</code> configurada. Ligar/desligar é um <strong>toggle de runtime</strong> (root) na página de federação — não uma variável de ambiente.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p><strong>Provider (egress de capacidade):</strong> publica um manifest em <code>/.well-known/maestro-federation.json</code> e expõe um ingress assinado <code>POST /api/v1/federation/invoke</code> (verificação HMAC + proteção de replay + execução selada).</p>
          <p><strong>Consumer (ingestão):</strong> registra peers (segredos cifrados), faz <code>sync</code> para puxar o manifest + entries remotas e invoca via <code>POST /api/v1/federation/remote/{entry_id}/invoke</code>. Uma guarda SSRF protege contra alvos internos.</p>
          <p>O custo da chamada remota é atestado pelo peer e limitado (clamp) na origem.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Disponibilizar um pipeline para um parceiro', body: 'Como provider, publique a capacidade (um pipeline published + visibilidade company) e gere as credenciais. O parceiro registra você como peer e invoca o pipeline remotamente — assinado e auditado.' },
          { title: 'Consumir um pipeline de outra org', body: 'Como consumer, registre o peer, rode o sync para descobrir as capabilities e invoque dentro do seu próprio fluxo. O custo é atestado pela origem.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Desligada por padrão', severity: 'warning', body: 'Sem MAESTRO_SECRET_KEY, a federação falha fechada (nada entra nem sai). Configure a chave antes de registrar peers.' },
          { title: 'Execução sempre selada ao snapshot', severity: 'info', body: 'No caminho federado, a execução fica presa ao subgrafo congelado da capacidade (snapshot em catalog_pipeline_defs) — nunca vaza para o mesh global. Pipeline sem snapshot selável não é executável por federação (retorna 422). Por isso só PIPELINES publicados com visibilidade company aparecem no manifest; agentes e recipes ficam de fora hoje.' },
          { title: 'Ligar e gerir peers exige root', severity: 'warning', body: 'Ligar a federação, definir o workspace e registrar/rotacionar/revogar peers são ações de perfil root (GET/PUT /api/v1/federation/config e /api/v1/federation/peers). O segredo compartilhado do peer aparece em plaintext UMA única vez (na criação ou rotação) — compartilhe na hora; o banco só guarda cifrado.' }
        ]
      }
    ],
    related: ['mesh', 'catalog', 'api_connectors']
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
            <li><strong>MCP Server</strong> — endpoint que expõe operações tipadas. Pode ser <code>HTTP</code> (JSON-RPC, com suporte ao transporte MCP Streamable HTTP/SSE) ou <code>stdio</code> (processo local: npx, node, python).</li>
            <li><strong>Registro no Maestro</strong> — nome, descrição, endpoint, classificação de sensibilidade e (opcional) credencial cifrada (API Key, OAuth2 ou mTLS).</li>
            <li><strong>Declaração na skill</strong> — listada em "Tool Bindings" do SKILL.md.</li>
          </ul>
          <p>O <strong>Permitted Toolset</strong> é a interseção entre tools registradas E declaradas na skill. Tools de fora dessa interseção são invisíveis ao LLM — não tem como ele "descobrir" e chamar por engano.</p>
          <p>A plataforma <strong>descobre</strong> as ferramentas reais do servidor (chamada MCP <code>tools/list</code>) e guarda o schema de cada uma. Com a flag <code>MCP_PER_TOOL_ENABLED</code> ligada (Configurações), cada ferramenta vira uma função própria com o schema REAL — o LLM chama direto (ex.: <code>create_issue</code>), sem o intermediário genérico <code>{operation, query}</code>.</p>
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
          { name: 'Schema de argumentos', required: false, body: 'JSON Schema dos parâmetros. Normalmente DESCOBERTO do próprio servidor MCP (tools/list) — você não precisa digitar. Quando a skill declara ## Inputs, esse schema tem prioridade. O Maestro valida antes de chamar.' }
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
    related: ['skills', 'api_connectors', 'catalog', 'agents']
  },

  // ═════════════════════════════════════════════════════════════════
  // /evidence — RAG (Base de Conhecimento)
  // ═════════════════════════════════════════════════════════════════
  evidence: {
    title: 'Bases de Conhecimento (RAG + Tabelas)',
    summary: 'Onde você cadastra o que o agent precisa SABER (a FONTE). Duas técnicas complementares: RAG (busca em textos) e Tabelas (consulta SQL). Fluxo: Fonte (RAG/Tabela/anexo/ferramenta) → regra "Exigir evidências" (Configurações) → resposta ou recusa honesta. Aqui é a FONTE; a REGRA fica em Configurações.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>A "Base de Conhecimento" é onde você dá ao agent o que ele precisa <em>saber</em> para responder bem. Sem isso, ele usa só o que aprendeu no treinamento do LLM — e pode "inventar" (alucinar) detalhes da sua empresa que ele nunca viu.</p>

          <p>A plataforma oferece <strong>DUAS TÉCNICAS COMPLEMENTARES</strong> de busca, que coexistem na mesma base. Saber a diferença ajuda a escolher a certa para cada caso:</p>

          <p><strong>1. RAG (Retrieval-Augmented Generation)</strong> — para <em>textos não-estruturados</em>: manuais, políticas, contratos, FAQs, atas de reunião. Funciona como uma "busca inteligente": a plataforma divide o documento em pedaços (chunks), indexa por significado e palavras-chave, e quando o agent precisa, devolve os trechos mais relevantes para o LLM ler. O LLM compõe a resposta citando esses trechos.</p>

          <p><strong>2. Tabelas (SQL parametrizado via DuckDB)</strong> — para <em>dados estruturados</em>: planilhas CSV/XLSX com colunas e linhas (vendas, clientes, métricas, inventário). Cada planilha vira uma tabela consultável. A Skill executa uma consulta <em>exata</em> (tipo "todos os clientes com renda > 5000") — sem chutar, sem alucinar números, sem perder linhas. O LLM <strong>não</strong> escreve SQL: o autor define os filtros (Tier 1). A bancada experimental "Perguntar à Tabela" (Tier 2, governada pelo Catálogo) deixa a IA <em>compilar</em> a pergunta numa consulta estruturada para o humano curar.</p>

          <p><strong>Por que dois jeitos?</strong> Porque texto e tabela têm naturezas diferentes:</p>
          <ul>
            <li>Texto livre se beneficia de <strong>busca semântica</strong> ("prazo de pagamento" deve achar trecho sobre "cobrança").</li>
            <li>Tabela exige <strong>filtros e agregações precisas</strong> ("qual a média de X agrupada por Y") — algo que busca semântica faz muito mal.</li>
          </ul>

          <p><strong>Importante:</strong> as duas técnicas BUSCAM, não JULGAM. Quem verifica se a resposta usou bem as fontes é o Verifier (§14.2 — veja /quality).</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Como cada técnica funciona por dentro — útil quando algo não está vindo como esperado.</p>

          <p><strong>RAG — busca híbrida em textos</strong></p>
          <p>Quando o agent precisa de uma informação textual, a plataforma combina <em>dois jeitos de buscar</em>:</p>
          <ul>
            <li><strong>BM25</strong> — busca clássica por palavras-chave (exatidão lexical). Se o user pergunta "qual o prazo de cobrança", encontra trechos com essas palavras exatas. Usa <code>tsvector</code> + índice GIN do Postgres.</li>
            <li><strong>Vetorial</strong> — busca semântica (por significado). "prazo de pagamento" também acharia trechos sobre "cobrança" porque os vetores ficam próximos. Usa <strong>pgvector</strong> (busca vetorial dentro do próprio Postgres) + embeddings (Qwen3 ou Azure). O provider de embeddings tem fallback automático: se o primário (Qwen3, via hub interno) cai, a plataforma migra para o Azure e registra o evento no log.</li>
          </ul>
          <p>Os dois rankings são fundidos via <strong>Reciprocal Rank Fusion</strong> (k=60) — uma fórmula que respeita o melhor dos dois sem ter que escolher. Opcional: um <strong>Reranker LLM</strong> faz uma re-ordenação final por relevância contextual. As top-N evidências vão para o LLM gerador montar a resposta.</p>
          <p>Pipeline RAG: <strong>ingestão</strong> (você sobe doc) → <strong>chunking</strong> (quebra em pedaços de ~500 tokens) → <strong>embedding</strong> (gera vetores) → <strong>indexação</strong> (BM25 + vector store) → consultável em tempo real.</p>

          <p><strong>Tabelas — consulta SQL em dados estruturados</strong></p>
          <p>Quando você sobe um CSV ou XLSX, a plataforma <em>analisa</em> automaticamente. Se detecta uma planilha estruturada (colunas com tipos, headers limpos), abre um modal próprio oferecendo "Promover para tabela consultável".</p>
          <ul>
            <li>Os dados vão para um arquivo <strong>DuckDB</strong> embarcado (1 arquivo por tabela, em <code>data/tabular/&lt;ks_id&gt;/&lt;table_id&gt;.duckdb</code>). DuckDB é como um SQLite "turbinado para análise" — rápido em filtros e agregações.</li>
            <li>No editor de Skills, o botão <strong>"Inserir Tabela"</strong> lista as tabelas disponíveis. Você escolhe uma, define os <em>filtros</em> (coluna + operador + valor que vem do input do user), as <em>colunas a retornar</em> e o <em>limite</em>. A skill recebe um YAML estruturado dentro da seção <code>## Data Tables</code>.</li>
            <li>Quando a skill executa, ela monta uma query parametrizada (<code>SELECT col1, col2 FROM data WHERE col3 = ? LIMIT N</code>) com <strong>bind variables seguras</strong> e roda em modo <em>read-only</em>. <strong>NÃO é o LLM que escreve SQL</strong> — é uma consulta predefinida com parâmetros que o LLM apenas preenche.</li>
          </ul>
          <p>Pipeline Tabelas: <strong>upload</strong> (CSV/XLSX) → <strong>análise</strong> (detecta colunas, tipos, abas, header mergeado) → <strong>promoção</strong> (cria <code>.duckdb</code>) → <strong>uso na skill</strong> via "Inserir Tabela" → consulta parametrizada na invocação.</p>

          <p><strong>As duas técnicas coexistem na MESMA base</strong>. Quando você sobe um CSV/XLSX, ele entra como chunks textuais (RAG) E você pode promover para tabela. A skill escolhe qual usar — ou ambos.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Quando usar cada técnica',
        items: [
          {
            title: 'Use RAG quando a pergunta é "o que diz X?"',
            body: 'Perguntas como "qual a política de devolução?", "como é o procedimento de onboarding?", "o que o contrato fala sobre rescisão?". A resposta está em algum trecho de texto — o LLM lê e responde.'
          },
          {
            title: 'Use Tabelas quando a pergunta exige filtros, agregações ou números precisos',
            body: 'Perguntas como "quantos clientes têm renda > 5000?", "top 10 produtos por vendas em Q4", "média de tempo de resposta por agente", "lista de pedidos do cliente X". A resposta vem de tabela — não de prosa.'
          },
          {
            title: 'Use AMBAS quando o CSV tem texto + dados',
            body: 'Planilha de feedback de cliente com 1 coluna de texto livre ("comentário") e várias colunas estruturadas (data, NPS, segmento). Texto vira chunk RAG (busca por significado: "reclamações sobre entrega"); colunas estruturadas viram tabela (filtragem: "NPS < 6 em janeiro").'
          },
          {
            title: 'Sintoma de escolha errada',
            body: 'Se sua skill RAG está "inventando" números ou comparando errado, provavelmente devia ser tabela. Se sua skill Tabela está perdendo nuance qualitativa (não consegue resumir o sentimento das observações), provavelmente devia ser RAG.'
          }
        ]
      },
      {
        kind: 'campos',
        title: 'Campos do registro de base',
        items: [
          { name: 'Nome', required: true, body: 'Nome legível da base. Aparece nas configurações dos agents e no dropdown "Inserir Tabela" do editor de Skills.' },
          { name: 'Tipo', required: true, options: ['manual', 'regulatório', 'contratual', 'FAQ'], body: 'Categoria do conteúdo. Útil para filtragem e gating por agent.' },
          { name: 'Confidencialidade', required: true, options: ['publica', 'interna', 'confidencial', 'restricted'], body: 'Nível de acesso. As Tabelas promovidas dessa base HERDAM essa configuração — uma tabela só é visível para os mesmos users que podem ver a KS de origem.' },
          { name: 'Domínio', required: false, body: 'Tag de área (fiscal, jurídico, RH). Agents do mesmo domínio têm preferência.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Ingestão lenta = base grande', severity: 'info', body: 'Documentos grandes (>100 páginas) levam tempo para chunkar + embedar. Não cancele no meio. Acompanhe o progresso na barra.' },
          { title: 'RAG sem Verifier = sem rede de proteção', severity: 'warning', body: 'Se você ativa RAG mas não usa Verifier (§14.2), o agent pode citar trecho errado e ninguém percebe. Ative ambos juntos.' },
          { title: 'Embedding model precisa ser consistente', severity: 'danger', body: 'O embedder padrão é Qwen3 (vetores de 1024 dimensões); o fallback é Azure (1536 dimensões). Ao TROCAR de provider de embeddings, a DIMENSÃO do vetor muda — e as queries novas deixam de casar com os vetores antigos da base. Sempre re-ingira (re-embede) tudo ao trocar de embedder. Obs.: o fallback automático Qwen3→Azure também troca a dimensão; a plataforma usa a dimensão do provider que de fato respondeu para não corromper o índice.' },
          { title: 'CSV/XLSX só como RAG = números viram texto pouco útil', severity: 'warning', body: 'Subir uma planilha SEM promover para tabela transforma os dados em chunks de texto. O agent vai citar trechos como "linha 47 tem valor 5000" — não consegue filtrar nem agregar nem comparar. SEMPRE promova planilhas estruturadas para tabela quando precisar de consulta numérica.' },
          { title: 'XLSX com título mergeado na linha 1 — auto-detect', severity: 'info', body: 'Se a linha 1 do XLSX tem só um título mergeado (ex: "TB_VENDAS" em A1:G1) e os headers reais estão na linha 2, a plataforma DETECTA automaticamente e usa a linha 2 como header. Você verá um aviso "↻ Auto-detect: linha 1 parecia título" no modal de promoção. Não precisa editar o arquivo.' },
          { title: 'XLSX multi-aba = N tabelas separadas', severity: 'info', body: 'Cada aba do XLSX vira UMA TABELA independente. O modal mostra todas as abas detectadas; você pode promover só uma, ou clicar "Promover todas as N abas" para criar uma tabela por aba. Cada uma fica referenciável pelo nome no editor de Skill.' },
          { title: 'Tabelas são read-only por execução', severity: 'info', body: 'A skill só pode CONSULTAR (SELECT). Não há como INSERT/UPDATE/DELETE/DROP nas tabelas — defesa técnica contra qualquer tentativa do LLM de modificar dados. Para atualizar a tabela, re-suba o arquivo (gera uma nova versão).' },
          { title: 'Não é o LLM que escreve o SQL', severity: 'info', body: 'A consulta SQL da tabela é DECLARADA antecipadamente no editor de Skill (filtros, colunas, limite). O LLM apenas preenche os parâmetros vindos da pergunta do user. Isso evita SQL injection e queries malucas — é mais perto de "form com parâmetros" do que "LLM falando SQL livre".' }
        ]
      }
    ],
    related: ['agents', 'skills', 'quality', 'workspace', 'settings']
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
          { title: 'Self-preference do juiz', severity: 'warning', body: 'Juiz e gerador do mesmo modelo (gpt-4o avaliando gpt-4o) tende a dar nota alta. Use um juiz diferente para reduzir esse viés.' },
          { title: 'Contrato com retry automático tem custo extra na falha', severity: 'info', body: 'Quando o ContractValidator marca a resposta como fora do output_contract, o Verifier re-chama o LLM 1x com os erros específicos para tentar corrigir o formato (ligado por padrão). Isso conserta violações triviais (vírgula sobrando, chave faltando) sem intervenção — ao custo de 1 chamada LLM extra apenas nas falhas. Desligue só em orçamento muito apertado.' }
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
          <p>API Connectors complementam o <strong>Tool Registry</strong> (/mcp). A diferença:</p>
          <ul>
            <li><strong>/mcp (MCP)</strong> — protocolo padronizado, schema tipado, agent chama via descrição declarada na skill.</li>
            <li><strong>/api-connectors (HTTP)</strong> — chamada HTTP direta, mais flexível, melhor para integrações ad-hoc ou testes.</li>
          </ul>
          <p>Suporta <strong>5 tipos de autenticação</strong>: <code>none</code>, <code>api_key</code> (header ou query), <code>bearer</code>, <code>basic</code> e <code>cookie</code> (sessão). Para cookie há um helper "Gerar cookie via login" que faz o POST de login e extrai o token do <code>Set-Cookie</code> automaticamente. A API key fica <strong>cifrada em repouso</strong>.</p>
          <p>O builder fala 5 formatos de corpo por endpoint (<code>json</code>, <code>form_urlencoded</code>, <code>multipart</code>, <code>text</code>, <code>xml</code>) e respeita <code>verify_ssl</code> por connector (desligável para APIs self-signed).</p>
          <p>Health check periódico mostra se cada connector está vivo. Histórico de chamadas registra método, URL, status, latência e body — para auditoria (limpeza manual por retenção).</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Descoberta automática (IA, me ajude!)',
        items: [
          { title: 'Cole a URL e a plataforma lê o OpenAPI/Swagger', body: 'O botão "IA, me ajude!" tenta os caminhos comuns de openapi.json/swagger.json (e até parseia a página do Swagger UI/ReDoc) para propor nome, base_url, autenticação, health_path e a lista de endpoints. Você revisa e aplica — nada é salvo sem o seu OK.' },
          { title: 'Descreva o endpoint em português e a IA preenche o form', body: 'No modal de novo endpoint, digite algo como "consultar CNPJ por número" e o modelo primário sugere método, path, categoria, descrição, sample_body e até valores de teste que retornam 200.' },
          { title: 'API sem openapi.json disponível', body: 'Se a API não publica OpenAPI (comum em APIs públicas como ViaCEP/BrasilAPI), cole um comando cURL — a plataforma extrai base_url, auth e endpoint — ou cadastre manualmente em "+ Novo endpoint".' }
        ]
      },
      {
        kind: 'campos',
        title: 'Campos do connector',
        items: [
          { name: 'Nome', required: true, body: 'Nome legível. Aparece em listas e no builder.' },
          { name: 'Base URL', required: true, body: 'Raiz da API. Endpoints concatenam path relativo a essa base.', example: 'https://api.viacep.com.br' },
          { name: 'Tipo de auth', required: true, options: ['none', 'api_key', 'bearer', 'basic', 'cookie'], body: 'Como o connector autentica. Determina os campos extras que aparecem.' },
          { name: 'verify_ssl', required: false, default: 'ligado', body: 'Validação do certificado TLS. Desligue (0) apenas para APIs internas com certificado self-signed — nunca para APIs públicas.' },
          { name: 'body_type (por endpoint)', required: false, options: ['json', 'form_urlencoded', 'multipart', 'text', 'xml'], default: 'json', body: 'Formato do corpo da requisição. O builder ajusta o Content-Type automaticamente.' },
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
    related: ['tools', 'catalog', 'agents', 'workspace']
  },

  // ═════════════════════════════════════════════════════════════════
  // /harness — Harness de Avaliação
  // ═════════════════════════════════════════════════════════════════
  harness: {
    title: 'Harness',
    summary: 'Motor de avaliação que roda o agente contra um Golden Dataset e decide, por um gate, se a release vai para produção.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Antes de promover uma release, o Harness roda os <strong>gold cases</strong> (casos curados) contra a versão candidata e produz um relatório: acurácia, recusa correta, falso positivo, latência, custo e — com o Verifier ligado — factuality/completeness/tone/safety. Se passa nos thresholds, libera; se não, bloqueia (com o motivo em gate_reason).</p>
          <p>É o "CI/CD de qualidade" da plataforma. Sem isso, você está apostando que a mudança no prompt não quebrou nada.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Golden Dataset (§9.4) é <strong>versionado</strong> e <strong>estratificado por jornada</strong>, com proporção de <strong>casos adversariais</strong>. Cada caso tem:</p>
          <ul>
            <li><code>input_text</code> — a entrada enviada ao agente</li>
            <li><code>expected_output</code> (similaridade) ou <code>expected_pattern</code> (regex, prioritário)</li>
            <li><code>expected_state</code> — a DECISÃO esperada: Recommend, Refuse ou Escalate</li>
            <li><code>case_type</code> — normal ou adversarial (adversariais alimentam a recusa correta)</li>
            <li><code>category</code> (taxonomia), <code>weight</code> (peso na média ponderada), <code>red_flags</code> (strings que NÃO podem aparecer)</li>
          </ul>
          <p>Gate automático: aprovado quando acurácia ponderada, recusa correta, factuality/completeness/tone, safety e contract-compliance ficam dentro dos thresholds, e falso positivo/alucinação abaixo do limite.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Antes de promover release', body: 'Você refinou um prompt. Roda o harness (baseline) — em minutos sabe se quebrou os casos críticos.' },
          { title: 'Regressão', body: 'run_type=regression compara contra o baseline mais recente do mesmo release e gold_version. Detecta degradação por mudança silenciosa no modelo do provider.' },
          { title: 'A/B de modelos', body: 'Mesma skill, 2 execuções. Compara lado a lado via /eval-runs/compare (deltas por métrica e por categoria).' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Decision-state colapsa em LogAndClose', severity: 'info', body: 'O FSM termina sempre em LogAndClose; a decisão real (Recommend/Refuse/Escalate) é recuperada do transition_log antes de casar com expected_state. Por isso o expected_state deve usar esses três valores, não LogAndClose.' },
          { title: 'Golden pequeno = teste fraco', severity: 'warning', body: 'Harness com 5 casos não diz quase nada. Comece com 20+ casos cobrindo as principais jornadas + 5+ adversariais (cobrindo Refuse/Escalate).' },
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
    summary: 'Versionamento atômico de configurações — promover entre ambientes e monitorar drift.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Uma <strong>release</strong> é um pacote imutável de configurações: <code>model_config</code>, <code>prompt_config</code>, <code>index_config</code> e <code>policy_config</code>, congelados num snapshot identificado.</p>
          <p>Em vez de promover artefatos isolados (atualizei a skill mas o índice está velho?), você promove a release inteira. Garante consistência.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Ambientes de promoção (campo <code>environment</code>):</p>
          <ul>
            <li><strong>staging</strong> — visível só para devs/testers</li>
            <li><strong>canary</strong> — fração do tráfego em produção</li>
            <li><strong>production</strong> — 100% do tráfego</li>
          </ul>
          <p>Promoção via <code>PUT /api/v1/releases/{id}/promote?target_env=...</code> (move environment + status e grava no audit_log). Drift é registrado em <code>drift_events</code>; a forma de pegar regressão entre versões hoje é rodar o Harness (§9.5) em <code>run_type=regression</code> contra o baseline do mesmo release e dataset. (Detecção estatística automática e rollback por SLO são roadmap, ainda não implementados.)</p>
        `
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Direto para production = roleta russa', severity: 'danger', body: 'Pular canary economiza 10 minutos e pode custar horas de incidente. Sempre passe por canary.' },
          { title: 'Regressão precisa de baseline', severity: 'warning', body: 'O harness em regression só compara se existe um baseline COMPLETO do mesmo release e mesmo gold_version. Rode o baseline antes de promover.' }
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
    summary: 'Traces, custos e performance — exportados para o stack OTEL (Tempo/Loki/Grafana) ou para o LangFuse.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Camada de tracing-first. Cada interação propaga <strong>W3C Trace Context</strong>, gera spans por camada (Maestro → Triagem → Especialista), registra prompt efetivo, modelo usado, output, custo em tokens e latência por etapa.</p>
          <p>Use para investigar incidentes em profundidade, comparar custo entre modelos e fazer debug de latência.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Dois caminhos, independentes e ambos de primeira classe:</p>
          <ul>
            <li><strong>OpenTelemetry self-hosted</strong> — toggle <code>OTEL_ENABLED</code>; spans vão para Tempo (traces) e os logs para Loki, com Grafana como UI. Sobe com <code>docker compose --profile full</code>.</li>
            <li><strong>LangFuse</strong> (SaaS ou self-hosted) — configure as credenciais em /settings → Plataforma → LangFuse. Cada interação vira um trace com spans hierárquicos.</li>
          </ul>
          <p>Spans manuais cobrem os pontos críticos (transições da FSM, etapas do RAG, reranker, ingestão, decisões de policy).</p>
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
          { title: 'Sem backend configurado = sem traces', severity: 'warning', body: 'Ou ligue OTEL_ENABLED e suba o profile full (Tempo/Loki/Grafana), ou cole as credenciais do LangFuse em /settings — senão não há onde os traces caírem.' }
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
    summary: 'Estado dos componentes da plataforma — banco, cache, vetores (pgvector) e observabilidade — em uma tela só.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>A Infraestrutura mostra a saúde dos serviços de baixo nível que a plataforma usa: PostgreSQL (dados E vetores do RAG, via pgvector), Redis (rate-limit + cache) e o stack de observabilidade quando ativo (Tempo, Loki, Grafana).</p>
          <p>É o lugar para investigar "por que está lento?" ou "por que essa página não carregou?" — antes de mergulhar em traces específicos no /observability.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Os componentes monitorados:</p>
          <ul>
            <li><strong>PostgreSQL</strong> — backend único de persistência. Tudo passa por aqui: agents, skills, interactions, audit_log, catalog, etc. Também guarda os vetores do RAG via extensão <strong>pgvector</strong> + índice HNSW. Se cair, a plataforma toda fica indisponível.</li>
            <li><strong>Redis</strong> — usado para rate-limit (sliding window) e cache leve de routing. Falha = rate-limit desligado (failsafe open).</li>
            <li><strong>Vetores (pgvector)</strong> — desde a Onda Q (2026-05-30), pgvector é o <strong>único</strong> backend e vive dentro do Postgres, sem serviço extra. (O Qdrant, usado antes, foi removido — não há mais escolha de backend.)</li>
            <li><strong>OpenTelemetry / LangFuse</strong> — exportação de traces. Quando configurado e ativo, traces aparecem em Tempo/Grafana ou em LangFuse.</li>
          </ul>
          <p>Status verificado em tempo real via probes leves (latência de query, ping, health endpoint). O card de Postgres inclui um check de <code>pgvector dim</code> (dimensão atual vs esperada pelo embedder ativo).</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Diagnóstico inicial de incidente', body: 'Site lento. Antes de pedir trace, vai em Infra: vê PostgreSQL com latência 2s/query. Foco vira tuning do banco, não da app.' },
          { title: 'Confirmar setup', body: 'Acabou de configurar OTEL? Abre Infra para confirmar que o exporter está conectado e enviando spans.' },
          { title: 'Capacity planning', body: 'Acompanhar tamanho do banco, uso de Redis e contagem de vetores no pgvector. Quando um deles passa do limite saudável, é hora de escalar.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Componente "offline" pode ser config', severity: 'info', body: 'Se LangFuse aparece offline, primeiro checa se as credenciais estão preenchidas em /settings. "Offline" pode significar "nunca configurado".' },
          { title: 'Dimensão divergente ≠ plataforma down', severity: 'warning', body: 'Se você troca o embedder, a dimensão dos vetores no pgvector pode divergir da esperada — o card de pgvector dim acende e o RAG vetorial para de casar. A plataforma continua de pé (busca cai para BM25-only). Rode POST /api/v1/evidence/reindex para regenerar a collection com a dimensão correta.' }
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
          { name: 'Plataforma > Embedding', body: 'Selector Azure | Qwen3. Qwen3 reusa o scheme://host do OSS-20B ou OSS-120B (só muda o path) e suporta densidade Matryoshka (128..1536). Há uma CADEIA DE RESILIÊNCIA: se o provider configurado não responde, a plataforma cai para o fallback (azure quando o primário não é azure; qwen3 caso contrário; ou o que você fixar em embedding_fallback_provider). O chip de Saúde dos Modelos no header fica âmbar quando esse fallback está ativo.' },
          { name: 'Roteamento LLM', body: 'Mapa: tool_calling/reasoning/instruct/classification → provider/modelo. Define qual LLM cada "tipo de tarefa" usa. Configurar uma vez e todos os agents com task_type ficam alinhados.' },
          { name: 'System Prompts', body: 'Templates de system prompts reutilizáveis. Quando criar um agent, você pode escolher um template salvo em vez de escrever do zero.' },
          { name: 'Usuários', body: 'Gestão de contas (root only). Roles: root (admin total), comum (uso normal), admin (gestão sem credenciais).' },
          { name: 'API Keys', body: 'Geração de chaves de API para acesso externo. Cada chave tem nome, escopo e data de expiração.' },
          { name: 'Header > Saúde dos Modelos (chip)', body: 'Chip no topo de toda tela que sonda os modelos em uso (1 token por modelo de chat + 1 embedding) e reporta o que será usado daqui pra frente. Verde = tudo responde; âmbar (com contador) = fallback ativo OU algum modelo indisponível. Abra o painel: cada linha por papel/Embeddings fica verde (ok) ou vermelha (indisponível). Cacheado ~5 min; force=true re-sonda. Endpoint GET /api/v1/llm/health.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Mudar Roteamento muda todos os agents com task_type', severity: 'warning', body: 'Configuração em "Roteamento LLM" afeta todos os agents que usam aquele task_type. Não é específica de um agent. Mude com cuidado em produção.' },
          { title: 'Modelo Primário só vale para agents SEM task_type', severity: 'info', body: 'Se o agent declara task_type, o Roteamento ganha. Primário é só fallback para agents legacy ou sem declaração.' },
          { title: 'API key pública é vazamento', severity: 'danger', body: 'Não cole API keys em código frontend, repositórios públicos, ou logs. Use o gerador em "API Keys" e proteja como senha.' },
          { title: 'Chip âmbar nem sempre é problema', severity: 'info', body: 'O chip de Saúde dos Modelos no header fica âmbar tanto para fallback ativo (tipicamente embeddings caindo para o provider de contingência) quanto para modelo indisponível — em ambos a plataforma pode continuar funcionando. Abra o painel e compare configured x effective na linha de Embeddings; linhas vermelhas indicam indisponibilidade real.' }
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
    summary: 'Marketplace interno de agents, skills, recipes e plataformas externas — com governança, divulgação de capacidade e versionamento.',
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
          <p>Tipos (<code>kind</code>): <code>agent</code>, <code>skill</code>, <code>recipe</code> (composição declarativa), <code>pipeline</code> (grafo publicado pelo Fluxograma) e <code>external_platform</code> (ChatGPT/etc).</p>
          <p><strong>Divulgação de Capacidade</strong> (etiqueta nutricional R6.3) é obrigatória: flags de dados + soberania + retenção. Quem consome o agent sabe exatamente o que ele faz com os dados.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Publicar agent para a empresa toda', body: 'Você criou um agent que valida cálculo de hora extra. Cria entry kind=agent, declara a divulgação de capacidade (processa dados pessoais, não treina, retém 30 dias), submete. Root aprova. Agora qualquer área pode invocar.' },
          { title: 'Recipe que encadeia 3 agents', body: 'Em vez de um agent gigante, cria 3 pequenos no Catálogo (Extrator, Validador, Resumo) e um Recipe que invoca em sequência. Reutilizável.' },
          { title: 'Cadastrar ChatGPT Plus', body: 'A empresa aprovou ChatGPT Plus para uso geral? Cria entry kind=external_platform, declara vendor + contrato vigente + casos de uso aprovados. Inventário regulatório fica completo.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Divulgação de Capacidade incompleta = submit reprovado', severity: 'warning', body: 'Pré-verificação obriga a divulgação. Se você marcar "stores_input" mas não preencher retention_days, Root rejeita.' },
          { title: 'Versão não pode regredir', severity: 'danger', body: 'Você publicou v1.2.0. Não pode publicar v1.1.0 depois. Sempre incremente.' },
          { title: 'Recipe sem steps = não executa', severity: 'info', body: 'Criou entry kind=recipe mas esqueceu de declarar os steps? Pré-verificação vai pegar. Use a aba "Passos do Recipe" da página de detalhe.' }
        ]
      }
    ],
    related: ['agents', 'skills', 'catalog_cost']
  },

  // ═════════════════════════════════════════════════════════════════
  // /catalog/publish — Wizard de publicação (4 steps)
  // ═════════════════════════════════════════════════════════════════
  // O help chat lê window.__pageHelpExtra para saber o step ativo e
  // priorizar a seção correspondente do conteúdo abaixo.
  catalog_publish: {
    title: 'Publicar no Catálogo',
    summary: 'Wizard de 4 passos para registrar um artefato no catálogo: escolha do artefato → metadados → divulgação de capacidade → revisão.',

    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Publicar uma <strong>entry</strong> no Catálogo é o ato de cadastrar oficialmente um agente, skill, recipe ou plataforma externa para que outras pessoas da empresa possam descobrir, avaliar e invocar.</p>
          <p>O fluxo é deliberadamente em quatro passos para garantir que a entry tenha <strong>todos os metadados de governança</strong> antes de ir para revisão Root — nome único, versão semver, divulgação das capacidades e (quando aplicável) dados de contrato/custo.</p>
          <p>Ao final do wizard, a entry é criada em status <code>draft</code> e automaticamente submetida para a fila Root (<code>submitted</code>). Root aprova/rejeita/pede mudanças; aprovação habilita a publicação efetiva.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fluxo do wizard',
        body: `
          <p>Quatro passos sequenciais, cada um com validação própria:</p>
          <ul>
            <li><strong>Passo 1 — Artefato:</strong> escolha o tipo (plataforma externa, recipe, agente interno ou skill interna). A escolha determina quais campos aparecem nos passos seguintes.</li>
            <li><strong>Passo 2 — Metadata:</strong> nome, versão semver (ex: 1.0.0), descrição, domínio (opcional), visibilidade (privada/empresa/departamento) e steward.</li>
            <li><strong>Passo 3 — Divulgação de Capacidade:</strong> 12 flags obrigatórias declarando o que a entry faz com dados (lê/escreve KB, chama APIs, processa PII/financeiro/saúde, etc.) + soberania e retenção.</li>
            <li><strong>Passo 4 — Revisão:</strong> resumo de tudo. Clicar em "Confirmar e Submeter" dispara: <code>POST /entries</code> (cria draft) → <code>PUT /entries/{id}/capability</code> (salva a divulgação) → <code>POST /entries/{id}/submit</code> (envia para fila Root).</li>
          </ul>
          <p>O <strong>identificador único</strong> (URN interno) é gerado a partir de tipo + nome + versão. A mesma combinação não pode existir duas vezes — para republicar, suba a versão (1.0.0 → 1.0.1).</p>
        `
      },
      {
        kind: 'campos',
        title: 'Passo 1 — Artefato',
        items: [
          {
            name: 'Registrar Plataforma Externa',
            body: 'Catalogar uma IA terceirizada já aprovada (ChatGPT Enterprise, Cursor, Copilot Studio, Lindy, etc.). Não tem vínculo com um agente/skill interno — você declara vendor, contrato, custo mensal e restrições.',
            example: 'ChatGPT_Plus_2023'
          },
          {
            name: 'Construir Recipe',
            body: 'Recipe é uma composição declarativa de entries existentes (chain). Você cria a entry primeiro com este wizard; os steps são adicionados depois em /catalog/{id}, aba "Recipe Steps". Execução real pelo engine de recipes (já operacional).',
          },
          {
            name: 'Publicar artefato interno (agente ou skill)',
            body: 'Lista os agentes e skills criados na plataforma. Clique no card para escolher. O tipo é detectado automaticamente do artefato selecionado.',
          }
        ]
      },
      {
        kind: 'campos',
        title: 'Passo 2 — Metadata',
        items: [
          {
            name: 'Nome',
            required: true,
            body: 'Como a entry aparece nas listas do catálogo. Use nome claro e descritivo. Pode ser editado depois.',
            example: 'Analista Fiscal — Restituição PF'
          },
          {
            name: 'Versão',
            required: true,
            default: '1.0.0',
            body: 'Siga o padrão semver MAJOR.MINOR.PATCH. Para republicar a mesma entry com mudanças, suba a versão (ex: 1.0.0 → 1.0.1). Cada combinação tipo + nome + versão só pode existir uma vez.',
            example: '1.0.0'
          },
          {
            name: 'Descrição',
            required: true,
            body: 'Resume o que a entry faz e seus casos de uso. Mínimo de 10 caracteres. Aparece em listagens — escreva pensando em quem vai descobrir e decidir se usa.',
            example: 'Responde dúvidas sobre restituição de IRPF analisando o extrato e calculando o valor estimado.'
          },
          {
            name: 'Domínio',
            required: false,
            body: 'Área de negócio à qual a entry pertence. Usado em buscas e relatórios por domínio.',
            example: 'fiscal'
          },
          {
            name: 'Visibilidade',
            required: true,
            options: ['Privada (só você + Root)', 'Empresa (todos veem após publish)', 'Departamento (só pessoas dos domínios)'],
            default: 'Privada',
            body: 'Controla quem enxerga a entry após ela estar published. Em draft/submitted/approved, só owner e Root veem (independente da visibilidade declarada).'
          },
          {
            name: 'Steward',
            required: false,
            body: 'Time responsável pela manutenção e atualização desta entry. Aparece como ponto de contato.',
            example: 'time_de_dados@empresa.com'
          }
        ]
      },
      {
        kind: 'campos',
        title: 'Passo 3 — Divulgação de Capacidade',
        items: [
          {
            name: 'Lê base de conhecimento do consumer',
            body: 'Marque se a entry acessa documentos/conteúdos da KB do usuário invocador. Importante para auditoria de acesso a dados.'
          },
          {
            name: 'Escreve em base do consumer',
            body: 'Marque se a entry grava em alguma KB do usuário (criar nota, salvar resultado). Implica em writes que sobrevivem à invocação.'
          },
          {
            name: 'Persiste input do consumer',
            body: 'Se o input do usuário é armazenado além do log padrão de auditoria, marque. Combine com retention_days para indicar por quanto tempo.'
          },
          {
            name: 'Chama APIs externas',
            body: 'Marque se a entry faz HTTP outbound para qualquer serviço fora da plataforma (Google, Stripe, governo, etc.).'
          },
          {
            name: 'Acessa internet aberta',
            body: 'Distinto de chamar APIs específicas — marca acesso a páginas web arbitrárias (scraping, browse). Maior superfície de risco.'
          },
          {
            name: 'Processa PII (dados pessoais)',
            body: 'Marque se a entry recebe ou produz dados pessoais identificáveis: CPF, email, telefone, endereço, etc. Aciona governance reforçado.'
          },
          {
            name: 'Processa dados financeiros',
            body: 'Marque para dados monetários, transações, saldos, cartões. Aciona controles regulatórios adicionais.'
          },
          {
            name: 'Processa dados de saúde',
            body: 'Marque para dados clínicos, diagnósticos, exames. LGPD trata como sensível — visibility deveria ser restrita.'
          },
          {
            name: 'Input vira training data',
            body: 'Marque se o input do consumer é usado para treinar/fine-tunar modelos. Importante para consentimento.'
          },
          {
            name: 'Output determinístico',
            body: 'Marque se a entry produz output reprodutível dado o mesmo input. Não-determinístico (default em LLMs) significa que cada execução pode variar.'
          },
          {
            name: 'Soberania de dados',
            required: false,
            options: ['Sem restrição', 'BR (Brasil)', 'EU (União Europeia)', 'US (Estados Unidos)'],
            body: 'Onde os dados podem trafegar/residir. Crítico para entries que processam PII ou saúde sujeitas a LGPD/GDPR.'
          },
          {
            name: 'Notas adicionais',
            required: false,
            body: 'Texto livre para informações que não cabem nas flags (ex: "PII pseudonimizada antes do storage", "rate-limited a 100 req/min").'
          }
        ]
      },
      {
        kind: 'campos',
        title: 'Passo 4 — Revisão',
        items: [
          {
            name: 'Confirmar e Submeter para Revisão',
            body: 'Botão final: dispara a criação da entry em draft, salva a divulgação de capacidade, submete para fila Root e redireciona para a página da entry. O processo é atômico para o usuário; se algum passo falha, a UI mostra erro acionável.'
          },
          {
            name: 'Já existe esta versão',
            body: 'Se a combinação tipo + nome + versão já foi publicada antes, aparece uma caixa âmbar com botão "Voltar e ajustar a versão" que pré-preenche a próxima patch (1.0.0 → 1.0.1). Não tente apagar a entry anterior — versionar é o caminho correto.'
          }
        ]
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Publicar um agente que eu acabei de criar', body: 'Em /agents, clique no ícone "Publicar no Catálogo" do agente. O wizard abre já com kind=agent e artifact_id pré-selecionados; você pula direto para o passo 2 (Metadata).' },
          { title: 'Registrar uma plataforma externa contratada', body: 'No passo 1, escolha "Registrar Plataforma Externa". Os passos seguintes pedem dados de vendor, contrato e custo mensal. Útil para inventário regulatório e chargeback.' },
          { title: 'Criar um recipe que encadeia 3 agentes', body: 'No passo 1, "Construir Recipe". Termine o wizard normalmente; depois entre em /catalog/{id} → aba "Recipe Steps" para definir a sequência de invocações.' },
          { title: 'Republicar uma entry com novas capacidades', body: 'Suba a versão (ex: 1.0.0 → 1.1.0) e refaça o passo 3 marcando as novas flags. Versões anteriores continuam disponíveis para quem já depende delas.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Versão semver é obrigatória', severity: 'warning', body: 'Strings como "v1" ou "beta" não passam na validação. Use sempre o formato MAJOR.MINOR.PATCH (1.0.0, 2.3.1, etc.).' },
          { title: 'URN duplicada', severity: 'info', body: 'A combinação tipo + nome + versão é o identificador único. Se já existe, a UI mostra caixa âmbar com botão para subir a versão automaticamente. Não delete a entry anterior — versionar é a forma correta.' },
          { title: 'Divulgação de capacidade não é opcional', severity: 'warning', body: 'Root precisa da divulgação preenchida para aprovar. As 12 flags + soberania são parte do contrato de governança — descreva o que a entry faz, mesmo que parcialmente.' },
          { title: 'Visibility "Departamento" exige scope', severity: 'info', body: 'Se escolher visibility=department, declare um domínio no scope (ex: "fiscal"). Sem isso a entry fica invisível para todos os usuários que não são owner/Root.' },
          { title: 'External Platform precisa de vendor', severity: 'warning', body: 'Para kind=external_platform, o campo "vendor" no passo 3 é obrigatório. Sem ele a entry não é criada. Outros campos da metadata externa são refináveis depois em /catalog/{id}.' }
        ]
      }
    ],
    related: ['catalog', 'agents', 'skills']
  },

  // ═════════════════════════════════════════════════════════════════
  // /catalog/cost — Custo & Consumo
  // ═════════════════════════════════════════════════════════════════
  // ═════════════════════════════════════════════════════════════════
  // /catalog/queue — Fila de revisão Root
  // ═════════════════════════════════════════════════════════════════
  catalog_queue: {
    title: 'Fila de Revisão',
    summary: 'Onde Root revisa submissões do Catálogo antes de publicar. Pré-verificações rodam automático no submit; aqui você vê o relatório e decide.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Toda entry submetida ao Catálogo (agent, skill, recipe, plataforma externa) cai aqui antes de ficar disponível. <strong>Só usuários com papel <code>root</code></strong> têm acesso. A fila é o ponto único de governança — sem aprovação aqui, nada vira <code>published</code>.</p>
          <p>Pré-verificações automáticas rodam no momento do submit (cobertura da divulgação, formatação de URN, vendor obrigatório para externas). Elas são <em>informativas</em>: você pode aprovar mesmo com aviso, ou rejeitar mesmo com tudo verde.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Cada submissão grava uma row em <code>catalog_submissions</code> com FK para <code>catalog_entries.id</code>. Lifecycle: <code>pending</code> → <code>approved</code> / <code>rejected</code> / <code>changes_requested</code>. A decisão atualiza a entry (status vira <code>approved</code> ou volta para <code>draft</code>) e dispara audit log.</p>
          <p>Submissões órfãs (entry deletada após submit) são filtradas via INNER JOIN — não poluem a fila.</p>
          <p>Filtros disponíveis: por status, por kind (agent/skill/recipe/external_platform), por submitter (user_id). Contador de pendentes no topo de cada aba.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Aprovar um agent simples', body: 'Clique na submissão → leia a divulgação + pré-verificações → "Aprovar". A entrada vai para publicada e fica visível conforme a visibilidade (privada/departamento/empresa).' },
          { title: 'Pedir mudanças sem rejeitar', body: 'Use "Pedir mudanças" com comentário objetivo (ex: "Falta marcar processa_pii"). Submitter recebe a entry de volta em draft para corrigir e re-submeter.' },
          { title: 'Triagem em lote por kind', body: 'Filtre por kind=recipe quando o time de FinOps pedir revisão de recipes específicos. Aprova/rejeita em sequência sem perder contexto.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Pré-verificações não bloqueiam', severity: 'info', body: 'Avisos em pré-verificações são informativos. Root decide se aprovar mesmo assim — a pré-verificação é insumo, não veto.' },
          { title: 'Rejeitar não deleta', severity: 'info', body: 'Status volta para draft e fica visível ao owner. Para apagar de verdade, vá em /catalog/{id} → DELETE (só draft/archived).' },
          { title: 'Visível só para Root', severity: 'warning', body: 'Usuários comuns recebem 403 ao tentar acessar /catalog/queue. Se você está vendo "Acesso restrito", peça promoção de papel ou use Curadoria.' }
        ]
      }
    ],
    related: ['catalog', 'catalog_inventory', 'catalog_stewardship']
  },

  // ═════════════════════════════════════════════════════════════════
  // /catalog/inventory — Inventário regulatório
  // ═════════════════════════════════════════════════════════════════
  catalog_inventory: {
    title: 'Inventário Regulatório',
    summary: 'Cruza entries com a divulgação de capacidade. Para comitê de privacidade/segurança: quais entries processam PII, dados sensíveis, chamam APIs externas, têm soberania específica.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Painel de <strong>compliance da IA na empresa</strong>. Cruza todas as entries publicadas com as flags da divulgação de capacidade (PII, saúde, biométrico, output não-determinístico, dados de treino, soberania, etc).</p>
          <p>Resposta para perguntas que aparecem em auditoria: "quais agents processam dados clínicos?", "que entries chamam APIs externas para US?", "temos algo com input virando training data?". <strong>Só Root acessa</strong> — não é dashboard operacional, é instrumento de auditoria.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Source de verdade: tabela <code>catalog_capability_disclosure</code> (1:1 com <code>catalog_entries</code>). Por padrão lista entries de qualquer status; <code>status</code> é apenas um filtro opcional — use-o para focar em <code>published</code>/<code>deprecated</code>, que são as que estão de fato em uso.</p>
          <p><strong>Filtros tristate</strong>: cada flag tem 3 estados — "não filtra" (vazio), "marca como true", "marca como false". Permite drill-down combinatório (ex: processa_pii=true E soberania=BR).</p>
          <p>Filtros adicionais: kind (agent/skill/recipe/external_platform), status, residência (texto livre — BR/EU/US/global).</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Resposta para LGPD/GDPR', body: 'Filtre processa_pii=true → exporte CSV → entregue ao DPO. Cada linha lista a entry, owner, soberania declarada e visibility.' },
          { title: 'Auditoria de saúde (CFM/ANS)', body: 'Filtre processa_saude=true → revisão de cada entry para verificar bases legais + retenção. Aproveite para conferir se a visibility está restrita.' },
          { title: 'Mapeamento de fornecedores externos', body: 'Filtre kind=external_platform → lista de ChatGPT, Cursor, Claude, etc contratados. Útil para renegociar contratos ou consolidar.' },
          { title: 'Quem usa modelos não-determinísticos', body: 'Filtre output_deterministico=false → entries que podem variar entre execuções. Pode ser sinal para revisar contratos SLA.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Tristate confunde', severity: 'warning', body: '"Vazio" não é o mesmo que "false". Vazio = não filtra; false = filtra explicitamente quem marcou false. Cuidado para não excluir entries por engano.' },
          { title: 'Divulgação incompleta vira ausência', severity: 'info', body: 'Entries antigas sem divulgação preenchida NÃO aparecem em filtros de flag. Para incluí-las, peça ao owner para reabrir submissão e preencher.' },
          { title: 'Residency é string livre', severity: 'info', body: 'O campo aceita texto (BR, EU, US, global, "BR e EU"). Não há validação de enum — bom para flexibilidade, ruim para queries agregadas. Padronize com seu time.' }
        ]
      }
    ],
    related: ['catalog', 'catalog_queue', 'catalog_stewardship']
  },

  // ═════════════════════════════════════════════════════════════════
  // /catalog/stewardship — Painel de stewards de área
  // ═════════════════════════════════════════════════════════════════
  catalog_stewardship: {
    title: 'Curadoria',
    summary: 'Entries agrupadas por área responsável. Detecta órfãs (owner inativo), paradas (30+ dias sem uso) e baixa confiabilidade. Aberto a quem tem domains, não só Root.',
    sections: [
      {
        kind: 'concept',
        title: 'O que é',
        body: `
          <p>Visão por <strong>área de negócio</strong> (fiscal, RH, jurídico, etc) das entries publicadas. Cada steward vê só os domínios aos quais ele está associado (campo <code>users.domains</code>); Root vê tudo.</p>
          <p>Foco em <em>saúde operacional</em>, não em compliance: o que está esquecido, sem dono, parado ou pouco confiável.</p>
        `
      },
      {
        kind: 'fundamentos',
        title: 'Fundamentos',
        body: `
          <p>Agrupa entries por <code>steward_team</code> (área responsável declarada na entry). Para cada grupo, calcula 3 sinais visuais:</p>
          <ul>
            <li><strong>Órfãs</strong>: <code>owner_user_id</code> aponta para usuário inativo (deletado ou sem login recente). Sinal de offboarding mal-feito.</li>
            <li><strong>Paradas</strong> (stale): published há 30+ dias sem registro de invocação em <code>catalog_recipe_executions</code> ou <code>interactions</code>. Pode estar obsoleta.</li>
            <li><strong>Baixa confiabilidade</strong>: <code>trust_reliability &lt; 0.5</code> (execuções completas ÷ finalizadas). Recalculado pelo motor a cada execução real.</li>
          </ul>
          <p>Quem você vê: Root vê todos os times; um curador não-root vê apenas os <code>steward_team</code> presentes em <code>user.domains</code> (sem domínios = lista vazia, por design).</p>
          <p>Cards de stat no topo viram <strong>filtros clicáveis</strong> (toggle on/off) — útil para focar só no que precisa de atenção.</p>
        `
      },
      {
        kind: 'casos_de_uso',
        title: 'Casos de uso',
        items: [
          { title: 'Limpeza trimestral de área', body: 'Clique no card "Paradas" → lista entries sem uso há 30+ dias. Decide: deprecar, arquivar ou contatar o owner pra confirmar uso real.' },
          { title: 'Reatribuir órfãs', body: 'Clique em "Órfãs" → para cada entry, abra detail e use "Reatribuir owner" (Root) ou peça ao Root para fazer. Sem owner ativo, a entry não tem responsável claro.' },
          { title: 'Onboarding de steward novo', body: 'Root associa o usuário aos domínios via /users → /catalog/stewardship vira a vista padrão da pessoa. Sabe o que precisa cuidar sem ter que decorar nada.' }
        ]
      },
      {
        kind: 'pegadinhas',
        title: 'Pegadinhas',
        items: [
          { title: 'Sem domains = tela vazia', severity: 'warning', body: 'Usuário comum sem nenhum domínio cadastrado vê mensagem "associe-se a domínios". Não é bug — é design (steward sem domínio não tem o que stewardar).' },
          { title: 'Sandbox conta como uso', severity: 'info', body: 'Execuções sandbox (botão 🧪) entram no cálculo de "parada". Se um agent só roda em testes, ele não vai aparecer como stale mesmo que ninguém o use em produção.' },
          { title: 'Confiabilidade vem de execuções reais', severity: 'info', body: 'A coluna "confiabilidade" é trust_reliability = execuções completed ÷ finished, recalculada pelo motor a cada execução real. Execuções sandbox e federadas NÃO contam (não envenenam o número do dono). Entry recém-publicada sem nenhuma execução real ainda aparece sem confiabilidade — rode-a para gerar sinal (sandbox não conta).' }
        ]
      }
    ],
    related: ['catalog', 'catalog_queue', 'catalog_inventory']
  },

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
          <p><strong>Anomalias</strong> são calculadas sob demanda quando você abre o painel (<code>GET /cost/anomalies</code>): pico (hoje ≥ 3× média dos últimos 7d, com baseline ≥ $1) e limite global (hoje > $100). Banner vermelho aparece quando há anomalia ativa.</p>
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
