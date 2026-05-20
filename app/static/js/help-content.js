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
  }

  // Outras páginas migram nos próximos PRs. Páginas não migradas usam
  // o helpContent legado de base.html (com 3 abas: O que é / Fundamento / Como usar).
};
