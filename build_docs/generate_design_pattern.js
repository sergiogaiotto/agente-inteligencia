// Gerador do documento "Design Pattern: SKILL-Sovereign Agent Mesh"
// Saida: docs/design_pattern_SKILL_Sovereign_Agent_Mesh.docx

const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, HeadingLevel, LevelFormat, BorderStyle, WidthType,
  ShadingType, PageBreak, TableOfContents, PageNumber, Footer, Header,
  TabStopType, TabStopPosition,
} = require('docx');

// ====================================================================
// HELPERS
// ====================================================================

const FONT = 'Calibri';
const MONO = 'Consolas';

function p(text, opts = {}) {
  if (Array.isArray(text)) {
    return new Paragraph({
      spacing: { after: 120 },
      ...opts,
      children: text,
    });
  }
  return new Paragraph({
    spacing: { after: 120 },
    ...opts,
    children: [new TextRun({ text, font: FONT })],
  });
}

function r(text, opts = {}) {
  return new TextRun({ text, font: FONT, ...opts });
}

function rb(text) { return r(text, { bold: true }); }
function ri(text) { return r(text, { italics: true }); }
function rcode(text) { return new TextRun({ text, font: MONO, size: 20 }); }

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 200 },
    children: [new TextRun({ text, font: FONT, bold: true, size: 36, color: '1F3864' })],
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 280, after: 160 },
    children: [new TextRun({ text, font: FONT, bold: true, size: 28, color: '2E5395' })],
  });
}

function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 220, after: 120 },
    children: [new TextRun({ text, font: FONT, bold: true, size: 24, color: '2E5395' })],
  });
}

function h4(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_4,
    spacing: { before: 180, after: 100 },
    children: [new TextRun({ text, font: FONT, bold: true, size: 22, color: '404040' })],
  });
}

function bullet(text, level = 0) {
  if (Array.isArray(text)) {
    return new Paragraph({
      numbering: { reference: 'bullets', level },
      spacing: { after: 80 },
      children: text,
    });
  }
  return new Paragraph({
    numbering: { reference: 'bullets', level },
    spacing: { after: 80 },
    children: [new TextRun({ text, font: FONT })],
  });
}

function num(text, level = 0) {
  if (Array.isArray(text)) {
    return new Paragraph({
      numbering: { reference: 'numbers', level },
      spacing: { after: 80 },
      children: text,
    });
  }
  return new Paragraph({
    numbering: { reference: 'numbers', level },
    spacing: { after: 80 },
    children: [new TextRun({ text, font: FONT })],
  });
}

function code(lines) {
  // Cada linha vira um paragrafo com fundo cinza-claro e fonte mono
  const arr = Array.isArray(lines) ? lines : lines.split('\n');
  return arr.map((line) => new Paragraph({
    spacing: { after: 0, line: 240 },
    shading: { fill: 'F4F4F4', type: ShadingType.CLEAR },
    children: [new TextRun({ text: line || ' ', font: MONO, size: 18 })],
  }));
}

function callout(label, text) {
  return new Paragraph({
    spacing: { before: 100, after: 160 },
    border: {
      left: { style: BorderStyle.SINGLE, size: 24, color: '2E75B6', space: 8 },
    },
    shading: { fill: 'EAF2FA', type: ShadingType.CLEAR },
    children: [
      new TextRun({ text: label + ' ', font: FONT, bold: true, color: '1F3864' }),
      new TextRun({ text, font: FONT }),
    ],
  });
}

function divider() {
  return new Paragraph({
    spacing: { before: 200, after: 200 },
    border: {
      bottom: { style: BorderStyle.SINGLE, size: 6, color: '888888', space: 1 },
    },
    children: [new TextRun({ text: '', font: FONT })],
  });
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

// Tabela compacta agnostica
function tableSimple(headers, rows, columnWidths) {
  const totalWidth = columnWidths.reduce((a, b) => a + b, 0);
  const border = { style: BorderStyle.SINGLE, size: 2, color: 'BFBFBF' };
  const borders = { top: border, bottom: border, left: border, right: border };

  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => new TableCell({
      borders,
      width: { size: columnWidths[i], type: WidthType.DXA },
      shading: { fill: '1F3864', type: ShadingType.CLEAR },
      margins: { top: 100, bottom: 100, left: 120, right: 120 },
      children: [new Paragraph({
        children: [new TextRun({ text: h, font: FONT, bold: true, color: 'FFFFFF', size: 20 })],
      })],
    })),
  });

  const bodyRows = rows.map((row, ri) => new TableRow({
    children: row.map((cell, ci) => new TableCell({
      borders,
      width: { size: columnWidths[ci], type: WidthType.DXA },
      shading: ri % 2 === 0
        ? { fill: 'FFFFFF', type: ShadingType.CLEAR }
        : { fill: 'F7F9FC', type: ShadingType.CLEAR },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: typeof cell === 'string'
        ? [new Paragraph({ children: [new TextRun({ text: cell, font: FONT, size: 20 })] })]
        : (Array.isArray(cell) ? cell : [cell]),
    })),
  }));

  return new Table({
    width: { size: totalWidth, type: WidthType.DXA },
    columnWidths,
    rows: [headerRow, ...bodyRows],
  });
}

// ====================================================================
// CONTEUDO
// ====================================================================

const cover = [
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 2400, after: 200 },
    children: [new TextRun({ text: 'DESIGN PATTERN', font: FONT, size: 28, bold: true, color: '888888' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 200, after: 200 },
    children: [new TextRun({ text: 'SKILL-Sovereign Agent Mesh', font: FONT, size: 56, bold: true, color: '1F3864' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 200, after: 1200 },
    children: [new TextRun({ text: 'Padrao Arquitetural Agnostico para Sistemas Multi-Agentes Inteligentes', font: FONT, size: 28, italics: true, color: '404040' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 200, after: 200 },
    children: [new TextRun({ text: 'Versao 1.0', font: FONT, size: 24, color: '404040' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 200 },
    children: [new TextRun({ text: 'Maio de 2026', font: FONT, size: 22, color: '404040' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 2400 },
    children: [new TextRun({ text: 'Sergio Gaiotto', font: FONT, size: 22, color: '404040' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 1600, after: 100 },
    children: [new TextRun({ text: 'Documento de referencia para arquitetos e desenvolvedores', font: FONT, size: 18, italics: true, color: '888888' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 50, after: 100 },
    children: [new TextRun({ text: 'que projetam sistemas multi-agentes baseados em LLM.', font: FONT, size: 18, italics: true, color: '888888' })],
  }),
  pageBreak(),
];

// ====================================================================
// INTRODUCAO
// ====================================================================

const introducao = [
  h1('Introducao'),
  p('Este documento apresenta um padrao arquitetural completo, agnostico de stack tecnologica, para a construcao de sistemas multi-agentes inteligentes baseados em LLM. O padrao e' + ' resultado da destilacao de uma plataforma real em producao e foi sistematizado para que outras equipes possam adota-lo, parcial ou integralmente, em projetos com requisitos similares.'),

  h2('Como ler este documento'),
  p('O documento e' + ' organizado em duas partes complementares:'),
  bullet([rb('Parte A '), r('apresenta o macro-pattern '), ri('SKILL-Sovereign Agent Mesh '), r('(SSAM) como blueprint completo, no formato classico de Gang of Four estendido com secoes praticas de implementacao.')]),
  bullet([rb('Parte B '), r('decompoe o macro-pattern em sete sub-patterns que podem ser adotados de forma isolada ou combinada, conforme o contexto e maturidade da equipe.')]),
  p('A Parte A e' + ' indicada para arquitetos que precisam de uma visao integrada do problema. A Parte B e' + ' indicada para desenvolvedores que ja' + ' tem uma arquitetura em construcao e querem incorporar partes especificas deste padrao.'),

  h2('Premissa'),
  p('Sistemas multi-agentes baseados exclusivamente em prompts livres tendem a sofrer de tres patologias correlacionadas:'),
  num('Comportamento nao-deterministico difícil de auditar.'),
  num('Acoplamento entre logica de orquestracao e texto de prompt.'),
  num('Drift silencioso quando provedores de LLM, modelos ou prompts mudam.'),
  p('O padrao apresentado neste documento ataca as tres patologias com um principio central: a identidade funcional, capacidades, contratos de entrada/saida e politicas de seguranca de cada agente sao declarados em um artefato versionado lido pelo runtime. O LLM atua exclusivamente dentro do espaco declarado, nunca fora dele.'),

  h2('Agnosticismo'),
  p('Este documento nao prescreve linguagem de programacao, framework web, banco de dados ou provedor de LLM. Os exemplos de pseudo-codigo sao puramente ilustrativos e usam sintaxe convencional. As escolhas de implementacao concretas (linguagem, motor de grafos, store de estado, engine de templates) sao explicitadas nas secoes de Implementacao como decisoes a serem tomadas pela equipe adotante, com tradeoffs documentados.'),

  h2('Convencoes terminologicas'),
  p('Adotamos uma terminologia de tres camadas hierarquicas para os agentes, neutra em relacao a frameworks especificos:'),
  bullet([rb('Orchestrator '), r('(camada 1) interpreta intencao em linguagem natural e decide para qual processo de negocio rotear.')]),
  bullet([rb('Router '), r('(camada 2) representa um processo de negocio discreto e decompoe-o em um grafo de tarefas.')]),
  bullet([rb('Worker '), r('(camada 3) e' + ' a unidade atomica de execucao, com escopo limitado e contrato estrito.')]),
  p('Quando o documento original da plataforma de referencia usa siglas como AOBD, AR e SA, este documento usa Orchestrator, Router e Worker. Quando uma fonte externa for relevante, ela aparece em nota lateral.'),

  pageBreak(),
];

// ====================================================================
// PARTE A -- SSAM (MACRO-PATTERN)
// ====================================================================

const parteA = [
  // Cabecalho da parte A
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 600, after: 200 },
    children: [new TextRun({ text: 'PARTE A', font: FONT, size: 22, bold: true, color: '888888' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 100 },
    children: [new TextRun({ text: 'Macro-Pattern', font: FONT, size: 32, bold: true, color: '1F3864' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 600 },
    children: [new TextRun({ text: 'SKILL-Sovereign Agent Mesh', font: FONT, size: 44, bold: true, color: '1F3864' })],
  }),
  divider(),

  // A.1 Intent
  h1('A.1 Intent'),
  callout('Resumo:', 'Definir uma arquitetura na qual a identidade funcional, capacidades e contratos de cada agente sao declarados em um artefato versionado (SKILL) lido pelo runtime; o LLM decide como dentro do espaco declarado, nunca o que fora dele.'),
  p('SSAM (SKILL-Sovereign Agent Mesh) prescreve a separacao radical entre o ' + 'que' + ' um agente e' + ' (declarado, versionado, auditavel) e o ' + 'como' + ' o agente raciocina (delegado ao LLM, mas restrito ao espaco declarado). O artefato SKILL e' + ' a fonte unica de verdade da identidade do agente: nada fora do SKILL muda comportamento; tudo dentro do SKILL e' + ' executavel.'),

  // A.2 Motivacao
  h1('A.2 Motivacao'),
  p('Sistemas multi-agentes maduros sao confrontados com seis problemas recorrentes:'),
  h3('1. Auditoria insuficiente'),
  p('Decisoes geradas por LLM sem mecanismo formal de evidencia produzem respostas plausiveis mas nao verificadas. Em dominios regulados (financeiro, juridico, saude) isso e' + ' inaceitavel: o sistema precisa demonstrar de qual fonte autorizada cada afirmacao foi derivada.'),
  h3('2. Drift silencioso'),
  p('Trocar o modelo, ajustar um prompt ou mudar parametros pode degradar comportamento de forma invisivel. Sem dataset gold e gate de release, regressoes so aparecem em producao.'),
  h3('3. Acoplamento prompt-codigo'),
  p('Quando a logica de orquestracao esta entrelacada ao texto do prompt, equipes nao conseguem evoluir agentes em paralelo: cada mudanca exige merge cuidadoso de codigo + prompt.'),
  h3('4. Versionamento ausente'),
  p('Capacidades de agentes raramente sao tratadas como artefatos com versao. Voltar para uma versao anterior de comportamento exige reconstrucao manual.'),
  h3('5. Alucinacao em dominios de risco'),
  p('LLMs sao otimos a gerar texto fluente, mesmo quando nao tem evidencia. Sem mecanismo de recusa estruturada, o sistema entrega respostas inventadas em casos onde o correto seria recusar.'),
  h3('6. Custo de orquestracao em chamadas determinísticas'),
  p('Muitas tarefas que parecem exigir LLM sao na verdade chamadas HTTP determinísticas com mapeamento previsivel. Usar LLM para executa-las desperdica latencia e custo.'),

  callout('Conclusao:', 'SSAM enderecca os seis problemas com seis principios arquiteturais correlatos.'),

  h2('Os seis principios do SSAM'),
  tableSimple(
    ['Principio', 'O que prescreve'],
    [
      ['1. Contrato declarativo soberano', 'Nenhum comportamento de agente existe fora do declarado em seu SKILL.'],
      ['2. Separacao control plane / data plane', 'Catalogo de capacidades (control) e' + ' fisicamente distinto da execucao de interacoes (data).'],
      ['3. Determinismo declarativo', 'O LLM decide ' + 'como' + ' dentro do espaco que o SKILL permite; nunca o ' + 'que' + ' fora dele.'],
      ['4. Contexto cidadao de primeira classe', 'O contexto entre agentes propaga-se por envelope tipado, append-only, nao por concatenacao de historico.'],
      ['5. Evidencia sobre geracao livre', 'Toda recomendacao e' + ' verificada contra fontes autorizadas antes da entrega.'],
      ['6. Recusa controlada como comportamento correto', 'Evidencia insuficiente resulta em recusa estruturada com proximo passo, nunca silencio ou alucinacao.'],
    ],
    [3000, 6360]
  ),

  // A.3 Aplicabilidade
  h1('A.3 Aplicabilidade'),
  h2('Quando usar SSAM'),
  bullet('Dominios regulados que exigem auditoria de cada decisao.'),
  bullet('Sistemas multi-agente com mais de duas camadas hierarquicas.'),
  bullet('Quando ha' + ' requisito de promocao gradual (staging -> canary -> production) e capacidade de rollback.'),
  bullet('Quando o custo de uma alucinacao e' + ' alto (financeiro, reputacional, juridico).'),
  bullet('Quando equipes diferentes precisam evoluir agentes em paralelo sem conflito de codigo.'),
  bullet('Quando ha' + ' requisito de explicabilidade: cada saida deve poder ser rastreada ate' + ' as fontes que a justificam.'),

  h2('Quando NAO usar SSAM'),
  bullet('Chatbot single-turn sem requisitos de auditoria.'),
  bullet('Prototipos exploratorios em estagio de descoberta.'),
  bullet('Sistemas com um unico agente single-LLM sem hierarquia.'),
  bullet('Quando latencia ultra-baixa (abaixo de 100 ms p95) supera em prioridade auditoria e verificacao.'),
  bullet('Quando o dominio e' + ' tao' + ' aberto que nao ha' + ' fontes autorizadas a consultar (criatividade pura, brainstorming).'),

  callout('Heuristica:', 'Se voce nao consegue articular ' + '"o que esse agente NAO pode fazer"' + ', SSAM ainda nao e' + ' o padrao certo para o seu projeto. SSAM exige um espaco de comportamento declaravel.'),

  // A.4 Estrutura
  h1('A.4 Estrutura'),
  p('SSAM e' + ' organizado em sete planos logicos. Cada plano e' + ' independentemente substituivel sem afetar os demais.'),

  h2('Visao geral em sete planos'),
  ...code([
    '+--------------------------------------------------------+',
    '|  PLANO 7: Release & Evaluation                         |',
    '|    Version Registry · Gold Dataset · Release Gate      |',
    '+--------------------------------------------------------+',
    '|  PLANO 6: Observability                                |',
    '|    Trace · Audit Log · Drift Detection                 |',
    '+--------------------------------------------------------+',
    '|  PLANO 5: Execution Engine                             |',
    '|    Interaction FSM · Reflect-Reason Harness · Policy   |',
    '+--------------------------------------------------------+',
    '|  PLANO 4: Evidence Runtime                             |',
    '|    Retriever · Reranker · Independent Verifier         |',
    '+--------------------------------------------------------+',
    '|  PLANO 3: Communication                                |',
    '|    A2A Envelope · Intent Descriptor · Context Delta    |',
    '+--------------------------------------------------------+',
    '|  PLANO 2: Agent Mesh                                   |',
    '|    Orchestrator <-> Router <-> Worker                  |',
    '+--------------------------------------------------------+',
    '|  PLANO 1: Declarative Catalog                          |',
    '|    SKILL Registry · Tool Registry · Connector Registry |',
    '+--------------------------------------------------------+',
  ]),

  h2('A.4.1 Plano 1 - Catalogo Declarativo'),
  p('Repositorio versionado de tres tipos de artefatos:'),
  bullet([rb('SKILL Registry: '), r('cada SKILL declara identidade, capacidades, workflow, tool bindings, output contract, failure modes, guardrails e telemetria de um agente.')]),
  bullet([rb('Tool Registry: '), r('inventario de operacoes disponiveis (locais ou remotas via protocolo de tools), com sensibilidade, custo e exigencia de contexto confiavel.')]),
  bullet([rb('Connector Registry: '), r('catalogo de endpoints HTTP externos com secrets isolados, autenticacao centralizada e roteamento por id, nunca por URL crua.')]),

  h2('A.4.2 Plano 2 - Agent Mesh'),
  p('Tres camadas hierarquicas, todas executando o mesmo motor mas com responsabilidades distintas:'),
  tableSimple(
    ['Camada', 'Responsabilidade primaria', 'Cardinalidade'],
    [
      ['Orchestrator', 'Interpretar texto natural -> Intent Descriptor; rotear ao processo de negocio.', '1 por dominio de negocio'],
      ['Router', 'Hidratar SKILL do processo, planejar DAG, ativar workers.', '1 por processo de negocio'],
      ['Worker', 'Executar tarefa atomica conforme contrato do SKILL.', 'N por tarefa do DAG'],
    ],
    [2200, 5500, 1660]
  ),

  h2('A.4.3 Plano 3 - Comunicacao'),
  p('Toda comunicacao inter-agente atravessa um envelope tipado que carrega rastreabilidade, intencao, referencia ao SKILL, contexto, orcamento e prazo. Mudancas no contexto sao append-only via Context Delta.'),

  h2('A.4.4 Plano 4 - Evidence Runtime'),
  p('Pipeline de tres componentes obrigatorios entre rascunho e entrega:'),
  num('Retriever busca em fontes autorizadas (com filtro por confidencialidade).'),
  num('Reranker reordena por relevancia ao contexto especifico.'),
  num('Verificador independente do gerador avalia consistencia, cobertura, conflito e risco.'),

  h2('A.4.5 Plano 5 - Motor de Execucao'),
  p('Maquina de estados de nove estados orquestra cada interacao do principio ao fim, com VerifyEvidence obrigatorio antes de qualquer entrega. Para tarefas que exigem raciocinio iterativo, o Reflect-Reason Harness opera dentro de DraftAnswer com limite duro de iteracoes.'),

  h2('A.4.6 Plano 6 - Observabilidade'),
  p('Cada transicao gera entrada em audit log append-only. Cada chamada LLM/tool/API gera evidencia tipada com hashes de request/response. Drift detection compara metricas correntes contra baseline da release.'),

  h2('A.4.7 Plano 7 - Release & Avaliacao'),
  p('Releases sao composicoes versionadas de (modelo + prompt + indice de evidencia + politica). Promocao staging -> canary -> production e' + ' bloqueada por gate de qualidade contra dataset gold adversarial. Rollback e' + ' troca atomica de pointer.'),

  // A.5 Participantes
  h1('A.5 Participantes'),
  p('Os papeis estruturais do padrao, com responsabilidade unica por papel:'),
  tableSimple(
    ['Papel', 'Responsabilidade'],
    [
      ['SKILL Artifact', 'Contrato versionado declarando frontmatter + secoes obrigatorias e opcionais. E' + ' a alma semantica do agente.'],
      ['Skill Parser', 'Le SKILL, valida estrutura, extrai secoes, computa hash de integridade. Tolerante a warnings, intolerante a inexistencia.'],
      ['Orchestrator Agent', 'Interpreta linguagem natural, gera Intent Descriptor estruturado, consulta Router Catalog, delega via envelope assinado.'],
      ['Router Catalog', 'Registro de routers ativos por dominio, com keywords de ativacao, taxa de sucesso e latencia p95.'],
      ['Router Agent', 'Hidrata SKILL do processo, valida contra schema, decompoe workflow em DAG, ativa workers com depends_on.'],
      ['Worker Agent', 'Carrega SKILL como contrato efetivo, invoca tools/APIs sob condicoes prescritas, emite resultado tipado conforme Output Contract.'],
      ['A2A Envelope', 'Estrutura tipada propagando trace_id, span_id, intent, skill_ref, context, budget, deadline e signature.'],
      ['Intent Descriptor', 'Estrutura tipada com domain, process_candidate, entities, constraints, urgency e actor.'],
      ['Context Delta', 'Mutacao append-only do contexto compartilhado. Nunca sobrescreve.'],
      ['Retriever', 'Busca evidencias em fontes autorizadas, com filtro por confidencialidade.'],
      ['Reranker', 'Reordena resultados do retriever por relevancia ao contexto da query.'],
      ['Evidence Checker', 'Verificador independente do gerador. Avalia consistencia, cobertura, conflito e risco.'],
      ['Interaction FSM', 'Maquina de nove estados; transicoes sao atomicas e auditadas.'],
      ['Reflect-Reason Harness', 'Loop limitado de raciocinio com auto-reflexao contra Output Contract e Guardrails.'],
      ['Policy Engine', 'Avalia permissoes antes de retrieve e draft. Decisoes negativas geram Refuse imediato.'],
      ['Tool Registry', 'Inventario declarativo de capacidades. Cada tool tem custo, sensibilidade e requisitos de contexto.'],
      ['Connector Registry', 'Catalogo de endpoints HTTP externos. SKILL referencia connector_id, nunca URL.'],
      ['Audit Log', 'Ledger append-only de toda transicao significativa com actor, action, entity e details.'],
      ['Version Registry', 'Composicoes release; cada release e' + ' (model_config + prompt_config + index_config + policy_config).'],
      ['Harness Evaluator', 'Executa releases contra dataset gold; calcula acuracia, recusa correta, falso positivo, regressao.'],
    ],
    [2400, 6960]
  ),

  // A.6 Colaboracoes
  h1('A.6 Colaboracoes'),
  p('Sequencia canonica de uma interacao bem-sucedida do principio ao fim:'),

  h2('Fluxo principal'),
  num('Cliente envia texto natural a interface.'),
  num('Interface persiste turno e cria contexto de interacao; FSM transita Intake.'),
  num('PolicyCheck consulta Policy Engine; permissao negada gera Refuse imediato.'),
  num('Orchestrator gera Intent Descriptor via LLM (formato estruturado, JSON validado).'),
  num('Orchestrator consulta Router Catalog; faz match hibrido por keywords + score de sucesso.'),
  num('Orchestrator emite envelope assinado para o Router eleito.'),
  num('Router carrega SKILL, valida contra schema, decompoe workflow em DAG.'),
  num('Router emite envelopes para Workers (paralelo onde nao ha' + ' depends_on, sequencial caso contrario).'),
  num('Cada Worker carrega SKILL como system prompt efetivo; FSM entra em RetrieveEvidence.'),
  num('Retriever busca em fontes autorizadas; Reranker reordena top-N.'),
  num('FSM entra em DraftAnswer; Reflect-Reason Harness produz rascunho dentro do orcamento.'),
  num('FSM entra em VerifyEvidence; verificador independente avalia (consistencia, cobertura, conflito, risco).'),
  num('Decisao terminal: Recommend (verificacao OK) | Refuse (evidencia insuficiente) | Escalate (risco alto).'),
  num('FSM entra em LogAndClose; estado terminal obrigatorio. Sem este estado a interacao e' + ' considerada vazada.'),
  num('Worker emite Context Delta append-only de volta ao Router.'),
  num('Router agrega resultados conforme Output Contract; quando todos workers terminam, emite resposta consolidada.'),
  num('Orchestrator devolve resposta ao cliente; envelope final assinado.'),
  num('Audit log e trace persistidos. Drift detection compara metricas do turno contra baseline da release ativa.'),

  h2('Fluxos alternativos'),
  h3('A.6.1 Recusa por evidencia insuficiente'),
  p('Quando VerifyEvidence retorna ok=false e a causa e' + ' insuficiencia de evidencia (cobertura abaixo do threshold), a FSM transita para Refuse com payload estruturado contendo motivo e proximo passo (por exemplo, ' + '"consultar fonte X"' + ' ou ' + '"contatar especialista de dominio Y"' + '). Nunca silencio.'),

  h3('A.6.2 Escalada por risco'),
  p('Quando o Verifier identifica risco alto (suspeita de fraude, decisao com impacto acima de threshold, conflito normativo) a FSM transita para Escalate, preservando o contexto completo, incluindo o rascunho rejeitado e o motivo da escalada. A interacao nao e' + ' encerrada do ponto de vista do usuario humano supervisor.'),

  h3('A.6.3 Falha de tool / circuit breaker'),
  p('Quando um tool ou binding declarativo falha (timeout, 5xx, esgotamento de retry), o Worker emite Compensation conforme declarado no SKILL. Se nao houver Compensation, o Worker emite Failure tipado e o Router decide se a falha e' + ' recuperavel (retry com diferente subagente) ou terminal (Refuse com motivo tecnico).'),

  // A.7 Consequencias
  h1('A.7 Consequencias'),

  h2('Beneficios'),
  bullet([rb('Auditoria total: '), r('cada decisao tem trace, evidencia citada e transicao registrada. Para cada saida do sistema voce pode reconstruir exatamente quais fontes a justificam.')]),
  bullet([rb('Promocao segura: '), r('SKILL v1 -> v2 com gate de release; rollback e' + ' troca atomica de pointer. Releases mal-sucedidas nao chegam a producao.')]),
  bullet([rb('Paralelismo de equipes: '), r('SKILLs sao artefatos independentes; equipes editam SKILLs distintos sem conflito de merge no codigo.')]),
  bullet([rb('Substituicao de LLM transparente: '), r('o contrato (SKILL) nao muda quando voce troca de provedor; muda apenas a serving config da release.')]),
  bullet([rb('Recusa estruturada: '), r('elimina alucinacao em dominios sensiveis. O sistema entrega respostas verificadas ou recusa explicita; nunca uma terceira via inventada.')]),
  bullet([rb('Reuso de SKILLs: '), r('um SKILL bem definido pode ser invocado por multiplos Routers. Workers especializados (calcular ICMS, validar CPF) viram bibliotecas reutilizaveis.')]),
  bullet([rb('Evolucao independente de planos: '), r('voce pode trocar o Retriever sem mexer no FSM, ou trocar o LLM sem mexer no Tool Registry. Os planos sao acoplados apenas por contratos tipados.')]),

  h2('Tradeoffs'),
  bullet([rb('Curva de adocao: '), r('a equipe precisa internalizar o formato canonico do SKILL antes de produzir agentes uteis. Investimento inicial nao trivial.')]),
  bullet([rb('Latencia adicional: '), r('VerifyEvidence acrescenta uma chamada (a um modelo possivelmente menor) por interacao. Mitigaveis via cache de verificacao e reranker barato, mas nao eliminaveis.')]),
  bullet([rb('Necessita dataset gold: '), r('o gate de release so funciona contra um dataset gold adversarial. Construir e manter este dataset e' + ' trabalho continuo.')]),
  bullet([rb('Mais infra: '), r('envelope, audit log, version registry, connector registry, harness evaluator. SSAM nao e' + ' viavel sem a infraestrutura completa; meia adocao tende a ser pior que zero adocao.')]),
  bullet([rb('Overkill em casos simples: '), r('para um FAQ chatbot single-turn, SSAM e' + ' resposta errada. Use o macro-pattern apenas onde a complexidade justifica.')]),
  bullet([rb('Disciplina de declaracao: '), r('SKILL como contrato exige rigor. Equipes acostumadas a iterar prompt em producao acham SSAM lento de inicio.')]),

  // A.8 Implementacao
  h1('A.8 Implementacao'),
  p('Sequencia recomendada de construcao, do plano mais fundamental ao mais externo. Cada plano deve estar funcional antes do proximo iniciar.'),

  h2('Roadmap de construcao'),
  num('Definir formato canonico do SKILL: frontmatter (id, version, kind, owner, stability) + secoes obrigatorias (Purpose, Activation Criteria, Inputs, Workflow, Tool Bindings, Output Contract, Failure Modes) e opcionais (Delegations, Compensation, Guardrails, Budget, Examples, Telemetry, Data Dependencies, Model Constraints, Evidence Policy, Gold Refs).'),
  num('Implementar parser tolerante: warnings nao bloqueiam criacao; apenas ausencia total de conteudo bloqueia. Hash SHA-256 do conteudo bruto para detectar alteracoes.'),
  num('Modelar Envelope tipado e Context Delta append-only. Validar via tipos estaticos ou schema na fronteira.'),
  num('Modelar FSM com transicoes atomicas. Cada transicao gera entrada em audit log antes de aplicar.'),
  num('Implementar Retriever, Reranker e Evidence Checker como componentes independentes do gerador. O Verifier deve usar um modelo diferente do que produz o rascunho.'),
  num('Construir Reflect-Reason Harness com max_iterations duro. Reflexoes adicionais consomem orcamento; orcamento esgotado encerra com melhor candidato disponivel.'),
  num('Modelar Tool Registry e (se ha' + ' chamadas externas) Connector Registry. URLs sao estaticas; apenas path e query sao templatizaveis.'),
  num('Implementar Audit Log append-only. Imutabilidade pode ser garantida por particionamento temporal e/ou hashing em cadeia.'),
  num('Modelar Release com gate explicito. Release rejeitada pelo gate nao pode ser promovida; apenas marcada como `rejected`.'),
  num('Construir observabilidade: trace por interacao, span por chamada, drift detection comparando metricas correntes vs baseline.'),

  h2('Decisoes-chave de design'),
  tableSimple(
    ['Decisao', 'Recomendacao', 'Justificativa'],
    [
      ['Frontmatter format', 'YAML', 'Mais legivel por humanos nao-tecnicos do que JSON ou TOML; suportado por todas as linguagens.'],
      ['Mutacao de contexto', 'Append-only via Delta', 'Permite reconstrucao exata do estado em qualquer ponto do trace; auditoria forte.'],
      ['Verificacao de evidencia', 'LLM independente do gerador', 'Mesmo LLM verificando si proprio sofre de self-grading bias; usar modelo diferente (idealmente menor e mais estrito).'],
      ['Templating de inputs', 'Engine sandboxed', 'Templates com acesso a sistema (eval, file I/O, etc.) sao vetor de execucao remota; sandboxed elimina superficie.'],
      ['URL de connector', 'Base estatica', 'Permitir URL inteira templatizavel cria SSRF; apenas path e query podem variar.'],
      ['Idempotencia', 'Obrigatoria em POST/PATCH/DELETE', 'Retry seguro requer idempotency key; sem ela, duplicacao em dominios financeiros e' + ' catastrofica.'],
      ['Politica de retry', 'Apenas em 5xx e timeout', '4xx indica erro do cliente (input ruim); retry de 4xx so reproduz o problema.'],
      ['Verificacao do mesmo modelo', 'Proibida', 'Evita confianca em uma unica fonte de raciocinio.'],
      ['Identidade SKILL (urn)', 'urn:skill:<dominio>:<processo>:<tarefa>', 'Hierarquia explicita facilita catalogacao e descoberta.'],
      ['Fallback de Verifier', 'Heuristica baseada em score', 'LLM Verifier pode falhar; threshold de relevancia agregada cobre o caso degradado.'],
    ],
    [2400, 2400, 4560]
  ),

  h2('Anti-patterns na implementacao'),
  bullet([rb('SKILL como documentacao: '), r('SKILL deve ser executavel pelo runtime; se ele e' + ' lido apenas por humanos, voce voltou ao mundo prompt+codigo entrelacados.')]),
  bullet([rb('Mutacao de contexto in-place: '), r('destroi auditoria. Sempre Delta append-only.')]),
  bullet([rb('Verificacao com mesmo LLM: '), r('self-grading bias garantido; o gerador da nota alta a si mesmo.')]),
  bullet([rb('URL templatizavel inteira: '), r('vetor classico de SSRF; o atacante injeta URL maliciosa via input do usuario.')]),
  bullet([rb('Pular VerifyEvidence: '), r('elimina o principal mecanismo de defesa contra alucinacao.')]),
  bullet([rb('Reflect ilimitado: '), r('loop infinito quando o LLM nunca alcanca satisfacao do Output Contract; max_iterations e' + ' obrigatorio.')]),
  bullet([rb('Tools fora do registry: '), r('agent invoca tool nao registrada -> sem auditoria, sem custo, sem politica de sensibilidade.')]),

  // A.9 Exemplo
  h1('A.9 Exemplo de uso'),
  callout('Cenario:', 'Apuracao de imposto mensal de uma filial corporativa.'),
  p('Este exemplo ilustra o caminho completo da entrada do usuario ate' + ' a entrega da resposta verificada, percorrendo todos os planos do SSAM.'),

  h3('Entrada do usuario'),
  ...code([
    'Usuario: "Apurar o imposto X de marco de 2026 da filial 07."',
  ]),

  h3('1. Orchestrator gera Intent Descriptor'),
  ...code([
    '{',
    '  "domain": "financeiro",',
    '  "process_candidate": "apuracao_imposto_mensal",',
    '  "entities": {',
    '    "competencia": "2026-03",',
    '    "filial": "07",',
    '    "imposto": "X"',
    '  },',
    '  "constraints": {},',
    '  "urgency": "normal",',
    '  "actor": "usuario_corporativo"',
    '}',
  ]),

  h3('2. Orchestrator consulta Router Catalog'),
  p('O Router Catalog retorna o Router `apurador_x` com keywords de ativacao = [imposto X, apuracao X, calcular X], success_rate = 0.96, latency_p95 = 1800 ms.'),

  h3('3. Router decompoe SKILL em DAG'),
  ...code([
    'extrair_notas (worker A, paralelo) ----+',
    'extrair_credito (worker B, paralelo) --+--> calcular_balanco (worker D)',
    'extrair_debito (worker C, paralelo) ---+              |',
    '                                                      v',
    '                                            gerar_guia (worker E)',
  ]),

  h3('4. Worker E (gerar guia) executa'),
  bullet('Carrega SKILL `gerar_guia_imposto_x` como system prompt efetivo.'),
  bullet('Retriever busca em base autorizada `legislacao_imposto_x` (confidencialidade=public).'),
  bullet('Reranker prioriza artigos diretamente aplicaveis ao caso.'),
  bullet('Reflect-Reason Harness produz rascunho com calculo + citacoes.'),
  bullet('Verifier (LLM independente) avalia: consistencia=ok, cobertura=0.97, conflito=nenhum, risco=baixo.'),
  bullet('FSM transita para Recommend.'),

  h3('5. Orchestrator devolve resposta'),
  ...code([
    '{',
    '  "result": "recomendacao",',
    '  "calculo": {',
    '    "credito": 12450.00,',
    '    "debito": 18900.00,',
    '    "balanco": 6450.00',
    '  },',
    '  "evidencias": [',
    '    {"source": "legislacao_imposto_x", "section": "Art. 23"},',
    '    {"source": "legislacao_imposto_x", "section": "Art. 41-A"}',
    '  ],',
    '  "trace_id": "01HX...",',
    '  "release_id": "rel_2026.04_canary"',
    '}',
  ]),

  callout('Nota:', 'Em cada um dos workers, o caminho foi: Intake -> PolicyCheck -> RetrieveEvidence -> DraftAnswer -> VerifyEvidence -> Recommend -> LogAndClose. Em nenhum momento o sistema gera saida sem verificacao.'),

  // A.10 Padroes Relacionados
  h1('A.10 Padroes Relacionados'),
  tableSimple(
    ['Padrao', 'Relacao com SSAM'],
    [
      ['Strategy (GoF)', 'SKILL e' + ' uma estrategia injetada no runtime; o motor executa qualquer SKILL valido.'],
      ['Template Method (GoF)', 'Workflow e' + ' declarado como template; o motor preenche os passos.'],
      ['Chain of Responsibility (GoF)', 'A FSM e' + ' uma cadeia de estados; cada estado e' + ' uma responsabilidade discreta.'],
      ['Saga (Microservices)', 'A secao Compensation do SKILL e' + ' compensacao de saga: como reverter quando uma etapa do DAG falha.'],
      ['Pipes & Filters (EIP)', 'Retriever -> Reranker -> Verifier e' + ' um pipeline classico de filtros sobre evidencias.'],
      ['CQRS', 'Control plane (catalogo de SKILLs) e' + ' separado do data plane (interacoes em execucao).'],
      ['Event Sourcing', 'Audit log + cadeia de envelopes formam um event store reconstrutivel.'],
      ['Bulkhead', 'Circuit breaker por binding declarativo isola falhas de um connector dos demais.'],
      ['Hexagonal / Ports & Adapters', 'Tool Registry e Connector Registry sao adaptadores; o motor depende apenas de portas (interfaces tipadas).'],
      ['Specification Pattern', 'Activation Criteria do SKILL e' + ' uma specification de quando o agente se ativa.'],
    ],
    [2800, 6560]
  ),

  pageBreak(),
];

// ====================================================================
// PARTE B -- CATALOGO DE SUB-PATTERNS
// ====================================================================

// Helper para gerar um sub-pattern com template uniforme
function subPattern({ id, name, also, intent, problem, structure, participants, consequences, implementation, code: codeBlock, when, related }) {
  const out = [];
  out.push(h1(`${id} ${name}`));
  if (also) out.push(p([ri('Tambem conhecido como: '), r(also)]));
  out.push(callout('Intent:', intent));
  out.push(h2('Problema'));
  problem.forEach((line) => out.push(p(line)));
  out.push(h2('Estrutura'));
  if (Array.isArray(structure.text)) structure.text.forEach((t) => out.push(p(t)));
  else out.push(p(structure.text));
  if (structure.diagram) out.push(...code(structure.diagram));
  out.push(h2('Participantes'));
  out.push(tableSimple(['Papel', 'Responsabilidade'], participants, [3000, 6360]));
  out.push(h2('Consequencias'));
  out.push(h3('Beneficios'));
  consequences.benefits.forEach((b) => out.push(bullet(b)));
  out.push(h3('Tradeoffs'));
  consequences.tradeoffs.forEach((t) => out.push(bullet(t)));
  out.push(h2('Implementacao'));
  implementation.forEach((step, i) => out.push(num(step)));
  if (codeBlock) {
    out.push(h2('Pseudo-codigo ilustrativo'));
    out.push(...code(codeBlock));
  }
  out.push(h2('Quando usar'));
  when.forEach((w) => out.push(bullet(w)));
  out.push(h2('Padroes Relacionados'));
  related.forEach((r) => out.push(bullet(r)));
  out.push(pageBreak());
  return out;
}

const parteBHeader = [
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 600, after: 200 },
    children: [new TextRun({ text: 'PARTE B', font: FONT, size: 22, bold: true, color: '888888' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 100 },
    children: [new TextRun({ text: 'Catalogo de Sub-Patterns', font: FONT, size: 32, bold: true, color: '1F3864' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 600 },
    children: [new TextRun({ text: 'Sete padroes adotaveis individualmente ou combinados', font: FONT, size: 22, italics: true, color: '404040' })],
  }),
  divider(),
  p('A Parte A apresenta SSAM como um todo integrado. A Parte B decompoe o macro-pattern em sete sub-patterns que podem ser adotados independentemente. Equipes que ainda nao tem maturidade para implementar SSAM completo podem comecar adotando apenas um ou dois sub-patterns onde o problema e' + ' mais agudo.'),
  p('Cada sub-pattern segue um template uniforme: Intent, Problema, Estrutura, Participantes, Consequencias, Implementacao, Pseudo-codigo, Quando usar, Padroes Relacionados.'),
  h2('Indice da Parte B'),
  bullet('B.1 SKILL-as-Contract'),
  bullet('B.2 Three-Tier Agent Topology'),
  bullet('B.3 A2A Envelope Protocol'),
  bullet('B.4 Append-Only Context Delta'),
  bullet('B.5 Evidence-Verified State Machine'),
  bullet('B.6 Declarative API Binding'),
  bullet('B.7 Reflect-Reason Harness'),
  pageBreak(),
];

// ----- B.1 -----
const subB1 = subPattern({
  id: 'B.1',
  name: 'SKILL-as-Contract',
  also: 'Declarative Agent Identity, Sovereign Skill Manifest',
  intent: 'Definir a identidade funcional, capacidades e contratos de um agente em um artefato declarativo versionado lido pelo runtime, eliminando o entrelacamento entre logica de orquestracao e texto de prompt.',
  problem: [
    'Agentes definidos por prompts livres tem tres patologias: (1) comportamento muda silenciosamente quando alguem edita o prompt, (2) duas equipes nao conseguem evoluir o mesmo agente em paralelo sem conflito de merge, (3) e' + ' impossivel reverter para uma versao anterior de comportamento.',
    'A causa raiz e' + ' que prompt + codigo ficam acoplados em texto livre. Nao ha' + ' contrato. Nao ha' + ' versao. Nao ha' + ' revisao formal de capacidade.',
  ],
  structure: {
    text: 'Um SKILL e' + ' um documento Markdown com frontmatter YAML obrigatorio + secoes nomeadas. O frontmatter declara identidade (id, version, kind, owner, stability). As secoes declaram o contrato executavel.',
    diagram: [
      '+--------------------------------------+',
      '| frontmatter (YAML)                   |',
      '|   id, version, kind, owner, stability|',
      '+--------------------------------------+',
      '| # Nome humano (H1)                   |',
      '+--------------------------------------+',
      '| ## Purpose            (obrigatoria)  |',
      '| ## Activation Criteria (obrigatoria) |',
      '| ## Inputs             (obrigatoria)  |',
      '| ## Workflow           (obrigatoria)  |',
      '| ## Tool Bindings      (obrigatoria)  |',
      '| ## Output Contract    (obrigatoria)  |',
      '| ## Failure Modes      (obrigatoria)  |',
      '| ## Delegations        (opcional)     |',
      '| ## Compensation       (opcional)     |',
      '| ## Guardrails         (opcional)     |',
      '| ## Budget             (opcional)     |',
      '| ## Examples           (opcional)     |',
      '| ## Telemetry          (opcional)     |',
      '| ## Data Dependencies  (opcional)     |',
      '| ## Model Constraints  (opcional)     |',
      '| ## Evidence Policy    (opcional)     |',
      '| ## Gold Refs          (opcional)     |',
      '+--------------------------------------+',
    ],
  },
  participants: [
    ['SKILL Document', 'Markdown + YAML frontmatter; e' + ' a fonte unica de verdade da identidade do agente.'],
    ['Skill Parser', 'Le, valida, extrai secoes, computa hash de integridade. Tolerante a warnings.'],
    ['Skill Registry', 'Persiste SKILLs versionados. Imutaveis apos publicacao; novas versoes sao novos registros.'],
    ['Skill Linter', 'Valida semantica: idempotencia, SSRF, leak de secret, ciclos no DAG, secoes obrigatorias.'],
    ['Runtime Loader', 'Hidrata SKILL ao iniciar uma execucao. Bloqueia se hash difere do registrado.'],
  ],
  consequences: {
    benefits: [
      'Versionamento e' + ' first-class: voltar uma versao de comportamento e' + ' trocar um pointer.',
      'Equipes editam SKILLs distintos em paralelo sem conflito de merge.',
      'Revisao de capacidade e' + ' revisao de codigo: pull request no SKILL.',
      'Hash de integridade detecta alteracoes nao autorizadas.',
      'Catalogo navegavel: dev pode descobrir capacidades existentes antes de duplicar.',
    ],
    tradeoffs: [
      'Disciplina inicial: equipes acostumadas a editar prompt em producao acham SKILL ' + 'lento' + '.',
      'O parser precisa ser tolerante a SKILLs imperfeitos; intolerante demais bloqueia adocao.',
      'Mudancas no formato canonico exigem migration de todos os SKILLs existentes.',
    ],
  },
  implementation: [
    'Definir o conjunto canonico de secoes obrigatorias e opcionais. Documente cada uma com exemplos.',
    'Implementar parser tolerante: warning para secoes faltantes, erro apenas para conteudo inexistente. Retornar objeto estruturado + lista de warnings.',
    'Calcular hash SHA-256 do conteudo bruto; persistir junto com o SKILL.',
    'Implementar linter semantico que rode em CI/CD bloqueando publicacao de SKILLs invalidos.',
    'Modelar versionamento: id estavel + version semver. Nova publicacao = novo registro; SKILLs antigos permanecem acessiveis.',
    'Expor endpoint de preview/validacao que receba SKILL bruto e retorne secoes encontradas + warnings, sem persistir.',
  ],
  code: [
    '---',
    'id: urn:skill:financeiro:apuracao_imposto:gerar_guia',
    'version: 1.2.0',
    'kind: worker',
    'owner: time_fiscal',
    'stability: stable',
    '---',
    '',
    '# Gerar Guia de Imposto Mensal',
    '',
    '## Purpose',
    'Produzir guia de recolhimento mensal a partir de balanco apurado.',
    '',
    '## Activation Criteria',
    '- Recebe balanco apurado (credito, debito, saldo) como input.',
    '- Competencia esta no periodo permitido pelo calendario fiscal.',
    '',
    '## Inputs',
    '- competencia: string formato YYYY-MM',
    '- filial: string formato 99',
    '- balanco: objeto {credito: number, debito: number, saldo: number}',
    '',
    '## Workflow',
    '1. Validar balanco contra schema.',
    '2. Buscar aliquotas vigentes para a competencia.',
    '3. Calcular valor_a_recolher = max(saldo, 0).',
    '4. Gerar codigo de barras conforme padrao FEBRABAN.',
    '5. Persistir guia no Tool `tax_guide_repository`.',
    '',
    '## Tool Bindings',
    '- tax_aliquota_lookup (read)',
    '- tax_guide_repository (write, idempotency_key obrigatorio)',
    '',
    '## Output Contract',
    '- guia_id: uuid',
    '- valor_a_recolher: number',
    '- vencimento: ISO date',
    '- codigo_barras: string 48 char',
    '',
    '## Failure Modes',
    '- ALIQUOTA_NOT_FOUND -> Refuse com proximo passo "atualizar tabela vigente"',
    '- VALOR_NEGATIVO -> Refuse (saldo credor; nao ha' + ' guia)',
    '',
    '## Guardrails',
    '- Nunca emitir guia com valor > 1MM sem Escalate.',
  ],
  when: [
    'Voce tem multiplos agentes com responsabilidades distintas e quer evoluir cada um separadamente.',
    'Voce precisa auditar exatamente o que cada agente pode (e nao pode) fazer.',
    'Voce quer permitir que stakeholders nao-tecnicos revisem capacidade do agente.',
  ],
  related: [
    'Strategy (GoF): SKILL e' + ' a estrategia injetada.',
    'Specification Pattern: Activation Criteria define quando o agente se ativa.',
    'B.6 Declarative API Binding: SKILL referencia bindings declarativos.',
  ],
});

// ----- B.2 -----
const subB2 = subPattern({
  id: 'B.2',
  name: 'Three-Tier Agent Topology',
  also: 'Orchestrator-Router-Worker, Hierarchical Agent Mesh',
  intent: 'Organizar agentes em tres camadas hierarquicas com responsabilidades distintas: Orchestrator interpreta intencao, Router representa processo de negocio, Worker executa tarefa atomica. Cada camada tem cardinalidade e escopo bem definidos.',
  problem: [
    'Sistemas single-agent tentam fazer tudo em um unico contexto: interpretar intencao, escolher processo, executar tarefas. Isso resulta em prompts gigantes, contextos saturados e degradacao de qualidade.',
    'Tentar resolver isso com ' + 'multiplos agentes sem hierarquia' + ' tambem falha: agentes peer-to-peer disputam protagonismo e geram loops de delegacao.',
  ],
  structure: {
    text: 'Hierarquia rigida em tres camadas. Comunicacao apenas entre camadas adjacentes. Sem peer-to-peer.',
    diagram: [
      '         +----------------------------+',
      '         |  ORCHESTRATOR (1 por dom.) |',
      '         |  Interpreta NL -> Intent   |',
      '         |  Roteia ao processo        |',
      '         +-------------+--------------+',
      '                       |',
      '             +---------+---------+',
      '             |                   |',
      '         +---v---+           +---v---+',
      '         |  R1   | ...       |  Rn   |    ROUTERS',
      '         |       |           |       |    (1 por processo)',
      '         +---+---+           +---+---+',
      '             |                   |',
      '       +-----+-----+         +---+----+',
      '       |           |         |        |',
      '     +-v-+      +--v--+    +-v-+   +--v--+',
      '     |W1 |      | W2  |    |W3 |   | Wn  |   WORKERS',
      '     +---+      +-----+    +---+   +-----+   (N por tarefa)',
    ],
  },
  participants: [
    ['Orchestrator', 'Unico por dominio. Interpreta texto natural, gera Intent Descriptor, consulta Router Catalog, delega.'],
    ['Router Catalog', 'Indice de Routers ativos com keywords e metricas; matching hibrido entre Intent e routers.'],
    ['Router', 'Unico por processo de negocio. Decompoe processo em DAG, ativa Workers conforme depends_on, agrega resultados.'],
    ['Worker', 'Atomico por tarefa. Carrega SKILL como system prompt, invoca tools, emite resultado tipado.'],
    ['Output Contract', 'Tipo de retorno declarado em cada SKILL; Router agrega segundo contratos dos Workers.'],
  ],
  consequences: {
    benefits: [
      'Cada camada tem prompt menor e mais focado: melhor qualidade de raciocinio.',
      'Substituicao independente: trocar um Worker nao afeta Router nem Orchestrator.',
      'Paralelismo natural: Workers sem depends_on rodam simultaneamente.',
      'Catalogabilidade: Router Catalog vira documento navegavel de capacidades do dominio.',
    ],
    tradeoffs: [
      'Latencia de coordenacao: chamadas atravessam tres camadas em sequencia.',
      'Overhead de envelope: cada delegacao serializa contexto.',
      'Roteamento errado tem custo alto: Orchestrator delega ao Router errado e o caminho inteiro precisa retroceder.',
    ],
  },
  implementation: [
    'Estabelecer regra: Orchestrator nunca conversa diretamente com Worker. Router nunca decide intencao.',
    'Modelar Router Catalog com keywords + metricas (success_rate, latency_p95). Matching e' + ' hibrido: filtro por keywords + score.',
    'Implementar Orchestrator como agente especializado em geracao de Intent Descriptor (output JSON validado).',
    'Implementar Router como executor de DAG: parse Workflow do SKILL -> grafo dirigido -> ordem topologica com paralelismo.',
    'Implementar Worker como executor de tarefa atomica: carrega SKILL como system prompt, executa, retorna.',
    'Tornar a hierarquia visivel: dashboard mostrando Orchestrator -> Routers -> Workers com contadores e estado.',
  ],
  code: [
    '// Orchestrator',
    'function orchestrate(userText):',
    '  intent = generate_intent_descriptor(userText)   // LLM call',
    '  routers = router_catalog.match(intent)',
    '  selected = pick_best(routers, intent)',
    '  envelope = sign(envelope_for(selected, intent))',
    '  return router_runtime.dispatch(envelope)',
    '',
    '// Router',
    'function dispatch(envelope):',
    '  skill = skill_registry.load(envelope.skill_ref)',
    '  dag = parse_workflow_to_dag(skill.workflow)',
    '  results = execute_dag(dag, envelope.context)',
    '  return aggregate_per_output_contract(results, skill.output_contract)',
    '',
    '// Worker',
    'function execute(envelope):',
    '  skill = skill_registry.load(envelope.skill_ref)',
    '  result = run_with_system_prompt(skill, envelope.inputs)',
    '  return ContextDelta.from(result)',
  ],
  when: [
    'Voce tem mais de um processo de negocio no mesmo dominio.',
    'Voce quer que tarefas atomicas (ex: validar CPF) sejam reutilizaveis entre processos.',
    'Voce precisa paralelizar tarefas independentes dentro de um processo.',
  ],
  related: [
    'Mediator (GoF): Router e' + ' mediator entre Workers.',
    'Composite (GoF): DAG de Workers e' + ' uma composicao executavel.',
    'B.3 A2A Envelope Protocol: comunicacao entre camadas.',
    'B.5 Evidence-Verified FSM: cada Worker executa a FSM.',
  ],
});

// ----- B.3 -----
const subB3 = subPattern({
  id: 'B.3',
  name: 'A2A Envelope Protocol',
  also: 'Typed Inter-Agent Communication, Signed Delegation',
  intent: 'Padronizar a comunicacao entre agentes via um envelope tipado que carrega rastreabilidade, intencao, referencia ao SKILL, contexto, orcamento, prazo e assinatura. Eliminar passagem ad-hoc de strings entre agentes.',
  problem: [
    'Quando agentes trocam dados via JSON livre ou strings concatenadas, voce perde: (1) rastreabilidade ponta-a-ponta, (2) capacidade de auditar quem delegou para quem, (3) garantia de tipos no destinatario.',
    'Sem envelope, o que era para ser uma chamada estruturada vira um ' + 'telephone game' + ' onde cada agente reformata o que recebeu.',
  ],
  structure: {
    text: 'Toda comunicacao inter-agente atravessa um Envelope com campos obrigatorios. O Envelope e' + ' assinado, validado na fronteira e propagado sem mutacao (apenas estendido via Context Delta).',
    diagram: [
      'Envelope {',
      '  envelope_id      : UUID v4',
      '  trace_id         : OpenTelemetry trace',
      '  span_id          : OpenTelemetry span',
      '  parent_span_id   : encadeamento',
      '  origin_agent_id  : emissor',
      '  target_agent_id  : destinatario',
      '  intent           : IntentDescriptor (tipado)',
      '  skill_ref        : urn:skill:...@version',
      '  context          : map (append-only via ContextDelta)',
      '  budget_remaining : {tokens, wall_ms, usd}',
      '  deadline         : ISO timestamp absoluto',
      '  signature        : hash(id + target + skill + context_hash)',
      '}',
    ],
  },
  participants: [
    ['Envelope Emitter', 'Constroi o envelope, assina, propaga.'],
    ['Envelope Validator', 'Verifica assinatura, expiracao (deadline), orcamento (budget) na recepcao.'],
    ['IntentDescriptor', 'Estrutura tipada com domain, process_candidate, entities, constraints, urgency, actor.'],
    ['Budget', 'Triplete (tokens, wall_ms, usd) decrementado a cada chamada.'],
    ['Trace Plane', 'Coleta envelope_id + trace_id + span_id para reconstruir cadeia ponta-a-ponta.'],
  ],
  consequences: {
    benefits: [
      'Rastreabilidade ponta-a-ponta: trace_id permite reconstruir cadeia inteira.',
      'Tipos seguros na fronteira: validator falha cedo quando intent e' + ' malformada.',
      'Orcamento explicito: chamadas que excederiam budget sao rejeitadas antes de chamar LLM.',
      'Deadline propagado: timeout proximo ao usuario nao gera trabalho descartado.',
      'Assinatura detecta tampering em transito (entre processos / mensageria).',
    ],
    tradeoffs: [
      'Custo de serializacao a cada delegacao.',
      'Mudancas no schema do Envelope sao mudancas breaking; precisa de versionamento.',
      'Validacao adiciona latencia (ainda que pequena) por chamada.',
    ],
  },
  implementation: [
    'Modelar Envelope como tipo imutavel. Campos sao read-only apos assinatura.',
    'Implementar IntentDescriptor com schema explicito e validacao na fronteira.',
    'Definir algoritmo de assinatura simples (ex: SHA-256 truncado dos campos criticos). Nao e' + ' criptografia; e' + ' integridade.',
    'Decrementar budget a cada chamada de LLM/tool/API. Rejeitar envelope com budget zerado antes de iniciar trabalho.',
    'Comparar deadline absoluto a clock corrente ao receber. Se ultrapassado, abortar com motivo.',
    'Persistir envelopes em store append-only para auditoria e replay.',
  ],
  code: [
    'Envelope = {',
    '  envelope_id, trace_id, span_id, parent_span_id,',
    '  origin_agent_id, target_agent_id,',
    '  intent: IntentDescriptor,',
    '  skill_ref: string,',
    '  context: Map<string, any>,',
    '  budget_remaining: { tokens: int, wall_ms: int, usd: float },',
    '  deadline: ISO_timestamp,',
    '  signature: string',
    '}',
    '',
    'function emit(target, intent, skill_ref, parent_envelope):',
    '  e = {',
    '    envelope_id: uuid4(),',
    '    trace_id: parent_envelope.trace_id,',
    '    span_id: uuid4(),',
    '    parent_span_id: parent_envelope.span_id,',
    '    origin_agent_id: self.id,',
    '    target_agent_id: target.id,',
    '    intent: intent,',
    '    skill_ref: skill_ref,',
    '    context: parent_envelope.context,',
    '    budget_remaining: parent_envelope.budget_remaining,',
    '    deadline: parent_envelope.deadline,',
    '    signature: ""',
    '  }',
    '  e.signature = hash(e.envelope_id + e.target_agent_id + e.skill_ref + hash(e.context))',
    '  audit_log.append(e)',
    '  return e',
    '',
    'function validate(e):',
    '  if hash_recomputed(e) != e.signature: reject("tampered")',
    '  if now() > e.deadline: reject("expired")',
    '  if e.budget_remaining.tokens <= 0: reject("budget_exhausted")',
  ],
  when: [
    'Voce tem mais de dois agentes que precisam coordenar.',
    'Voce precisa rastrear ponta-a-ponta uma interacao multi-agente.',
    'Voce roda agentes em processos separados ou em rede (mensageria, RPC).',
  ],
  related: [
    'Command (GoF): Envelope e' + ' um comando reificado.',
    'Message (EIP): Envelope e' + ' uma mensagem auto-contida.',
    'B.4 Append-Only Context Delta: contexto dentro do Envelope so muda por Delta.',
    'B.2 Three-Tier Topology: envelopes sao o vetor de delegacao entre camadas.',
  ],
});

// ----- B.4 -----
const subB4 = subPattern({
  id: 'B.4',
  name: 'Append-Only Context Delta',
  also: 'Immutable Shared State, Context Sourcing',
  intent: 'Fazer com que o estado compartilhado entre agentes seja mutavel apenas via deltas append-only que registram quem mudou, o que mudou e quando. Nunca sobrescrever; sempre acrescentar.',
  problem: [
    'Quando agentes mutam um dicionario compartilhado in-place, voce perde a capacidade de reconstruir o estado em qualquer ponto da execucao. Auditoria fica fraca; debugging vira arqueologia.',
    'Pior: dois agentes paralelos podem sobrescrever a chave do outro sem deteccao. O ultimo escritor vence silenciosamente.',
  ],
  structure: {
    text: 'Toda mudanca de contexto e' + ' um Delta tipado. O contexto inteiro e' + ' a aplicacao sequencial de todos os deltas. Reconstruir o estado em qualquer ponto e' + ' replay dos deltas ate' + ' aquele ponto.',
    diagram: [
      'context_v0 = {}',
      '   |',
      '   v',
      'delta_1 (worker_A, t=10): { credito: 12450 }',
      '   |',
      '   v',
      'context_v1 = { credito: 12450 }',
      '   |',
      '   v',
      'delta_2 (worker_B, t=12): { debito: 18900 }',
      '   |',
      '   v',
      'context_v2 = { credito: 12450, debito: 18900 }',
      '   |',
      '   v',
      'delta_3 (worker_C, t=15): { evidencias: [+1] }   // append em lista',
      '   |',
      '   v',
      'context_v3 = { credito: 12450, debito: 18900, evidencias: [...] }',
    ],
  },
  participants: [
    ['Context Delta', 'Estrutura tipada {origin_agent, additions, timestamp, predecessor_hash}.'],
    ['Context Reducer', 'Funcao pura que aplica delta -> novo contexto. Append em listas; substituicao detectada e logada em escalares.'],
    ['Delta Log', 'Lista append-only de deltas; reconstruir estado em t e' + ' replay ate' + ' t.'],
    ['Conflict Detector', 'Deteca quando dois deltas concorrentes mudam a mesma chave escalar; emite evento de auditoria.'],
  ],
  consequences: {
    benefits: [
      'Auditoria completa: cada mudanca tem origem e timestamp.',
      'Time-travel debugging: reconstruir estado em qualquer ponto.',
      'Listas crescem sem perda quando agentes paralelos contribuem.',
      'Conflitos em escalares sao detectaveis (vs sobrescrita silenciosa).',
    ],
    tradeoffs: [
      'Maior consumo de memoria/storage que mutacao in-place.',
      'Reducer mais complexo que assignment direto.',
      'Conflitos em escalares precisam de politica explicita (ultimo vence? primeiro vence? merge custom?).',
    ],
  },
  implementation: [
    'Modelar Delta como dataclass imutavel: origin_agent_id, additions (map), timestamp, predecessor_hash.',
    'Implementar Reducer puro: para listas, append; para escalares, substituicao com log de evento ' + 'context_overwrite' + '.',
    'Manter Delta Log como atributo do Envelope (ou em store separada com referencia por trace_id).',
    'Para conflitos: emitir audit event obrigatorio. A politica (last-write-wins, first-write-wins, custom merge) e' + ' uma decisao do dominio; o que NAO pode acontecer e' + ' a sobrescrita ser silenciosa.',
    'Para reconstruir estado em t: filtrar deltas com timestamp <= t e dobrar o reducer.',
  ],
  code: [
    'Delta = { origin_agent_id, additions, timestamp, predecessor_hash }',
    '',
    'function apply_delta(context, delta):',
    '  merged = clone(context)',
    '  for key, value in delta.additions:',
    '    if is_list(merged[key]) and is_list(value):',
    '      merged[key] = merged[key] + value',
    '    else:',
    '      if key in merged and merged[key] != value:',
    '        audit.emit("context_overwrite", {',
    '          key: key, old: merged[key], new: value,',
    '          by: delta.origin_agent_id, at: delta.timestamp',
    '        })',
    '      merged[key] = value',
    '  merged._deltas.append(delta)',
    '  return merged',
    '',
    'function reconstruct_at(deltas, t):',
    '  filtered = [d for d in deltas if d.timestamp <= t]',
    '  return reduce(apply_delta, filtered, {})',
  ],
  when: [
    'Voce tem agentes paralelos que contribuem para o mesmo contexto.',
    'Voce precisa auditar exatamente quem alterou o que e quando.',
    'Voce quer time-travel debugging em producao.',
  ],
  related: [
    'Event Sourcing: cada delta e' + ' um evento.',
    'Memento (GoF): cada versao do contexto e' + ' um memento reconstrutivel.',
    'CRDT: variantes que resolvem conflito automaticamente em estruturas mais complexas.',
    'B.3 A2A Envelope Protocol: contexto vive dentro do envelope.',
  ],
});

// ----- B.5 -----
const subB5 = subPattern({
  id: 'B.5',
  name: 'Evidence-Verified State Machine',
  also: 'Mandatory Verification Gate, FSM with Evidence Check',
  intent: 'Estruturar a execucao de uma interacao como uma maquina de estados explicita na qual nenhum rascunho chega ao usuario sem passar por verificacao independente contra evidencias autorizadas.',
  problem: [
    'LLMs geram texto fluente mesmo quando nao tem evidencia. Sistemas que entregam saida diretamente do gerador para o usuario sao vulneraveis a alucinacao em dominios sensiveis.',
    'Verificacoes ad-hoc dentro do prompt do gerador sao auto-avaliacao: o LLM tende a aprovar suas proprias respostas.',
  ],
  structure: {
    text: 'FSM explicita com nove estados. Cinco estados de processamento, tres estados terminais funcionais e um estado terminal obrigatorio de log.',
    diagram: [
      '[*]',
      ' |',
      ' v',
      'Intake -> PolicyCheck -> RetrieveEvidence -> DraftAnswer -> VerifyEvidence',
      '                                                                  |',
      '              +---------------------------------------------------+',
      '              |                                                   |',
      '              v                                                   v',
      '         (verifica OK)                                   (verifica FAIL)',
      '              |                                                   |',
      '   +----------+----------+                                        |',
      '   v          v          v                                        v',
      'Recommend  Refuse    Escalate                                  Refuse',
      '   |          |          |                                        |',
      '   +----------+----------+                                        |',
      '              v                                                   |',
      '         LogAndClose <----------------------------------------+',
      '              |',
      '              v',
      '             [*]',
    ],
  },
  participants: [
    ['Intake', 'Recebe input, normaliza, persiste turno, cria contexto de interacao.'],
    ['PolicyCheck', 'Avalia permissoes via Policy Engine. Negativo -> Refuse direto.'],
    ['RetrieveEvidence', 'Retriever busca em bases autorizadas; Reranker reordena.'],
    ['DraftAnswer', 'Gerador (LLM ou Reflect-Reason Harness) produz rascunho.'],
    ['VerifyEvidence', 'Verificador independente avalia consistencia, cobertura, conflito, risco.'],
    ['Recommend', 'Verificacao OK -> entrega final com citacoes.'],
    ['Refuse', 'Evidencia insuficiente -> recusa estruturada com proximo passo.'],
    ['Escalate', 'Risco alto ou fraude -> delegacao a supervisor humano com contexto preservado.'],
    ['LogAndClose', 'Estado terminal obrigatorio. Sem este estado a interacao e' + ' considerada vazada.'],
  ],
  consequences: {
    benefits: [
      'Garantia estrutural contra alucinacao: verificacao e' + ' obrigatoria, nao opcional.',
      'Recusa controlada: sistema admite ' + 'nao sei' + ' em vez de inventar.',
      'Escalada explicita para risco alto: humanos entram no loop quando devem.',
      'Auditoria total: cada transicao e' + ' atomica e logada.',
      'LogAndClose obrigatorio impede vazamento de interacoes nao registradas.',
    ],
    tradeoffs: [
      'Latencia adicional do VerifyEvidence (uma chamada LLM extra).',
      'Verificador independente exige outro modelo (custo).',
      'Equipes resistem inicialmente a Refuse: preferem ' + 'qualquer resposta' + ' a recusa.',
      'Definir threshold de cobertura/risco e' + ' calibragem continua.',
    ],
  },
  implementation: [
    'Modelar States como enum. Definir transicoes validas em uma tabela; rejeitar transicoes invalidas.',
    'Cada transicao e' + ' atomica: persistir novo estado + emitir audit event ANTES de aplicar logica do novo estado.',
    'Verifier deve usar modelo distinto do gerador. Idealmente menor e mais estrito (ex: gerador usa modelo grande; verifier usa modelo menor com prompt focado).',
    'Verifier avalia em quatro dimensoes: consistencia, cobertura, conflito entre evidencias, risco. Saida e' + ' ' + '{ok, confidence, issues, risk_high, fraud_suspected}' + '.',
    'Threshold de cobertura calibravel por dominio (ex: financeiro 0.95, juridico 0.90, atendimento geral 0.70).',
    'Refuse retorna estrutura: { motivo, proximo_passo, evidencias_disponiveis }. Nunca apenas ' + '"nao sei"' + '.',
    'LogAndClose nao e' + ' opcional. Verificar em testes que nenhum caminho da FSM evita LogAndClose.',
  ],
  code: [
    'States = enum { Intake, PolicyCheck, RetrieveEvidence, DraftAnswer,',
    '                 VerifyEvidence, Recommend, Refuse, Escalate, LogAndClose }',
    '',
    'TRANSITIONS = {',
    '  Intake:           [PolicyCheck],',
    '  PolicyCheck:      [RetrieveEvidence, Refuse],',
    '  RetrieveEvidence: [DraftAnswer, Refuse],',
    '  DraftAnswer:      [VerifyEvidence],',
    '  VerifyEvidence:   [Recommend, Refuse, Escalate],',
    '  Recommend:        [LogAndClose],',
    '  Refuse:           [LogAndClose],',
    '  Escalate:         [LogAndClose],',
    '  LogAndClose:      []',
    '}',
    '',
    'function transition(ctx, from_state, to_state):',
    '  if to_state not in TRANSITIONS[from_state]:',
    '    raise InvalidTransition(from_state, to_state)',
    '  audit_log.append({ trace: ctx.trace_id, from: from_state, to: to_state, t: now() })',
    '  ctx.state = to_state',
    '  return ctx',
    '',
    'function verify_evidence(draft, evidences):',
    '  // VERIFIER usa MODELO DIFERENTE do gerador',
    '  result = verifier_llm.evaluate({',
    '    draft: draft,',
    '    evidences: evidences,',
    '    dimensions: ["consistency", "coverage", "conflict", "risk"]',
    '  })',
    '  if result.ok and not result.risk_high: return Recommend',
    '  if result.risk_high or result.fraud_suspected: return Escalate',
    '  return Refuse',
  ],
  when: [
    'Voce opera em dominio onde alucinacao tem custo alto (regulado, financeiro, juridico, saude).',
    'Voce precisa demonstrar a auditores como cada saida foi verificada.',
    'Voce aceita ' + 'nao sei' + ' como resposta valida quando evidencia falta.',
  ],
  related: [
    'State (GoF): a FSM e' + ' a aplicacao classica do padrao.',
    'Pipes & Filters: Retriever -> Reranker -> Verifier e' + ' pipeline.',
    'Circuit Breaker: Verifier degradado pode entrar em modo conservador (refuse default).',
    'B.7 Reflect-Reason Harness: opera dentro de DraftAnswer.',
  ],
});

// ----- B.6 -----
const subB6 = subPattern({
  id: 'B.6',
  name: 'Declarative API Binding',
  also: 'LLM-Free External Call, Bound Endpoint',
  intent: 'Permitir que agentes invoquem APIs externas sem chamada de LLM, atraves de bindings declarativos no SKILL que mapeiam inputs -> request HTTP -> response -> contexto, com resiliencia, idempotencia e auditabilidade.',
  problem: [
    'Muitas tarefas que parecem exigir LLM sao chamadas HTTP determinísticas com mapeamento previsivel: dado um cliente, buscar saldo; dado um pedido, criar nota fiscal. Usar LLM para gerar essas chamadas e' + ' caro, lento e introduz risco de alucinacao em parametros.',
    'Por outro lado, hardcoding de chamadas em codigo perde os beneficios do SKILL declarativo: nao versiona, nao audita, nao permite rollback.',
  ],
  structure: {
    text: 'O SKILL ganha uma secao ' + '## API Bindings' + ' que declara: connector_id (resolvido em registry separado), path/query templatizaveis (URL base nunca templatizavel), input mapping, output mapping (JSONPath), resilience (timeout, retry, idempotency), depends_on para DAG.',
    diagram: [
      '+--------------------------------------+',
      '|  SKILL.md                            |',
      '|                                      |',
      '|  ## API Bindings                     |',
      '|    binding: get_balance              |',
      '|      connector: bank_api             |',
      '|      method: GET                     |',
      '|      path: /accounts/{{ id }}/balance|',
      '|      output_mapping:                 |',
      '|        balance: $.amount             |',
      '|      resilience:                     |',
      '|        timeout_ms: 2000              |',
      '|        retry: [5xx]                  |',
      '+-------------+------------------------+',
      '              |',
      '              v',
      '+--------------------------------------+',
      '|  Connector Registry                  |',
      '|  bank_api -> https://...             |',
      '|  + secrets isolados                  |',
      '+--------------------------------------+',
      '              |',
      '              v',
      '       chamada HTTP direta',
      '       (sem LLM no caminho)',
    ],
  },
  participants: [
    ['Binding Definition', 'Trecho declarativo no SKILL: id, method, path, query, headers, body, output_mapping, resilience.'],
    ['Connector Registry', 'Tabela separada com connector_id -> base_url + auth + secrets. SKILL nunca contem URL crua.'],
    ['Declarative Engine', 'Resolve bindings em ordem topologica (depends_on); aplica templating; chama HTTP; mapeia response; atualiza contexto.'],
    ['Template Engine', 'Sandboxed: sem acesso a sistema de arquivos, sem eval, sem importacao.'],
    ['JSONPath Extractor', 'Extrai campos de response JSON conforme output_mapping.'],
    ['Circuit Breaker', 'Por binding; abre apos N falhas; meio-aberto para teste; fecha apos sucesso.'],
    ['Evidence Emitter', 'Cada chamada bem ou mal sucedida emite evidence ' + 'api_call' + ' tipada com hashes de request/response.'],
  ],
  consequences: {
    benefits: [
      'Latencia reduzida: chamadas determinísticas executam sem LLM no caminho.',
      'Custo reduzido: zero tokens em chamadas que nao precisam de raciocinio.',
      'Auditabilidade: hash de request/response em evidence tipada.',
      'Seguranca: secrets nunca passam pelo prompt; URL base estatica elimina SSRF.',
      'Rollback: mudar binding e' + ' editar SKILL e bumpar versao; LLM nao precisa retreinar nada.',
    ],
    tradeoffs: [
      'Apenas tarefas determinísticas se beneficiam; raciocinio livre ainda exige LLM.',
      'Connector Registry e' + ' nova superficie de gerenciamento (rotacao de secrets, expiracao).',
      'Linter precisa validar idempotencia e SSRF em CI/CD.',
      'Mistura LLM + declarativo no mesmo SKILL pode confundir; muitas equipes preferem segregar.',
    ],
  },
  implementation: [
    'Adicionar `execution_mode: declarative | reasoning | hybrid` ao frontmatter do SKILL. Modo declarative bypassa LLM completamente.',
    'Criar Connector Registry como tabela separada (nao YAML inline). SKILL referencia connector_id; resolucao acontece no runtime.',
    'Implementar engine de templates sandboxed. Proibir acesso a sistema de arquivos, eval, modulos externos.',
    'Implementar engine declarativa: parse bindings -> grafo por depends_on -> ordem topologica -> paralelismo onde nao ha' + ' dependencia.',
    'Politica de resiliencia padrao: timeout obrigatorio, retry apenas em 5xx e timeout, idempotency_key obrigatoria em POST/PATCH/DELETE.',
    'URL base e' + ' SEMPRE estatica (vem do connector). Apenas path e query sao templatizaveis. Linter rejeita URL inteira em template.',
    'Circuit breaker por binding. Depois de N falhas em janela, abrir; testar com half-open; fechar com sucesso.',
    'Cada chamada emite evidence ' + 'api_call' + ' com input_hash + output_hash. Audit log nunca contem secrets.',
  ],
  code: [
    '## API Bindings',
    '',
    '### binding: extract_invoice',
    '  connector: erp_invoices',
    '  method: GET',
    '  path: /invoices',
    '  query:',
    '    period: "{{ inputs.competencia }}"',
    '    branch: "{{ inputs.filial }}"',
    '  output_mapping:',
    '    invoices: "$.data[*]"',
    '    total: "$.summary.total"',
    '  resilience:',
    '    timeout_ms: 3000',
    '    retry:',
    '      max_attempts: 3',
    '      on_status: [502, 503, 504, "timeout"]',
    '',
    '### binding: create_tax_guide',
    '  connector: tax_authority',
    '  method: POST',
    '  path: /guides',
    '  body:',
    '    period: "{{ inputs.competencia }}"',
    '    amount: "{{ context.balanco.saldo }}"',
    '  resilience:',
    '    timeout_ms: 5000',
    '    idempotency_key: "guide:{{ inputs.competencia }}:{{ inputs.filial }}"',
    '  depends_on: [extract_invoice]',
    '  output_mapping:',
    '    guide_id: "$.id"',
    '    barcode: "$.barcode"',
  ],
  when: [
    'O agente faz muitas chamadas HTTP determinísticas com parametros derivaveis.',
    'Latencia importa e voce quer eliminar LLM do caminho critico.',
    'Auditoria de chamadas externas e' + ' requisito (financeiro, juridico).',
  ],
  related: [
    'Adapter (GoF): connector adapta API externa ao formato esperado pelo agente.',
    'Facade (GoF): binding expoe interface simplificada da API.',
    'Circuit Breaker: por binding.',
    'B.1 SKILL-as-Contract: bindings vivem dentro do SKILL.',
    'B.7 Reflect-Reason Harness: nao se aplica a chamadas declarativas; apenas a reasoning.',
  ],
});

// ----- B.7 -----
const subB7 = subPattern({
  id: 'B.7',
  name: 'Reflect-Reason Harness',
  also: 'Bounded Self-Correction Loop, Reasoning with Reflection',
  intent: 'Estruturar o raciocinio de um agente como um loop limitado de duas fases: gerar (reason) e auto-avaliar contra Output Contract e Guardrails (reflect). Refinar quando insatisfatorio; encerrar quando satisfatorio ou quando orcamento esgota.',
  problem: [
    'Single-shot LLM frequentemente produz saidas que violam o Output Contract: campo faltante, formato errado, alucinacao parcial. Lancar isso ao usuario degrada confianca.',
    'Loops de raciocinio sem limite (ReAct sem max_iterations) podem nunca convergir e consumir orcamento ate' + ' explodir custo.',
  ],
  structure: {
    text: 'Grafo de dois nos com edge condicional: reason -> {reflect, end}; reflect -> reason. Limite duro de iteracoes; orcamento de tokens decrementado a cada passada.',
    diagram: [
      '   +------------+',
      '   |   reason   | <----------------+',
      '   +------+-----+                  |',
      '          |                        |',
      '          v                        |',
      '   +------------+                  |',
      '   |  decision  |                  |',
      '   +-+--------+-+                  |',
      '     | ok     | refine             |',
      '     v        v                    |',
      '   [END]   +----------+            |',
      '           | reflect  | -----------+',
      '           +----------+',
      '',
      '  Limite: max_iterations (ex: 3)',
      '  Orcamento: budget.tokens decrementa a cada reason+reflect',
    ],
  },
  participants: [
    ['Reason Node', 'Gera resposta candidata via LLM, dado o estado atual (mensagens + contexto + reflexoes anteriores).'],
    ['Reflect Node', 'Avalia resposta contra Output Contract e Guardrails. Saida e' + ' ' + '{satisfatoria: bool, criticas: [string]}' + '.'],
    ['Decision Edge', 'Roteamento condicional: satisfatoria -> end; insatisfatoria -> reflect.'],
    ['Iteration Counter', 'Decrementa a cada ciclo. Zerou -> aceita melhor candidato disponivel mesmo se nao satisfatorio.'],
    ['Budget Tracker', 'Decrementa tokens consumidos. Esgotou -> aborta com motivo ' + 'budget_exhausted' + '.'],
    ['Best Candidate', 'Memoria do melhor candidato visto ate' + ' agora; usado se loop encerra sem satisfacao.'],
  ],
  consequences: {
    benefits: [
      'Saida significativamente mais aderente ao Output Contract.',
      'Convergencia limitada: nunca loop infinito.',
      'Custo previsivel: max_iterations + budget impedem explosao.',
      'Reflexoes intermediarias sao auditaveis (ficam no contexto).',
    ],
    tradeoffs: [
      'Latencia aumenta proporcionalmente a numero de iteracoes (ate' + ' max_iterations).',
      'Custo aumenta linearmente com iteracoes.',
      'Reflexoes podem ser falsamente satisfatorias (LLM aprova proprio rascunho); por isso nao substitui VerifyEvidence (B.5) que usa modelo independente.',
    ],
  },
  implementation: [
    'Modelar AgentState como tipo: messages (append-only), iteration, max_iterations, context, output_draft, best_so_far.',
    'Implementar reason como chamada LLM com system prompt do SKILL + historico de reflexoes anteriores como contexto.',
    'Implementar reflect como prompt focado: avalia rascunho contra Output Contract e Guardrails, retorna estrutura json.',
    'Edge condicional: se reflect.satisfatoria -> end; senao se iteration < max_iterations -> reflect (que prepara contexto para proximo reason); senao -> end com best_so_far.',
    'Decrementar budget.tokens com tokens consumidos a cada chamada. Budget zero -> abortar com motivo.',
    'Default conservador: max_iterations = 3. Aumentar so com evidencia que justifique.',
    'Reflect-Reason NAO substitui VerifyEvidence: o primeiro e' + ' auto-avaliacao; o segundo e' + ' verificacao independente. Voce precisa dos dois em dominios sensiveis.',
  ],
  code: [
    'AgentState = {',
    '  messages, iteration, max_iterations,',
    '  context, output_draft, best_so_far, budget',
    '}',
    '',
    'function reason(state):',
    '  state.output_draft = llm.generate(',
    '    system: state.skill.system_prompt,',
    '    history: state.messages,',
    '    context: state.context',
    '  )',
    '  state.budget.tokens -= last_call.tokens_used',
    '  return state',
    '',
    'function reflect(state):',
    '  evaluation = llm.evaluate(',
    '    draft: state.output_draft,',
    '    contract: state.skill.output_contract,',
    '    guardrails: state.skill.guardrails',
    '  )',
    '  state.budget.tokens -= last_call.tokens_used',
    '  if score(state.output_draft) > score(state.best_so_far):',
    '    state.best_so_far = state.output_draft',
    '  state.last_evaluation = evaluation',
    '  return state',
    '',
    'function decide(state):',
    '  if state.last_evaluation.satisfatoria: return "end"',
    '  if state.iteration >= state.max_iterations: return "end"',
    '  if state.budget.tokens <= 0: return "end_budget"',
    '  state.iteration += 1',
    '  return "reflect"',
    '',
    '// Output final: state.best_so_far (em caso de end_budget ou max_iter)',
    '//                 ou state.output_draft (se end por satisfacao)',
  ],
  when: [
    'O Output Contract do agente e' + ' rico (multiplos campos, formatos especificos).',
    'Voce ja' + ' tentou single-shot e a taxa de aderencia ao contrato e' + ' baixa.',
    'Voce esta disposto a trocar latencia/custo por qualidade de saida.',
  ],
  related: [
    'ReAct: precursor; Reflect-Reason adiciona limite explicito e best-so-far.',
    'Self-Refine: tecnica equivalente em literatura academica.',
    'Template Method (GoF): reason/reflect e' + ' o template; o motor preenche.',
    'B.5 Evidence-Verified FSM: complementar; reflect e' + ' interno, VerifyEvidence e' + ' externo e independente.',
  ],
});

// ====================================================================
// APENDICE
// ====================================================================

const apendiceHeader = [
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 600, after: 200 },
    children: [new TextRun({ text: 'APENDICE', font: FONT, size: 22, bold: true, color: '888888' })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 600 },
    children: [new TextRun({ text: 'Material de Referencia', font: FONT, size: 32, bold: true, color: '1F3864' })],
  }),
  divider(),
];

const apendiceA = [
  h1('Apendice A - Matriz de combinacao'),
  p('A tabela abaixo indica relacoes de dependencia (D) e recomendacao (R) entre os sub-patterns. Linha = sub-pattern adotado; coluna = sub-pattern relacionado.'),
  tableSimple(
    ['Adotando \\ Relaciona', 'B.1', 'B.2', 'B.3', 'B.4', 'B.5', 'B.6', 'B.7'],
    [
      ['B.1 SKILL-as-Contract',          '-',  'R',  'R',  'R',  'R',  'R',  'R'],
      ['B.2 Three-Tier Topology',        'D',  '-',  'D',  'R',  'R',  '-',  '-'],
      ['B.3 A2A Envelope',               '-',  'D',  '-',  'R',  '-',  '-',  '-'],
      ['B.4 Append-Only Context Delta',  '-',  '-',  'D',  '-',  '-',  '-',  '-'],
      ['B.5 Evidence-Verified FSM',      'R',  '-',  '-',  'R',  '-',  '-',  'R'],
      ['B.6 Declarative API Binding',    'D',  '-',  '-',  '-',  '-',  '-',  '-'],
      ['B.7 Reflect-Reason Harness',     'R',  '-',  '-',  '-',  'R',  '-',  '-'],
    ],
    [3000, 909, 909, 909, 909, 909, 909, 906]
  ),
  p([rb('Legenda: '), r('D = dependencia (precisa do outro para funcionar); R = recomendacao (funciona melhor com); - = sem relacao significativa.')]),
  callout('Observacao:', 'B.1 (SKILL-as-Contract) e' + ' a base recomendada de qualquer combinacao. SSAM completo combina os sete; um subset minimo viavel para ' + 'Evidence-First Agents' + ' e' + ' B.1 + B.5 + B.7.'),
];

const apendiceB = [
  h1('Apendice B - Glossario'),
  tableSimple(
    ['Termo', 'Definicao'],
    [
      ['A2A', 'Agent-to-Agent. Protocolo de comunicacao entre agentes via Envelope tipado.'],
      ['Activation Criteria', 'Secao do SKILL que declara quando o agente se ativa.'],
      ['Audit Log', 'Ledger append-only de toda transicao significativa.'],
      ['Budget', 'Triplete (tokens, wall_ms, usd) propagado no Envelope; decrementado a cada chamada.'],
      ['Connector Registry', 'Catalogo de endpoints HTTP externos com secrets isolados; SKILL referencia connector_id.'],
      ['Context Delta', 'Mutacao append-only do contexto compartilhado entre agentes.'],
      ['DAG', 'Directed Acyclic Graph; estrutura usada para decompor um Workflow em tarefas com depends_on.'],
      ['Drift Detection', 'Mecanismo que compara metricas correntes contra baseline da release ativa.'],
      ['Envelope', 'Estrutura tipada de comunicacao inter-agente.'],
      ['Evidence', 'Trecho de fonte autorizada citado em uma recomendacao; tipado e versionado.'],
      ['Failure Mode', 'Modo de falha declarado no SKILL com transicao para Refuse ou Escalate.'],
      ['FSM', 'Finite State Machine; aqui, a maquina de nove estados que orquestra cada interacao.'],
      ['Gold Cases', 'Dataset gold adversarial usado pelo Harness Evaluator para gate de release.'],
      ['Idempotency Key', 'Chave que garante que retry de POST/PATCH/DELETE nao duplica efeito.'],
      ['Intent Descriptor', 'Estrutura tipada produzida pelo Orchestrator a partir de texto natural.'],
      ['Knowledge Source', 'Base autorizada de evidencias; pode ser publica, interna, confidencial ou restrita.'],
      ['LogAndClose', 'Estado terminal obrigatorio da FSM; sem ele, interacao e' + ' considerada vazada.'],
      ['Orchestrator', 'Camada 1; interpreta NL, gera Intent Descriptor, roteia.'],
      ['Output Contract', 'Tipo de retorno declarado no SKILL; agentes devem aderir.'],
      ['Policy Engine', 'Componente que avalia permissoes (acesso, sensibilidade, autoridade) antes de retrieve/draft.'],
      ['Reflect-Reason Harness', 'Loop limitado de raciocinio com auto-avaliacao.'],
      ['Release', 'Composicao versionada de (model + prompt + index + policy); promovida via gate.'],
      ['Retriever', 'Componente que busca em fontes autorizadas.'],
      ['Reranker', 'Componente que reordena resultados do Retriever.'],
      ['Router', 'Camada 2; representa um processo de negocio.'],
      ['SKILL', 'Artefato declarativo (Markdown + YAML frontmatter) que define identidade e capacidades de um agente.'],
      ['SKILL Linter', 'Validador semantico de SKILLs (idempotencia, SSRF, ciclos, secret_leak).'],
      ['SSRF', 'Server-Side Request Forgery; vulnerabilidade quando URL inteira e' + ' templatizavel.'],
      ['SSAM', 'SKILL-Sovereign Agent Mesh; o macro-pattern da Parte A.'],
      ['Tool Binding', 'Secao do SKILL que declara quais tools o agente pode invocar.'],
      ['Tool Registry', 'Inventario de capacidades (tools) com sensibilidade, custo, requisitos.'],
      ['Verifier', 'Componente independente do gerador que avalia rascunho contra evidencias.'],
      ['Workflow', 'Secao do SKILL que descreve a sequencia/grafo de execucao da tarefa.'],
      ['Worker', 'Camada 3; unidade atomica de execucao.'],
    ],
    [2400, 6960]
  ),
];

const apendiceC = [
  h1('Apendice C - Anti-patterns'),
  p('Praticas recorrentes que parecem solucoes razoaveis e que destroem propriedades do SSAM. Evite todas.'),

  h2('AP.1 SKILL-as-Documentation'),
  p([rb('Sintoma: '), r('SKILL existe mas e' + ' lido apenas por humanos; o codigo do agente nao depende dele.')]),
  p([rb('Por que e' + ' ruim: '), r('voce voltou ao mundo prompt+codigo entrelacados. Versionar SKILL nao versiona comportamento.')]),
  p([rb('Correcao: '), r('o runtime DEVE ler SKILL e usar Workflow, Tool Bindings e Output Contract como fonte unica de verdade. Se SKILL nao for parsable e executavel, ele nao existe arquiteturalmente.')]),

  h2('AP.2 Mutate-Context-In-Place'),
  p([rb('Sintoma: '), r('agentes editam o dicionario de contexto compartilhado por assignment direto.')]),
  p([rb('Por que e' + ' ruim: '), r('destroi auditoria; conflitos entre agentes paralelos sao silenciosos.')]),
  p([rb('Correcao: '), r('use Context Delta append-only (B.4). Reducer detecta sobrescrita de escalares e emite audit event.')]),

  h2('AP.3 Same-LLM Verification'),
  p([rb('Sintoma: '), r('o LLM que gera o rascunho tambem e' + ' o que verifica.')]),
  p([rb('Por que e' + ' ruim: '), r('self-grading bias. O modelo aprova suas proprias respostas com confianca alta.')]),
  p([rb('Correcao: '), r('Verifier (B.5) DEVE usar modelo distinto do gerador. Idealmente menor e mais estrito, com prompt focado em refutar em vez de confirmar.')]),

  h2('AP.4 Templated Base URL'),
  p([rb('Sintoma: '), r('binding declarativo permite URL inteira em template.')]),
  p([rb('Por que e' + ' ruim: '), r('SSRF. Atacante injeta URL maliciosa via input do usuario; agente faz request a host arbitrario com secrets do connector.')]),
  p([rb('Correcao: '), r('URL base SEMPRE estatica (vem do Connector Registry). Apenas path e query sao templatizaveis. Linter rejeita SKILLs que violem.')]),

  h2('AP.5 Skip-VerifyEvidence'),
  p([rb('Sintoma: '), r('rascunho do gerador vai direto para Recommend.')]),
  p([rb('Por que e' + ' ruim: '), r('elimina o principal mecanismo estrutural contra alucinacao. Sistema vira chatbot.')]),
  p([rb('Correcao: '), r('VerifyEvidence e' + ' obrigatorio na FSM. Em testes, falhar build se houver caminho que evite VerifyEvidence.')]),

  h2('AP.6 Unbounded Reflect'),
  p([rb('Sintoma: '), r('Reflect-Reason loop sem max_iterations.')]),
  p([rb('Por que e' + ' ruim: '), r('em casos onde o LLM nao converge, o loop consome orcamento ate' + ' a interrupcao por timeout duro do servidor. Custo explode.')]),
  p([rb('Correcao: '), r('max_iterations duro (ex: 3); budget.tokens explicito; best_so_far como saida quando loop encerra sem satisfacao.')]),

  h2('AP.7 Tools-Outside-Registry'),
  p([rb('Sintoma: '), r('agente invoca biblioteca/funcao diretamente, sem passar pelo Tool Registry.')]),
  p([rb('Por que e' + ' ruim: '), r('sem auditoria, sem politica de sensibilidade, sem custo declarado. Fan-out descontrolado.')]),
  p([rb('Correcao: '), r('todo acesso a capacidade externa (LLM, HTTP, arquivo, banco) atravessa Tool Registry ou Connector Registry. Linter rejeita SKILLs que mencionem capacidade nao registrada.')]),

  h2('AP.8 Implicit-Refuse'),
  p([rb('Sintoma: '), r('quando evidencia falta, agente retorna ' + '"nao sei"' + ' em texto livre.')]),
  p([rb('Por que e' + ' ruim: '), r('cliente downstream nao consegue diferenciar Refuse legitimo de erro tecnico.')]),
  p([rb('Correcao: '), r('Refuse e' + ' estado terminal estruturado: { motivo, proximo_passo, evidencias_disponiveis }. Sempre tipado, nunca em prosa.')]),

  h2('AP.9 Hidden-LLM-Provider'),
  p([rb('Sintoma: '), r('codigo do agente importa cliente de provedor especifico (OpenAI, Anthropic, Google) diretamente.')]),
  p([rb('Por que e' + ' ruim: '), r('migrar de provedor vira refactor invasivo; A/B testing entre provedores e' + ' impossivel sem branchear codigo.')]),
  p([rb('Correcao: '), r('LLM Providers atras de interface tipada com factory por release config. SKILL declara constraints (model_constraints), nao instancia provedor.')]),

  h2('AP.10 No-Idempotency-On-Writes'),
  p([rb('Sintoma: '), r('binding declarativo POST sem idempotency_key.')]),
  p([rb('Por que e' + ' ruim: '), r('retry duplica efeito. Em dominios financeiros, isso e' + ' catastrofico (cobrancas duplicadas, transferencias duplas).')]),
  p([rb('Correcao: '), r('idempotency_key obrigatoria em POST/PATCH/DELETE. Linter falha CI/CD se ausente. Construa a key a partir de inputs determinísticos do binding.')]),
];

// ====================================================================
// MONTAGEM FINAL DO DOCUMENTO
// ====================================================================

const allChildren = [
  ...cover,
  ...introducao,
  ...parteA,
  ...parteBHeader,
  ...subB1,
  ...subB2,
  ...subB3,
  ...subB4,
  ...subB5,
  ...subB6,
  ...subB7,
  ...apendiceHeader,
  ...apendiceA,
  ...apendiceB,
  ...apendiceC,
];

const doc = new Document({
  creator: 'Sergio Gaiotto',
  title: 'Design Pattern: SKILL-Sovereign Agent Mesh',
  description: 'Padrao arquitetural agnostico para sistemas multi-agentes inteligentes',
  styles: {
    default: {
      document: { run: { font: FONT, size: 22 } },
    },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 36, bold: true, font: FONT, color: '1F3864' },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 28, bold: true, font: FONT, color: '2E5395' },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 24, bold: true, font: FONT, color: '2E5395' },
        paragraph: { spacing: { before: 220, after: 120 }, outlineLevel: 2 } },
      { id: 'Heading4', name: 'Heading 4', basedOn: 'Normal', next: 'Normal', quickFormat: true,
        run: { size: 22, bold: true, font: FONT, color: '404040' },
        paragraph: { spacing: { before: 180, after: 100 }, outlineLevel: 3 } },
    ],
  },
  numbering: {
    config: [
      { reference: 'bullets',
        levels: [
          { level: 0, format: LevelFormat.BULLET, text: '*', alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
          { level: 1, format: LevelFormat.BULLET, text: '-', alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 1440, hanging: 360 } } } },
        ] },
      { reference: 'numbers',
        levels: [
          { level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
        ] },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({ text: 'SKILL-Sovereign Agent Mesh - v1.0', font: FONT, size: 16, color: '888888' })],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
          children: [
            new TextRun({ text: 'Sergio Gaiotto', font: FONT, size: 16, color: '888888' }),
            new TextRun({ text: '\t', font: FONT }),
            new TextRun({ children: ['Pagina ', PageNumber.CURRENT, ' de ', PageNumber.TOTAL_PAGES], font: FONT, size: 16, color: '888888' }),
          ],
        })],
      }),
    },
    children: allChildren,
  }],
});

const JSZip = require('C:/nvm4w/nodejs/node_modules/docx/node_modules/jszip');

const outPath = path.join(__dirname, '..', 'docs', 'design_pattern_SKILL_Sovereign_Agent_Mesh.docx');

(async () => {
  const buffer = await Packer.toBuffer(doc);

  // Post-process: docx-js gera abstractNums template com bullets unicode (●, ○, etc.)
  // que quebram validators no Windows (cp1252). Trocar por ASCII.
  const zip = await JSZip.loadAsync(buffer);
  const numberingXml = await zip.file('word/numbering.xml').async('string');
  const replacements = {
    '●': '*',  // bullet preto
    '○': 'o',  // bullet branco
    '■': '#',  // quadrado preto
    '□': '+',  // quadrado branco
    '▪': '-',  // quadrado pequeno preto
    '▫': '-',  // quadrado pequeno branco
    '•': '*',  // bullet
    '◦': 'o',  // bullet white circle
  };
  let patched = numberingXml;
  for (const [k, v] of Object.entries(replacements)) {
    patched = patched.split(k).join(v);
  }
  zip.file('word/numbering.xml', patched);
  const finalBuffer = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
  fs.writeFileSync(outPath, finalBuffer);
  console.log('Wrote:', outPath);
  console.log('Size :', finalBuffer.length, 'bytes');
})();
