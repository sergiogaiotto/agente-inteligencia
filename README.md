# 🎼 Maestro

**Plataforma de Gestão e Desenvolvimento de Multi-Agentes de IA, orientada a SKILL.md, sobre AI Mesh**

> Documenta a plataforma na versão **54.0.0** · Especificação Funcional §1–§24 · pt-BR

> **Novidades 52.x–54.x** (destaques):
> - **"Conhecer o agente" no Fluxo (54.0.0)** — o botão direito no nó abre um assistente que **explica** o agente (o que faz, propósito, config, posição no mesh, comportamento) ancorado na definição real — e **não o executa** (sem interação, sem gasto, sem histórico). Testar de verdade fica no Playground/Executar ([4.8](#48-fluxo-de-agentes-meshflow--o-estúdio-de-pipelines)).
> - **Recusa/escalonamento viram estado da FSM (opt-in)** — uma recusa redigida pelo agente (dado de terceiro, injeção de prompt) ou um escalonamento transicionam para `Refuse`/`Escalate` via a flag `verifier_signals_drive_fsm`, em **qualquer** caminho de verificação (Parte I, §8).
> - **Fundamentação por RAG mais honesta** — declarar `## Evidence Policy` só fundamenta com **"Exigir evidência" ligado** e `min_relevance` baixo (~0,0); o diagnóstico aponta a **causa real** quando o RAG é pulado (Parte I, §7).
> - **Métrica de alucinação do Harness honesta** — medida só sobre os casos com factualidade avaliada (`N/A` quando não medida), sem punir pipeline corretamente fundamentado ([4.13](#413-harness-de-avaliação-harness)).

> **Novidades 40.x–42.x** (destaques detalhados nas seções indicadas):
> - **Cobertura per-tool + depreciação visível** — métrica-gate de prontidão da frota MCP, chip "legado" e dry-run per-tool completo ([4.6](#46-mcp--tool-registry-mcp)).
> - **Fluxo de agentes muito mais vivo** — Menu de Regência (botão direito), Dossiê do Agente com Skill expandível, **"Converse com seu agente"** (chat real com FSM), **simulador de roteamento no canvas**, isolar vizinhança, e painel do pipeline com selo/domínio/ajuda em "?" ([4.8](#48-fluxo-de-agentes-meshflow--o-estúdio-de-pipelines)).
> - **Golden Dataset editável** — editar e excluir casos pela UI, com integridade histórica preservada ([4.13](#413-harness-de-avaliação-harness)).
> - **"Quem é o usuário?"** — dono/ator com nome nas telas de Observabilidade, Histórico, Auditoria e Qualidade, e filtro por usuário ([4.15](#415-observabilidade-observability-infra-infra-e-histórico-history)).
> - **Codegen ensina a anexar** — o código gerado no Playground mostra o bloco `attachments` em base64 ([Parte V](#anexos-dois-transportes)).

---

## Para quem nunca viu isso: o que é o Maestro?

Imagine uma **orquestra**. Cada músico é excelente em um instrumento, mas nenhum concerto acontece sem três coisas: uma **partitura** que diz o que tocar, um **maestro** que decide quem toca quando, e um **ensaio** que prova que a apresentação está pronta antes da estreia.

O Maestro aplica exatamente essa lógica a agentes de Inteligência Artificial:

- Cada **agente** é um músico: um especialista em uma tarefa (responder sobre crédito, analisar um documento, consultar um sistema externo).
- A **Skill** (um arquivo chamado `SKILL.md`) é a partitura: descreve, em texto estruturado, o que o agente sabe fazer, com quais ferramentas, com quais entradas e saídas — e o agente **não pode tocar nada fora da partitura**.
- O **Fluxo de agentes** é a disposição da orquestra: quem passa a vez para quem, em paralelo ou por decisão ("se o cliente mencionar fraude, chame o especialista jurídico").
- O **Pipeline** é o concerto ensaiado e fechado: um conjunto de agentes com entrada única, pronto para ser executado por pessoas e sistemas.
- O **Harness de Avaliação** é o ensaio geral: uma bateria de casos reais que mede, com números, se o conjunto continua tocando bem depois de cada mudança.

O que torna o Maestro diferente de "um chatbot com prompt" é o princípio que atravessa tudo: **declarar → provar → selar → vigiar**. Tudo que você declara (uma regra, um contrato de parâmetros, uma frase de teste) é provado por máquina antes de valer, selado quando publicado, e vigiado para sempre depois disso.

---

## Sumário

- [Parte I — Fundamentos e conceitos](#parte-i--fundamentos-e-conceitos)
- [Parte II — Como o Maestro pensa (arquitetura)](#parte-ii--como-o-maestro-pensa-arquitetura)
- [Parte III — Instalação e primeiros 15 minutos](#parte-iii--instalação-e-primeiros-15-minutos)
- [Parte IV — Tour completo, módulo a módulo](#parte-iv--tour-completo-módulo-a-módulo)
- [Parte V — A API para desenvolvedores](#parte-v--a-api-para-desenvolvedores)
- [Parte VI — Qualidade contínua em profundidade](#parte-vi--qualidade-contínua-em-profundidade)
- [Parte VII — Segurança, governança e LGPD](#parte-vii--segurança-governança-e-lgpd)
- [Parte VIII — Casos de uso completos](#parte-viii--casos-de-uso-completos)
- [Parte IX — Para quem desenvolve a plataforma](#parte-ix--para-quem-desenvolve-a-plataforma)
- [FAQ](#faq--perguntas-que-todo-iniciante-faz)
- [Licença e contato](#licença-e-contato)

---

# Parte I — Fundamentos e conceitos

Esta seção explica os conceitos na ordem em que você vai encontrá-los. Cada um tem três camadas: **a analogia** (para entender), **o fundamento técnico** (para confiar) e **um exemplo** (para reconhecer na tela).

### 1. Agente

**Analogia:** um funcionário especializado que só faz o que está no seu contrato de trabalho.

**Fundamento:** um agente é um processo computacional que une um modelo de linguagem (LLM) a uma Skill. Existem três camadas de agentes, e a camada define o papel no fluxo:

| Camada | Nome na UI | Papel | Analogia |
|---|---|---|---|
| `aobd` | **Maestro** | Orquestra o domínio: interpreta a intenção do usuário e delega | O maestro da orquestra |
| `router` | **Triagem / Roteador** | Decide o caminho: qual especialista atende este caso? | O recepcionista que encaminha |
| `subagent` | **Especialista** | Executa a tarefa fim: responde, consulta, calcula | O músico solista |

**Exemplo:** num fluxo de atendimento de telecom, o Maestro recebe "minha internet caiu", o Roteador de triagem técnica identifica que é um incidente, e o Especialista NOC abre o chamado com SLA.

**Detalhes que importam:** cada agente declara se **aceita documentos** e se **aceita imagens** (isso controla quais anexos chegam até ele), o idioma de resposta, a "temperatura" (criatividade) e o esforço de raciocínio. Em vez de fixar um modelo de IA, o recomendado é declarar o **tipo de tarefa** (raciocínio, classificação, chamadas de ferramenta…) e deixar o **Roteamento LLM** escolher o modelo — trocar de provedor vira uma configuração, não uma reedição de agentes.

### 2. SKILL.md — a partitura executável

**Analogia:** a receita de bolo que o cozinheiro é obrigado a seguir — não é uma sugestão, é o contrato.

**Fundamento:** o `SKILL.md` é um arquivo Markdown com um cabeçalho (identidade, versão, estabilidade) e seções padronizadas. Ele **não é documentação**: é parseado, validado e vira o comportamento efetivo do agente. Nenhum comportamento existe fora do que está declarado ali. As seções principais:

| Seção | O que declara | Vira o quê |
|---|---|---|
| `## Purpose` | O propósito em uma frase | Direção do prompt |
| `## Activation Criteria` | Quando esta skill deve ser acionada | Sinal de roteamento |
| `## Inputs` | JSON Schema dos parâmetros estruturados | **Contrato de `args`** da API, formulário do Playground, validação |
| `## Workflow` | O passo a passo da execução | Plano que o agente segue |
| `## Tool Bindings` | Ferramentas externas (MCP) autorizadas | Funções que o LLM pode chamar |
| `## Output Contract` | O formato da resposta | Validação da saída |
| `## Decisions` | Campos e valores que o agente anuncia ao decidir | **Contrato de Decisão** lido pelas regras do fluxo |
| `## Guardrails` / `## Failure Modes` | Limites e planos B | Restrições e recusas controladas |
| `## Evidence Policy` | Fontes autorizadas | Regra de fundamentação |
| `## Data Tables` | Planilhas/CSVs anexados como tabela | Consultas SQL parametrizadas (DuckDB) |
| `## API Bindings` | Chamadas HTTP declarativas | Execução **sem LLM** (modo declarativo) |

**Exemplo:** uma skill de cotação declara em `## Inputs` que aceita `{cd_cliente: integer, segmento: enum[varejo, premium]}`. A partir daí, a API rejeita com erro nomeado qualquer chamada com `segmento: "gold"`, o Playground desenha um formulário com esses dois campos, e o tradutor de linguagem natural sabe exatamente o que extrair de um pedido em português.

**Modo declarativo:** skills com `execution_mode: declarative` executam `## API Bindings` e `## Data Tables` **sem passar por um LLM** — útil para integrações determinísticas (consultar um CEP, ler uma tabela de preços) com custo zero de tokens.

### 3. AI Mesh e o Fluxo de agentes

**Analogia:** o organograma vivo da equipe — quem fala com quem, e em que condição.

**Fundamento:** o *mesh* é o grafo de conexões entre agentes. Cada conexão (aresta) tem um tipo:

| Tipo | Comportamento | Quando usar |
|---|---|---|
| **Paralela** | Os dois destinos executam ao mesmo tempo | Tarefas independentes (resumo + classificação) |
| **Condicional** | O destino só executa se a **regra** for verdadeira | Roteamento por conteúdo/decisão ("se mencionar fraude…") |
| **Cadeia** | O destino executa após a origem, recebendo a saída | Etapas sequenciais (extrair → analisar → responder) |
| **Default** | O caminho de escape quando nenhuma condicional casou | Garantir que todo caso tem destino |

**Exemplo:** Triagem → (condicional: `decision.tipo == 'tecnico'`) → Especialista NOC; Triagem → (default) → Atendimento geral.

### 4. Regras condicionais e o Contrato de Decisão

**Analogia:** as placas de trânsito do fluxo — objetivas, verificáveis, sem "depende".

**Fundamento:** cada regra condicional é uma expressão booleana avaliada num ambiente controlado (Jinja2 *sandboxed*) com um vocabulário **fechado** de variáveis do runtime: o texto de entrada e saída (inclusive versões normalizadas sem acento), sinais como `has_document`/`has_image`, os `inputs.*` estruturados e — o mais poderoso — `decision.*`: os campos que o agente de origem **declarou** em `## Decisions` e anuncia numa linha `DECISAO:` da resposta. Você escreve a regra por três caminhos: galeria de cards prontos, tradutor de linguagem natural, ou expressão manual. Em todos, o sistema **prova** a expressão antes de aceitar (sintaxe, variáveis existentes, avaliação sem erro).

**Exemplo:** você digita "quando o cliente não reconhecer a compra" → a IA propõe `'nao reconhec' in input_norm` → o sistema valida contra as variáveis reais → você sela a regra.

### 5. Frases-Prova — o teste de regressão do roteamento

**Analogia:** frases de clientes reais coladas na porta de cada decisão: "esta frase DEVE entrar por aqui; aquela NÃO".

**Fundamento:** em cada regra condicional você registra frases reais com o veredito esperado (executar/pular). Elas ficam **seladas com a aresta** e são reavaliadas de forma determinística (sem custo de tokens) em quatro momentos: no simulador do editor a cada mudança da regra; **no ato de publicar** (reprovação bloqueia a publicação); **em toda avaliação do pipeline no Harness**; e no histórico (drift e comparação), protegidas por um *hash do conjunto* — mudou uma frase, comparações antigas são recusadas em vez de mentir.

**Exemplo:** a frase "quero um plano novo" com veredito "executar" na aresta que leva ao time de vendas. Se alguém editar a regra e a frase parar de casar, o publish trava e o Harness acusa — antes do cliente sentir.

### 6. Pipeline — a unidade selada

**Analogia:** o produto na prateleira: receita fechada, rótulo com versão, pronto para consumo.

**Fundamento:** um pipeline agrupa agentes do mesh com uma **entrada definida** (o "Início"). A associação é exclusiva (um agente pertence a um pipeline por vez). O agente-raiz define o contrato de `args` do pipeline. Quando publicado, o pipeline é **selado**: grafo e contrato congelados com versão e hash — editar a skill depois não muda a API publicada (a tela avisa que há *drift* e pede republicação).

### 7. Evidência e fundamentação (grounding)

**Analogia:** um parecer técnico que só pode citar documentos do processo — nada de "ouvi dizer".

**Fundamento:** por padrão a plataforma é *grounded-by-default*: o agente responde **apenas** com base em evidências (anexos, bases RAG, resultados de ferramentas). Sem fundamento suficiente, a resposta correta é uma **recusa estruturada** com próximo passo — nunca uma invenção confiante. A busca de evidências é híbrida (BM25 + vetorial com fusão RRF, re-ranqueada por LLM opcional), sobre bases classificadas por confidencialidade. Um agente pode receber a exceção `allow_general_knowledge` quando conhecimento geral for desejado (ex.: brainstorming).

**Ligar o RAG na prática (52.2.0):** declarar fontes no `## Evidence Policy` da skill **não basta** para fundamentar dentro de um pipeline — a busca só dispara com **"Exigir evidência" ligado** no agente. E como os scores da fusão RRF são baixos (~0,03), um `min_relevance` aparentemente razoável (ex.: 0,2) descarta **tudo** em silêncio; use ~0,0. Quando o agente declara fontes mas o RAG foi pulado (evidência desligada), o diagnóstico da resposta aponta a **causa real** em vez do genérico "registre bases".

### 8. FSM — a máquina de estados de toda interação

**Analogia:** a esteira de um protocolo hospitalar: toda entrada passa pelas mesmas etapas, e tudo fica registrado.

**Fundamento:** cada interação percorre 9 estados: `Intake → PolicyCheck → RetrieveEvidence → DraftAnswer → VerifyEvidence → (Recommend | Refuse | Escalate) → LogAndClose`. Invariantes: todo caminho termina em `LogAndClose` (não existe execução sem registro); nenhum rascunho chega ao usuário sem `VerifyEvidence`; toda transição é auditada. A **decisão real** (Recommend/Refuse/Escalate) fica no log de transições — é ela que o Harness compara com o esperado.

**Recusa/escalonamento como estado (53.0.0, opt-in):** uma recusa redigida no texto ("não posso fornecer dados de terceiros") ou um escalonamento ("encaminhar ao NOC") normalmente terminavam em `Recommend` — a decisão ficava só na prosa. Com a flag `verifier_signals_drive_fsm` (Configurações → Parâmetros, **desligada por padrão**), o Verifier detecta esses sinais no rascunho e a FSM transiciona para `Refuse`/`Escalate` — em **qualquer** caminho de verificação, inclusive quando o juiz LLM está indisponível (independente do juiz, 53.0.1). Desligada, o mapeamento de estados é idêntico ao histórico.

### 9. LLM-as-Judge — o segundo par de olhos

**Analogia:** o revisor independente que dá nota ao trabalho — de preferência de outra escola.

**Fundamento:** o Verificador v2 julga cada resposta em 4 dimensões (factualidade, completude, tom, segurança) mais conformidade de contrato e afirmações sem respaldo. Roda no Harness (com gabarito) e, por amostragem, em produção. O modelo do juiz é um papel próprio no Roteamento LLM (`judge`) — idealmente de provedor diferente do gerador, e o sistema marca quando o juiz julgou a si mesmo.

### 10. Contrato de args, prova e dry-run

**Analogia:** o formulário do cartório: campos definidos, validação na entrada, e um "ensaio de preenchimento" antes de assinar.

**Fundamento:** o `## Inputs` do agente-raiz vira o contrato de `args` do pipeline. Toda chamada é validada (tipos coagidos, defaults aplicados, obrigatórios exigidos, enum conferido, campo desconhecido rejeitado com "você quis dizer…?"). O modo **dry** (`dry: true`) resolve tudo isso **sem executar e sem gastar tokens**, devolvendo o payload final e a origem de cada valor (você × default). O envelope distingue campos `exatos` (viajam selados, fora do LLM) de campos `interpretáveis` (entram na prosa), via anotação `x-uso`.

### 11. MCP e o modo per-tool

**Analogia:** tomadas padronizadas para plugar ferramentas externas — e, na versão moderna, cada ferramenta com o seu próprio plugue etiquetado.

**Fundamento:** MCP (Model Context Protocol) conecta servidores de ferramentas (busca web, arquivos, APIs) aos agentes. No modo clássico, o LLM via cada conector como uma função genérica `{operation, query}`. No modo **per-tool**, cada ferramenta **descoberta** no servidor vira uma função própria com o schema real — menos erro, mais precisão. O modo é decidido **por conector** (Herdar global / Ligado / Desligado), permitindo pilotar num conector só ou fazer opt-out pontual. A descoberta acontece no "Testar conexão" (inclusive para servidores locais via `stdio`, ex.: `npx -y pacote`).

### 12. Selo, gate e vigilância

**Analogia:** lacre de qualidade + inspeção de fábrica + câmera ligada depois da entrega.

**Fundamento:** três mecanismos que transformam intenção em garantia: o **selo** congela contratos ao publicar; o **gate** bloqueia o que não passa na prova (Frases-Prova no publish; thresholds de métricas no Harness); a **vigilância** compara o presente com o passado (regressão por alvo, eventos de *drift* release a release). É a espinha dorsal "declarar → provar → selar → vigiar".

---

# Parte II — Como o Maestro pensa (arquitetura)

## O desenho geral

```
┌────────────────────────────────────────────────────────────────────┐
│  FRONTEND — Jinja2 + Tailwind + Alpine.js (31 páginas)             │
│  Dashboard · Agentes · Skills · Fluxo · Playground · Catálogo ·    │
│  Harness · Qualidade · Observabilidade · Configurações · …         │
│  ⌘K busca global · Tour guiado · Ajuda contextual por página       │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ REST (23 módulos de rota, 150+ endpoints)
┌──────────────────────────────▼─────────────────────────────────────┐
│  BACKEND — FastAPI (Python 3.11, async)                            │
│                                                                    │
│  Motor de agentes (LangGraph) ── FSM 9 estados ── Protocolo A2A    │
│  Evidence Runtime (BM25+vetorial+RRF) ── Verificador (juiz 4D)     │
│  Harness de Avaliação ── Drift ── Frases-Prova (determinístico)    │
│  Contrato de args (prova/dry/selo) ── Anexos (2 transportes)       │
│  MCP runtime (per-tool por conector) ── Skills declarativas        │
│  Tabular (DuckDB, SQL parametrizado) ── Federação A2A (HMAC)       │
│  Jobs assíncronos (202+reaper+webhook) ── LGPD (forget/retenção)   │
│                                                                    │
│  PostgreSQL 16 (52 tabelas, pgvector, migrações idempotentes)      │
│  Redis (memória de contexto) ── LangFuse (traces) ── Prometheus    │
└────────────────────────────────────────────────────────────────────┘
```

## Princípios arquiteturais (e o que significam na prática)

1. **SKILL.md é soberano** — se não está declarado, o agente não faz. Auditoria vira leitura de texto, não engenharia reversa.
2. **Separação de planos** — o catálogo/governança (control plane) é distinto da execução (data plane). Publicar não executa; executar não republica.
3. **Determinismo declarativo** — o LLM decide *como* dentro do espaço permitido; nunca *o quê* fora dele.
4. **Contexto é cidadão de primeira classe** — viaja em envelope tipado (A2A), com orçamento, prazo e assinatura; nunca por concatenação cega de histórico.
5. **Evidência sobre geração livre** — e recusa controlada é comportamento correto, não falha.
6. **Tudo que se declara, se prova** — regras, frases, contratos e sugestões de IA passam por validação de máquina antes de valer.
7. **Métricas sem falsa confiança** — amostras pequenas ganham aviso, comparações incompatíveis são recusadas com o motivo, e todo número diz como foi calculado.

## Stack tecnológico

| Camada | Tecnologia | Papel |
|---|---|---|
| Linguagem | Python 3.11+ | Backend completo |
| Web | FastAPI + Uvicorn | API REST assíncrona |
| Motor de agentes | LangGraph (StateGraph) | Grafos com ciclos e condições |
| LLMs | Azure OpenAI · OpenAI público · Maritaca (Sabiá) · Ollama · GPT-OSS 20B/120B | Multi-provedor com roteamento por tarefa e fallback |
| Embeddings | Qwen3-Embedding (hub interno) ou Azure | Busca vetorial |
| Banco | PostgreSQL 16 + asyncpg + pgvector | 52 tabelas, pool async, migrações idempotentes no boot |
| Tabular | DuckDB | Consultas SQL parametrizadas sobre CSV/XLSX |
| Cache | Redis | Memória de conversa e circuit-breaker |
| Observabilidade | LangFuse + logs estruturados JSON + Prometheus/Grafana | Traces, métricas RED, troubleshooting |
| Frontend | Jinja2 + Tailwind + Alpine.js | Server-side com reatividade leve, sem build step |
| Contêineres | Docker + docker-compose | Ambiente local = produção |

## O modelo de dados em uma olhada (52 tabelas)

Agrupadas por responsabilidade — os nomes dizem quase tudo:

- **Construção:** `agents`, `skills`, `agent_bindings`, `system_prompts`, `domains`, `users`
- **Mesh e pipelines:** `mesh_connections`, `pipelines`, `pipeline_agents`, `car_entries`
- **Runtime:** `interactions`, `turns`, `envelopes`, `journeys`, `uploaded_files`, `invoke_jobs`
- **Conhecimento:** `knowledge_sources`, `evidence_chunks`, `evidences`, `data_tables`, `saved_queries`, `data_table_query_logs`
- **Ferramentas e integrações:** `tools`, `tool_calls`, `api_connectors`, `api_endpoints`, `api_call_logs`, `binding_executions`
- **Qualidade:** `gold_cases`, `eval_runs`, `verifications`, `verifier_jobs`, `drift_events`, `releases`
- **Catálogo/governança:** `catalog_entries`, `catalog_submissions`, `catalog_capability_disclosure`, `catalog_costs`, `catalog_external_metadata`, `catalog_recipes`, `catalog_recipe_executions`, `catalog_pipeline_defs`, `catalog_conformance_reports`, `invocation_costs`
- **Acesso e custo:** `api_keys`, `api_key_cost_ledger`
- **Federação:** `federation_peers`, `federation_nonces`
- **Plataforma:** `platform_settings`, `audit_log`, `playground_runs`, `playground_run_threads`

---

# Parte III — Instalação e primeiros 15 minutos

## Pré-requisitos

- **Docker + docker-compose** (caminho recomendado — sobe app, PostgreSQL, Redis e o resto da infra de uma vez)
- Uma chave de LLM (Azure OpenAI, OpenAI, Maritaca…) — pode ser configurada **depois, pela tela**, sem mexer em arquivo

## Subindo

```bash
git clone https://github.com/sergiogaiotto/agente-inteligencia.git
cd agente-inteligencia

docker compose up --build -d

# Acesse:
# http://localhost:7000
```

No boot, a aplicação cria o schema (52 tabelas com `CREATE TABLE IF NOT EXISTS`), aplica migrações idempotentes (`ALTER TABLE … IF NOT EXISTS` — atualizar versão nunca perde dados) e copia as configurações do banco para o ambiente (`platform_settings` é a fonte da verdade; o `.env` é só semente).

> **Importante para produção:** defina `MAESTRO_SECRET_KEY` no ambiente — é a chave-mestra da cifra de segredos em repouso e da Federação (que falha fechado sem ela).

## Primeiro acesso

1. Abra `http://localhost:7000` → a tela de **Login** entra em **modo setup** e pede a criação do primeiro usuário **Root**.
2. Vá em **Configurações → Plataforma** e preencha as credenciais do seu provedor de LLM. Elas são **seladas**: valem a partir do banco, tela a tela, aba a aba.
3. Em **Configurações → Roteamento LLM**, escolha qual modelo atende cada tipo de tarefa (pode deixar os defaults).

## Seus primeiros 15 minutos (roteiro guiado)

1. **Skills → Nova skill** → clique em **"Wizard IA"** e descreva em português: *"responder dúvidas sobre horário de funcionamento e segunda via de fatura"*. O Wizard gera o SKILL.md completo; revise e salve.
2. **Agentes → Novo agente** → wizard de 4 passos; vincule a skill criada; escolha a camada **Especialista**.
3. Repita para um agente **Triagem** (roteador) simples.
4. **Fluxo de agentes** (`/mesh/flow`) → arraste uma conexão Triagem → Especialista, tipo **condicional**, e use o tradutor: *"quando mencionar fatura ou segunda via"*. Cole 2 Frases-Prova ("preciso da segunda via" ✓ executa; "quero cancelar tudo" ✗ pula).
5. Crie o **Pipeline** com os dois agentes e defina o Início.
6. **Playground** (`/mesh/playground`) → selecione o pipeline → **Executar** com uma frase real → veja a trilha por agente.
7. **Harness** (`/harness`) → crie 3 casos gold (1 adversarial) → rode um **baseline** apontando para o pipeline.
8. Quando estiver satisfeito: **Catálogo → Publicar** — o contrato é selado e as Frases-Prova rodam como gate.

Você acabou de percorrer o ciclo inteiro: **declarar → provar → selar → vigiar**.

---

# Parte IV — Tour completo, módulo a módulo

Cada módulo abaixo segue o mesmo esqueleto: **O que é · Fundamento · Quando usar · Caso de uso · Exemplo de ação · Dicas**.

## 4.1 Login e papéis

- **O que é:** porta de entrada com sessão assinada por cookie. No primeiro acesso, vira o assistente de criação do usuário **Root**.
- **Fundamento:** três papéis — **Root** (tudo, inclusive credenciais e fila do catálogo), **Admin** (parâmetros, usuários, preços) e **Membro** (operação). Toda a API `/api/v1/*` é *default-deny*: sem cookie de sessão ou chave de API, nada responde.
- **Exemplo de ação:** Root cria os colegas em **Configurações → Usuários**, atribuindo papel e domínio.

## 4.2 Dashboard (`/`)

- **O que é:** a fotografia da operação — métricas compactas e a topologia por camada (Maestro / Triagem / Especialista) com contagem de ativos.
- **Quando usar:** como ponto de partida do dia; os cards levam direto aos módulos.
- **Dica:** o item **Guia Interativo** (e o `?` de cada página) abre ajuda contextual em três níveis: Tour, Guia dos Módulos e "Ajuda desta página".

## 4.3 Agentes (`/agents`, `/agents/new`, `/agents/{id}/invocations`)

- **O que é:** o cadastro dos "funcionários" de IA.
- **Fundamento:** agente = camada + skill + política de LLM + interruptores de anexo/idioma. O formulário é um wizard em passos com **pré-flight**: uma checagem de prontidão (skill vinculada? modelo alcançável? contradições?) antes de salvar.
- **Quando usar:** sempre que uma nova responsabilidade surgir — prefira **vários especialistas enxutos** a um generalista gigante; o fluxo é quem compõe.
- **Caso de uso:** criar o "Especialista NOC" com skill de abertura de incidentes, aceitando documentos (para logs anexados), reasoning alto.
- **Exemplo de ação:** `/agents/new` → passo 1 escolha a camada (os cards explicam a metáfora e o "quando usar" de cada uma) → passo 2 tipo de tarefa (deixe o roteamento escolher o modelo) → passo 3 prompt (use "IA, me ajude") → passo 4 revisão com pré-flight verde → salvar.
- **Dica:** `/agents/{id}/invocations` mostra o histórico paginado de execuções do agente, filtrável pelo estado final (Recommend/Refuse/Escalate…) — ótimo para investigar "por que ele recusou?".

## 4.4 Skills (`/skills`, `/skills/new`)

- **O que é:** o editor das partituras (SKILL.md).
- **Fundamento:** parser canônico com validação tolerante (warnings não bloqueiam; conteúdo vazio sim), hash de integridade, versão com bump automático a cada edição, linter de regras.
- **Quando usar:** toda capacidade nova nasce aqui, de preferência pelo **Wizard IA** — descreva em português e ele gera o SKILL.md completo, já com as ferramentas MCP selecionadas e (novidade 39.2) **ensinando os nomes reais das ferramentas descobertas** quando o conector está em modo per-tool.
- **Caso de uso:** skill declarativa de consulta de boletos: `## API Bindings` chama o endpoint HTTP, `## Inputs` declara `{cpf: string}` — execução sem LLM, custo zero.
- **Exemplo de ação (dry-run):** na tela da skill, rode o **dry-run da tool** — simula a chamada MCP sem tocar o servidor: mostra o function spec que o LLM verá, o payload que seria enviado e um diagnóstico de problemas (operação inventada, contrato divergente). Se o conector estiver em modo per-tool, um aviso no topo explica que o runtime exporá as funções reais.
- **Dica:** as seções `## Inputs` e `## Decisions` são as que mais pagam retorno — tudo que a plataforma prova depois nasce delas.

## 4.5 API Connectors (`/api-connectors`)

- **O que é:** a árvore de conectores HTTP e endpoints que alimentam as **skills declarativas**.
- **Fundamento:** cada conector agrupa endpoints com método, URL, headers e autenticação; há teste inline, proxy de execução, health por conector e até **sugestão de endpoint via IA** a partir de uma descrição.
- **Quando usar:** quando a resposta vem de um sistema seu (ERP, CRM, API pública) e não precisa de "criatividade" — só de uma chamada bem feita.
- **Exemplo de ação:** cadastre o conector "ViaCEP" → endpoint GET com `{{ inputs.cep }}` na URL → teste inline → referencie no `## API Bindings` da skill.

## 4.6 MCP — Tool Registry (`/mcp`)

- **O que é:** o registro das ferramentas externas via Model Context Protocol.
- **Fundamento:** cada conector tem endpoint (HTTP ou comando stdio), autenticação (API Key/OAuth2/mTLS, cifradas em repouso), classificação de sensibilidade, e — o coração moderno — **ferramentas descobertas** com schema real.
- **Quando usar:** busca na web (Tavily), documentação (Context7), sistemas de arquivos, qualquer servidor MCP do ecossistema.
- **Caso de uso (piloto per-tool):** com a frota no modo clássico, ligue **Modo per-tool: Ligado** só no conector Tavily. A partir daí o LLM enxerga `tavily_search`, `tavily_extract`… como funções separadas com os campos reais (`query`, `max_results`, `search_depth`) em vez do genérico `{operation, query}`.
- **Exemplo de ação:** registrar → **Testar conexão** (para stdio a 1ª execução pode demorar: o `npx` baixa o pacote) → conferir "N ferramentas descobertas" → decidir o Modo per-tool.
- **Dica:** o botão de **backfill** descobre em lote os conectores antigos que ainda não têm ferramentas persistidas.
- **Cobertura per-tool (40.0.0):** um painel no topo mede a **prontidão** da frota para aposentar o caminho legado `{operation, query}` — quantos conectores já têm ferramentas descobertas — separado da **adoção** (quantos rodam per-tool hoje). Conector sem descoberta ganha o chip **legado** com dica acionável; `oauth2`/`mTLS` aparecem como pendência nomeada (o backfill em lote não os cobre; use "Testar conexão"). O endpoint `GET /api/v1/tools/per-tool-coverage` é o gate objetivo dessa transição. O **dry-run da ferramenta** também virou per-tool completo: com o conector em modo per-tool, ele simula a função **real** descoberta e os args crus, não mais o par genérico.

## 4.7 RAG — Base de Conhecimento (`/rag`)

- **O que é:** as fontes autorizadas de evidência (a matéria-prima do "responder com fundamento").
- **Fundamento:** cada base tem classificação de confidencialidade (público/interno/confidencial/restrito) e um modo (`text` para busca semântica, `tabular` para virar tabela consultável, `hybrid`). A ingestão aceita arquivo e URL; o texto é fatiado em *chunks*, indexado em BM25 + vetores (pgvector), e consultado com fusão RRF.
- **Quando usar:** políticas internas, catálogos de produto, FAQs — tudo que o agente deve **citar**, não inventar.
- **Caso de uso (Onda Tabular):** suba um XLSX de preços → "promover a tabela" → o arquivo vira uma `data_table` DuckDB; a skill referencia em `## Data Tables` e consulta por **SQL parametrizado** (não é text-to-SQL solto: a consulta é compilada e curada, com parâmetros validados).
- **Dica:** o banner de saúde do vector store avisa quando a dimensão dos embeddings mudou (ex.: trocou o provedor) e oferece o reindex — sem isso a busca vetorial volta vazia.

## 4.8 Fluxo de agentes (`/mesh/flow`) — o Estúdio de Pipelines

- **O que é:** o editor visual único do mesh: canvas com nós arrastáveis, pan/zoom, lente por pipeline, painel de detalhe do agente, e o **modal Executar** com trilha ao vivo por agente e suporte a anexos.
- **Fundamento:** tudo da Parte I §3–§6 acontece aqui — tipos de aresta, regras provadas, Frases-Prova, replay da última execução.
- **Quando usar:** desenhar e evoluir o fluxo; testar rapidamente com o Executar antes de formalizar no Playground.
- **Caso de uso completo:** triagem de telecom — Triagem (roteador) com três saídas condicionais (técnico / financeiro / vendas) + default (atendimento geral); cada saída com 2–4 Frases-Prova.
- **Exemplo de ação (regra em 60 segundos):** clique na conexão → "Descreva em português: *se a decisão for escalar*" → **Gerar regra** → o card verde mostra `decision.escalar == 'sim'` provada → **Usar esta regra** → cole as frases de teste → salvar.
- **Dicas:** a linha `DECISAO:` do agente de origem é o que alimenta `decision.*` — declare `## Decisions` na skill; anexos executados por aqui usam upload da sessão (2 passos) e são roteados pelos interruptores "aceita documentos/imagens" de cada agente.

### O que o canvas ganhou (41.x)

- **🖱️ Menu de Regência (botão direito):** cada nó abre um menu contextual próprio (nada de menu nativo do navegador) com ações reais — **Conhecer o agente**, Definir como Início, Abrir Skill no dossiê, Ver execuções recentes, **Isolar vizinhança** (esmaece tudo que não conecta ao nó, para ler grafos grandes) e Editar agente. As **conexões** também têm menu: rodar Frases-Prova, editar regra, excluir.
- **📋 Dossiê do Agente (clique esquerdo):** o painel direito mostra a **Skill** vinculada em cascata — nome, selos e um botão "Ver SKILL.md" que abre um **leitor expandido** (markdown legível, botão copiar); o mesmo para o prompt do sistema. Traz também as **execuções recentes** do agente. Cursor vira pointer em tudo que é clicável.
- **💬 Conhecer o agente (54.0.0):** um chat que **explica** o agente — o que faz, seu propósito, quando é acionado, como está configurado e sua posição no fluxo — ancorado na definição real (config + SKILL.md + arestas do mesh com as regras + diagnóstico agregado). É um assistente *sobre* o agente, **não é o agente**: não executa, não cria interação, não gasta o orçamento dele nem entra no histórico (endpoint `POST /agents/{id}/explain`, superfície de UI). Para testar de verdade, o painel aponta o **Playground / Executar**.
- **🧭 Simulador de roteamento no canvas:** no menu de um roteador, digite uma frase de cliente e **as arestas acendem** — a que casou fica sólida, as demais esmaecem. Determinístico, pelo **mesmo motor do publish e do harness** (`test-conditional`): custo **zero de tokens**. Cada resultado pode virar **Frase-Prova** da aresta com um clique (o veredito observado vira o `expect`), e o menu da aresta roda as Frases-Prova existentes na hora.
- **Painel do pipeline:** os textos de "Roteamento rápido" e "Auditoria da resposta" viraram popovers atrás de um **"?"** (menos paredão de prosa); o **selo do contrato** aparece explícito (🔒 selado · vN / 🔓 não selado) com um "?" explicando que **publicar sela**; e o **domínio** do pipeline (a etiqueta que vira chip na lista) é editável ali mesmo.

## 4.9 Workspace (`/workspace`)

- **O que é:** o chat operacional multi-sessão — conversar com um agente ou pipeline com o log de execução ao lado.
- **Fundamento:** cada conversa é uma *interaction* com FSM completa; a memória multi-turno é reconstruída por sessão com escopo por camada; upload de arquivos entra como evidência do turno.
- **Quando usar:** atendimento assistido, testes exploratórios, e o **invoke direto de binding** — executar uma ferramenta MCP/API/RAG/tabela *sem LLM*, com formulário tipado.
- **Exemplo de ação (invoke direto per-tool, novidade 39.3):** selecione o agente → painel de bindings → com o conector em modo per-tool, aparece **um formulário por ferramenta real** (ex.: `tavily_search` com `query`, `max_results`) → preencha → executar → resultado cru, com opção de tradução.
- **Dica:** para continuar uma conversa via API, reenvie o `interaction_id` como `session_id` — mesma semântica do chat.

## 4.10 Playground (`/mesh/playground`)

- **O que é:** o console de API — testa o pipeline **como o seu aplicativo o verá**, com chave real e resposta projetada.
- **Fundamento:** os helpers do formulário de entrada (Parte I §10): *inputs esperados* (formulário tipado do contrato), **IA: descrever** (tradutor português→args com prova e selo), *inserir template*, *pré-visualizar* (dry). Modo Conversa (multi-turn ao vivo), comparação A/B (duas execuções lado a lado), mapa de erros clicável (dispara o 401/404/400 de verdade para você ver o corpo), histórico de runs no servidor e export do cockpit em PPTX.
- **Quando usar:** antes de entregar a integração — e sempre que "funciona na UI mas não no meu app".
- **Exemplo de ação (NL→args, novidade 38.x):** selecione o pipeline → **IA: descrever** → digite *"atendimento urgente do cliente tier gold pelo canal app, valor 250"* → card **verde** com `{tier:"gold", canal:"app", valor:250}` e o selo "provado contra o contrato selado v3" → **Usar estes args** → a pré-visualização dry dispara sozinha → Executar.
- **Codegen:** snippets em 10 linguagens, coleção Postman, **SDK Python** (com `args`, `attachments` e conversa multi-turn) e fragmento OpenAPI — todos gerados do estado atual do seu teste.

## 4.11 Catálogo (`/catalog` + publicar/fila/inventário/curadoria/custos)

- **O que é:** o marketplace corporativo — o que a empresa oficializou como capacidade disponível.
- **Fundamento:** ciclo de governança completo: **publicar** (wizard em 4 passos com divulgação de capacidade: PII? APIs externas? volumetria?) → **fila de revisão** (Root decide, com pré-verificações) → publicado/desativado/arquivado. Pipelines publicados ganham o **selo de contrato** e passam pelo **gate de Frases-Prova**. Recipes permitem compor execuções; plataformas externas entram com sonda de conformidade.
- **Quando usar:** sempre que algo sai de "experimento do time" para "serviço da empresa".
- **Sub-páginas:** **Inventário** (visão regulatória: cruza entries com a divulgação de capacidade; exporta CSV para o comitê de privacidade), **Curadoria** (entradas por área, detecção de órfãs e paradas), **Custo & Consumo** (quem consome o quê, por dia/área; Root vê tudo, cada um vê o seu).
- **Exemplo de ação:** publicar o pipeline de triagem → o gate roda as Frases-Prova de todas as arestas → uma reprova → a publicação **trava** com o relatório frase a frase → você corrige a regra no Fluxograma → publica de novo (ou, excepcionalmente, publica com override — auditado, com as contagens).

## 4.12 Releases (`/releases`)

- **O que é:** o Version Registry — cada release é uma composição nomeada (modelo + prompt + índice + política) que caminha `staging → canary → production`.
- **Fundamento:** o Harness referencia a release em cada avaliação; release reprovada no gate não deve ser promovida. Releases de teste podem ser excluídas (com trava para as que estão em canary/produção).
- **Exemplo de ação:** criar `release-2026-07-v2` → rodar baseline no Harness → promover a canary → repetir a suíte → produção.

## 4.13 Harness de Avaliação (`/harness`)

- **O que é:** o ensaio geral com nota — o Golden Dataset e as execuções de avaliação.
- **Fundamento:** detalhado na Parte VI. Em resumo: casos reais (com estado esperado, categoria, peso, regex e *red flags*), avaliação por **alvo** (agente isolado ou pipeline completo), gate multi-dimensional, comparação A×B com casos divergentes, painel **Baseline por alvo**, e as **Frases-Prova rodando em todo run de pipeline**.
- **Exemplo de ação:** monte 15 casos (5 adversariais) → baseline no pipeline → mude o prompt do especialista → rode **regressão** → o gate compara com o baseline do mesmo alvo e acusa a queda de acurácia antes de qualquer cliente.
- **Golden Dataset editável (40.2.0):** cada caso tem **editar** (✏️) e **excluir** (🗑️) na própria linha — corrigir um typo não exige mais recriar o caso do zero. A integridade histórica é preservada por desenho: cada execução guarda o *hash* do conjunto que avaliou, então editar/excluir um caso **não reescreve resultados passados** — as próximas execuções é que passam a usar o conjunto novo (a confirmação de exclusão explica isso).
- **Métrica de alucinação honesta (52.2.1):** a `hallucination_rate` é medida **só sobre os casos em que a factualidade foi de fato avaliada** — e fica `N/A` quando o juiz não pôde pontuar nenhuma —, espelhando `safety`/`contract`. Assim um pipeline **corretamente fundamentado** deixa de ser reprovado por uma métrica que o próprio harness não conseguiu medir (ex.: quando as evidências recuperadas não chegam ao juiz do envelope reancorado).

## 4.14 Qualidade / Auditoria (`/quality`)

- **O que é:** a leitura do **LLM-as-Judge**: cada verificação multi-dimensional registrada, com filtros e deep-link a partir da conversa.
- **Fundamento:** dimensões 1–5 (factualidade, completude, tom, segurança), conformidade de contrato, afirmações sem respaldo; julgamento assíncrono por amostragem em produção; re-julgamento sob demanda.
- **Quando usar:** investigar "o que exatamente estava errado naquela resposta?" e acompanhar a saúde qualitativa fora dos ensaios.
- **Quem é o usuário (42.0.0):** cada verificação mostra o **dono** da interação julgada (nome resolvido no servidor) e há **filtro por usuário** — para responder "quais respostas do fulano falharam no juiz?". Ver a nota de atribuição de usuário em [4.15](#415-observabilidade-observability-infra-infra-e-histórico-history).

## 4.15 Observabilidade (`/observability`), Infra (`/infra`) e Histórico (`/history`)

- **Observabilidade** — o **como** executou: interações, latências, tokens, eventos de drift; logs estruturados com administração (tail, rotate, explicação de erro por IA); link para LangFuse; métricas RED por caminho (`/metrics` Prometheus + dashboard Grafana).
- **Infra** — status e latência de cada serviço do docker-compose, com link para a UI nativa (útil para "é a plataforma ou é o banco?").
- **Histórico** — consulta unificada e paginada de interações, turnos e auditoria, com busca textual. A trilha de auditoria é *append-only*: criação/edição, transições da FSM, promoções, publicações, overrides — tudo com autor e detalhe.
- **Quem é o usuário (42.0.0):** na hora de investigar um erro, a pergunta "quem disparou isto?" agora tem resposta na tela. Interações mostram o **dono** (nome resolvido no servidor, porque a lista de usuários é restrita a admin), a auditoria mostra o **ator com nome**, e a linha de erro do Log Viewer identifica o usuário. Três regras de honestidade: chamada por chave de API não é clique humano → aparece o dono da chave com o badge **"via chave: nome"**; interação legada sem dono → **"—"** (nunca se inventa um autor); usuário deletado → UUID curto + "(removido)".
- **Dica de troubleshooting:** `docs/troubleshooting.md` cataloga sintoma → consulta nos logs por `event=`.

## 4.16 Federação A2A (`/federation`)

- **O que é:** duas instâncias Maestro (ex.: matriz e filial, ou você e um parceiro) descobrindo e invocando capacidades uma da outra.
- **Fundamento:** manifesto público em `/.well-known/maestro-federation.json` (só capacidades publicadas e não-federadas), peers com segredo compartilhado cifrado (rotação com janela de sobreposição), invoke com envelope assinado HMAC e proteção anti-replay; guarda SSRF no egress. **Desligada por padrão** e falha fechado sem `MAESTRO_SECRET_KEY`.
- **Exemplo de ação:** habilitar a federação nas duas pontas → registrar o peer (workspace + URL + segredo exibido uma única vez) → **Sync** puxa as capacidades remotas → invocá-las como entries locais somente-leitura.

## 4.17 Configurações (`/settings`)

Abas separadas por papel:

| Aba | Quem vê | O que controla |
|---|---|---|
| **Plataforma** | Root | Credenciais dos provedores LLM (seladas), LangFuse, chaves de infraestrutura |
| **Roteamento LLM** | Root/Admin | Modelo por tipo de tarefa + papel `judge` + fallback visível no trace |
| **Parâmetros** | Root/Admin | ~40 parâmetros com **valor efetivo + fonte** (banco vs ambiente) e "restaurar padrão": gates do Harness, verifier, sinais de decisão Verifier→FSM (`verifier_signals_drive_fsm`), invoke assíncrono, per-tool global, RAG com gabarito, gate de Frases-Prova… Tudo vale **em runtime, sem restart** |
| **Preços LLM** | Root/Admin | Tabela de preço por modelo → SSOT de custo |
| **System Prompts** | Root | Biblioteca versionada de prompts reutilizáveis |
| **Usuários** | Root/Admin | CRUD com papéis e domínios |
| **API Keys** | todos | Criação/escopo/orçamento das chaves (ver Parte V) |

O registro completo de opções está em [`docs/configuracoes-plataforma.md`](docs/configuracoes-plataforma.md) (~90 opções comentadas).

---

# Parte V — A API para desenvolvedores

## Autenticação

Duas formas: **cookie de sessão** (a própria UI) e **chave de API** (`X-API-Key: ag_live_…`) para integrações. Chaves são criadas em Configurações → API Keys, com:

- **Escopo**: somente leitura e/ou lista de pipelines permitidos;
- **Orçamento de custo** (opcional): débito real por invocação no ledger e bloqueio `402` ao estourar a janela;
- **Webhook padrão** para conclusão de jobs assíncronos;
- **Estação de cURL**: ao criar a chave, a tela gera os comandos prontos (com o exemplo de anexos, válido para agentes e pipelines).

Dois endurecimentos opcionais (Parâmetros): `api_key_public_surface_only` (a chave só alcança descoberta+invoke) e `api_key_invoke_published_only` (a chave só invoca pipelines **publicados**, i.e., contrato selado).

## Invocar um pipeline

```bash
# Síncrono
curl -X POST http://localhost:7000/api/v1/pipelines/<PIPELINE_ID>/invoke \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"message": "quero um plano novo para o cliente 1031", "verbosity": "summary"}'
```

- **`message`** (texto livre) e/ou **`args`** (parâmetros estruturados validados contra o contrato).
- **`verbosity`**: `full` (tudo, incluindo trace/custo), `summary` (resposta + narrativa por etapa) ou `minimal`. O padrão por chave é configurável.
- **Multi-turn**: reenvie o `interaction_id` da resposta como `session_id` no próximo turno.
- **Streaming**: `POST …/invoke/stream` devolve eventos SSE por etapa (é o que o modal Executar e o Playground consomem).

## Args com prova (e o dry-run)

```bash
# Ensaiar sem executar (não gasta tokens):
curl -X POST …/invoke -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"args": {"cd_cliente": "1031", "segmento": "varejo"}, "dry": true}'
# → resolved_args (com tipos coagidos e defaults), provenance (você × default),
#   uso (exato × interpretar), sealed + contract_version
```

Erros de contrato voltam como `422 args_validation_failed` **nomeando cada campo** (obrigatório ausente, tipo, enum, campo desconhecido com "você quis dizer"). A descoberta do contrato é pública: `GET …/inputs-schema` (inclui versão/hash do selo, aviso de drift e — desde a 38.2 — as **capacidades de anexo** da cadeia).

## Anexos (dois transportes)

```bash
# Chamada única (base64) — máx. 5 arquivos × 10 MB:
python - <<'EOF'
import base64, json
b64 = base64.b64encode(open("nota.pdf","rb").read()).decode()
print(json.dumps({"message":"resuma o documento",
  "attachments":[{"filename":"nota.pdf","content_base64":b64}]}))
EOF
# → corpo do POST …/invoke
```

- **upload-ref** (2 passos): `POST /api/v1/workspace/upload` (multipart) e referencie o descriptor devolvido — o caminho da UI.
- Documento vira texto extraído (PDF/DOCX/XLSX via conversor); **imagem vai como pixels ao modelo multimodal**.
- O dispatcher entrega cada anexo **apenas** aos agentes que o aceitam.
- Violações → `422` nomeado. O `/invoke/async` ainda não aceita base64 (use upload-ref).
- **O Playground ensina isso (41.3.1):** ao anexar um arquivo no Playground, o **código gerado** (curl/Python/…) passa a incluir o bloco `attachments` com o base64 — um integrador copia o snippet e já vê o formato exato, sem adivinhar. Antes o código omitia o anexo mesmo com o arquivo anexado na UI.

## Assíncrono (202 + jobs)

```bash
curl -X POST …/invoke/async -H "X-API-Key: $KEY" \
  -H "Idempotency-Key: pedido-8812" -H "Content-Type: application/json" \
  -d '{"message":"análise completa do contrato X"}'
# → 202 + Location: …/jobs/<id>   (polling)  + webhook assinado na conclusão
```

Idempotência de verdade: mesmo `Idempotency-Key` + mesmo corpo → o **mesmo job** (200); corpo diferente → `409`. Job store durável com retomada pós-restart, prazo por job, retenção configurável e métricas RED. Exige a flag *Invoke assíncrono* ligada em Parâmetros (runbook operacional em [`docs/invoke-async-runbook.md`](docs/invoke-async-runbook.md)).

## Invocar um agente isolado

`POST /api/v1/agents/{id}/invoke` — mesmo espírito (inputs, sessão, anexos base64, contexto), para quando você quer o especialista sem o fluxo.

## Superfícies auxiliares

- `GET /api/v1/agents/callable` — descoberta do que a chave pode chamar;
- `POST /api/v1/pipelines/{id}/suggest-args` — o tradutor português→args (superfície **da UI**, cookie; integrações validam com `dry`);
- `POST /api/v1/privacy/forget` — esquecimento LGPD (Parte VII);
- `GET /metrics` — Prometheus.

---

# Parte VI — Qualidade contínua em profundidade

## O Golden Dataset

Cada caso gold descreve uma situação real: o texto de entrada, o **estado esperado** da decisão (Recommend/Refuse/Escalate), a resposta esperada (por similaridade ou por **regex** `expected_pattern`), a categoria, o **peso** (casos críticos pesam mais na média) e as **red flags** — strings que jamais podem aparecer na resposta (ex.: "senha", "CPF de outro cliente"). Casos **adversariais** (tentativas de burlar) são essenciais: sem eles, a "taxa de recusa correta" é vácuo — e a tela avisa.

## Avaliação por alvo

Todo run tem um **alvo**: um agente isolado (clássico) ou um **pipeline** (modo que executa a cadeia selada caso a caso e torna o **roteamento avaliável** — cada caso registra o caminho percorrido). O gate de regressão compara **apenas** com baseline do mesmo alvo e do mesmo dataset (hash do conteúdo, não só o rótulo); o comparador A×B recusa pares incompatíveis com o motivo explícito.

## As métricas e o gate

Acurácia ponderada, recusa correta, falso positivo, factualidade/completude/tom (1–5, via juiz), violações de segurança, conformidade de contrato, alucinação, latência — cada uma com threshold configurável em Parâmetros. O resultado é binário (`approved`/`rejected`) com **cada razão nomeada**. Em regressão, quedas acima do tolerado (por dimensão e de acurácia) reprovam.

## Frases-Prova no Harness (o ciclo fechado)

Todo run de **pipeline** reexecuta as Frases-Prova de todas as arestas condicionais — determinístico, custo zero. O resultado aparece no card (frase a frase), pode reprovar o run (gate opcional `harness_phrases_gate`) e entra no histórico **atrás de um hash do conjunto**: drift e comparação só acontecem entre runs que avaliaram as mesmas frases. Honestidade primeiro: frases provam a **regra**, não o comportamento do modelo — por isso nunca se misturam com a acurácia.

## Drift release a release

A cada avaliação, as métricas são comparadas com o baseline comparável e movimentos acima do ruído viram **eventos de drift** com severidade (melhora = info; piora leve = warning; piora acima do gate = critical), consultáveis por alvo na Observabilidade.

## Baseline por alvo (e seus avisos)

O painel do Harness mostra o baseline vigente de cada alvo **com o critério na tela** (o mais recente concluído — apagar um run muda o baseline), badges de **amostra pequena** (n<5 = provisório) e o asterisco no `refusal_rate` de 100% quando pode ser vácuo.

---

# Parte VII — Segurança, governança e LGPD

| Tema | Como a plataforma trata |
|---|---|
| **Autenticação/RBAC** | Sessão assinada + papéis (Root/Admin/Membro); API default-deny; endurecimentos por chave |
| **Segredos** | Cifrados em repouso (Fernet com `MAESTRO_SECRET_KEY`); a UI mostra só fingerprints; GET nunca devolve o valor |
| **Confidencialidade** | Bases e ferramentas classificadas (público→restrito); trusted context para tools sensíveis |
| **Auditoria** | `audit_log` append-only com ator, ação, detalhes e IP — inclusive overrides de gate. As telas resolvem o **nome do ator/dono** (server-side), mas o dado exibido é o **operador da plataforma** — o titular-final (`customer_hash`) permanece hasheado e jamais aparece |
| **LGPD — esquecimento** | `POST /privacy/forget` apaga por titular (hash do `customer_ref`): interações, turnos, verificações, jobs e **arquivos enviados** (binário incluso), em lotes até esgotar |
| **LGPD — retenção** | Janelas configuráveis; purga automática de órfãos; resultados de jobs expiram (72h padrão) |
| **Custo governado** | Preço por modelo → SSOT de custo por invocação; débito por chave com teto `402`; painéis por entry/área/consumidor. Princípio: cálculo de custo **nunca** no caminho da resposta |
| **Injeção de prompt** | Grounded-by-default + guardrails declarados + red flags no dataset + juiz de segurança + casos adversariais no gate |
| **Federação** | OFF por padrão; manifesto mínimo; HMAC + anti-replay; SSRF guard; fail-closed |

Relatório da última auditoria de segurança (56 achados, todos endereçados): [`docs/security-audit-2026-07-01.md`](docs/security-audit-2026-07-01.md).

---

# Parte VIII — Casos de uso completos

Seis cenários reais usados nos testes de ponta a ponta da plataforma — bons moldes para o seu primeiro projeto:

1. **Aurora — Crédito (demo seedada):** 7 agentes e 2 pipelines para atendimento de crédito bancário: triagem → limite/portabilidade/cheque especial, com RAG de políticas, 45 clientes fictícios e um harness pronto (`aurora-v1`) com baseline ≈ 0,87. Mostra: regras por `decision.*`, gold cases com pesos e red flags.
2. **Órbita — fluxo via UI pura:** criação de agentes/skills/conexões 100% pela interface, provando o contrato da API de mesh. Mostra: o caminho "sem tocar em código".
3. **Hélios — energia solar:** roteamento paralelo + condicional + cadeia + default no mesmo fluxo comercial/técnico. Mostra: composição de todos os tipos de aresta.
4. **Arca — clínica veterinária:** atendimento multi-canal com anexos (fotos de exames) e confirmação de agendamento. Mostra: anexos roteados por `aceita imagens`, modal Executar.
5. **Pulsar — telecom:** triagem N1 com Evidence Policy rigorosa e escalonamento. Mostra: grounded-by-default na prática (e por que selecionar a fonte pelo dropdown importa).
6. **Atlas — service desk federado:** escalonamento N1→N2 entre **duas instâncias** via Federação A2A, com invoke assinado. Mostra: capacidades remotas como entries locais.

---

# Parte IX — Para quem desenvolve a plataforma

## Estrutura do projeto (mapa mental)

```
app/
├── main.py                # FastAPI, lifespan (init_db, reaper, resume de jobs)
├── core/                  # config (settings selados), database (52 tabelas + Repository
│                          #  genérico + migrações idempotentes), auth, metrics, retention
│                          #  (LGPD), invoke_jobs (async 202), attachments, secrets…
├── agents/                # engine (motor 3 camadas + pipelines + dispatcher de anexos),
│                          #  state_machine (FSM), conditional/args suggest (tradutores NL),
│                          #  textnorm, conversation_memory, preflight
├── mcp/runtime.py         # build/execute de tools, per-tool por conector, descoberta,
│                          #  backfill, stdio client
├── skill_parser/          # parser canônico, inputs/decisions schema, linter, validador
├── evidence/              # retriever híbrido, embedder, conversores, chunking
├── verifier/              # LLM-as-Judge (v2 multi-dim) + jobs assíncronos
├── harness/evaluator.py   # avaliação, gate, drift, frases-prova
├── catalog/               # defs de pipeline (subgrafo/selo), conformance
├── workspace/             # binding_schema (forms canônicos, per-tool)
├── a2a/ · federation/     # envelope tipado · peers/manifesto/HMAC
├── routes/ (23 módulos)   # a API — ver inventário na Parte II
├── templates/ (31 páginas)# Jinja2 + Alpine
└── static/js/             # help-content, module-guide, curl_auth…
tests/  (≈250 arquivos; suíte canônica ~5.2k testes) · tests/e2e/ (Playwright)
docs/   (runbooks, configurações, troubleshooting, planos E2E)
infra/  (grafana dashboards)
```

## Convenções que valem lei

- **Docker é o ambiente local**: código é *baked* — toda mudança exige `docker compose build app && docker compose up -d`.
- **Suíte canônica**: `pytest -m "not integration and not e2e"` (o CI roda também integração com Postgres real, ruff **bloqueante**, build docker + smoke).
- **Todo PR**: testes novos/atualizados, bump de `APP_VERSION` em `app/core/version.py` (funcionalidade→MAJOR, melhoria→MEDIUM, fix→MINOR), base `main`.
- **Migrações**: sempre idempotentes (`IF NOT EXISTS`), aplicadas no boot; JSONB exige `json.dumps` (asyncpg não aceita dict cru fora do Repository).
- **UI**: verificar em browser real (Alpine tem armadilhas que teste de template não pega); nunca `{{ }}` literal em `<script>` (Jinja come).
- **Smoke autenticado local**: cookie via `sign_session` num `docker exec` + `curl` (receita em memória do projeto).

---

# FAQ — perguntas que todo iniciante faz

**Preciso saber programar para usar?** Não para operar: skills nascem do Wizard em português, regras têm tradutor e galeria, e o Playground gera o código de integração pronto. Programar ajuda na hora de integrar (Parte V).

**O agente pode "inventar" uma resposta?** O desenho inteiro existe para evitar isso: sem evidência suficiente ele **recusa com próximo passo** (e isso conta como acerto no Harness). A exceção é explícita, por agente.

**Qual a diferença entre Workspace e Playground?** Workspace = conversar como operador (sessões, memória, execução direta de ferramentas). Playground = testar **como o seu sistema** vai consumir (chave real, resposta projetada, código gerado).

**Por que minha imagem "não chegou" ao agente?** Confira os interruptores *aceita documentos/imagens* dos agentes da cadeia — o dispatcher só entrega a quem aceita. E lembre: quem escreve a resposta final pode ser um agente que não recebe anexos.

**Mudei a skill e a API não mudou. Bug?** Não — o pipeline publicado usa o contrato **selado**. A tela avisa o *drift*; republique para atualizar (a versão do contrato incrementa).

**O que acontece se eu editar uma regra que tem Frases-Prova?** O simulador reexecuta na hora; se você tentar publicar com frase reprovando, o gate trava; e o Harness passa a acusar nos próximos runs. É o sistema funcionando.

**Posso usar dois provedores de LLM ao mesmo tempo?** Sim — o Roteamento por tarefa faz exatamente isso (ex.: raciocínio no GPT-OSS 120B interno, visão no GPT-4o, juiz num terceiro).

**Como apago os dados de um cliente (LGPD)?** `POST /api/v1/privacy/forget` com a referência do titular — apaga interações, turnos, verificações, jobs e arquivos, até esgotar.

**Consigo saber quem disparou uma interação ou um erro?** Sim (desde a 42.0.0). Observabilidade, Histórico, Auditoria e Qualidade mostram o **dono/ator** com nome; chamadas por chave de API aparecem com o badge "via chave". Interações antigas sem dono mostram "—" (não se inventa autor).

**Posso conversar com um agente sem sair do desenho do fluxo?** Sim — no Fluxo de agentes, clique no nó e use **"Converse com seu agente"**: é o agente real (com FSM e guardrails), multi-turno, com o estado de cada resposta visível.

**Como testo o roteamento sem gastar tokens?** Botão direito no roteador → **Simular roteamento** → digite uma frase e veja as arestas acenderem. É determinístico (o mesmo motor do publish e do harness), custo zero.

---

## Licença e contato

Proprietário — **Sergio Gaiotto**

**Contato:** sergio.gaiotto@gmail.com · https://falagaiotto.com.br · https://github.com/sergiogaiotto
