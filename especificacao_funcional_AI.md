# Especificação Funcional — Plataforma Multi-Agente Orientada a SKILL.md sobre AI Mesh

## 1. Sumário Executivo

Plataforma de agentes hierárquica, poliárquica em execução e monárquica em governança, na qual cada agente é um processo computacional cuja identidade funcional é definida por um artefato declarativo `SKILL.md`. O `SKILL.md` não é documentação: é o **contrato executável** e a **alma semântica** do agente — carregado em tempo de ativação, interpretado pelo System Prompt Canônico de cada agente, e vinculante sobre quais ferramentas (MCP) podem ser invocadas, em que ordem, sob quais condições e com quais contratos de saída.

A topologia é composta por três camadas verticais de agentes — Orquestrador de Business Domain, Agentes Roteadores, Subagentes — acopladas lateralmente por um protocolo Agent2Agent (A2A) que propaga contexto, estado e telemetria. A plataforma opera sobre um AI Mesh Kubernetes-native, com perímetro de entrada governado por API Gateway e AI Gateway/Model Router, camada de dados e ML integrada (Lakehouse, Feature Store, Vector DB, Model Registry), governança centralizada (IAM, Vault, KMS, OPA, DLP), observabilidade OpenTelemetry, registro de experimentos em LangFuse, e quando implantado acompanhar pelo MLflow e visualização em Grafana.

A plataforma distingue dois planos operacionais complementares: um **plano offline** — responsável por ingestão de dados brutos, anonimização, curadoria de datasets gold adversariais e execução de harnesses de avaliação baseline/regressão — e um **plano online** — o runtime de atendimento onde orquestrador, retriever, policy engine, evidence checker, guardrails e LLM gateway colaboram em tempo real sob supervisão de tracing, versionamento e detecção de drift contínuos.

## 2. Objetivos e Não-Objetivos

### 2.1 Objetivos
- Permitir que unidades de negócio descrevam processos em `SKILL.md` e obtenham agentes executáveis sem a rescrita de código.
- Separar **intenção** (interpretada pelo orquestrador), **roteamento** (decisão de qual processo atender) e **execução** (subagentes com ferramentas).
- Garantir rastreabilidade ponta a ponta: toda decisão de um agente referencia um trecho versionado de um `SKILL.md`.
- Ser agnóstica de modelo: qualquer LLM compatível com prompt estruturado pode encarnar um agente, podendo ser possível escolher previamente OpenAI_API_KEY e chat.maritaca.ai/api (API_KEY).
- Prover camada de dados e ML integrada que alimente agentes com features, embeddings e contexto histórico sem acoplamento direto a fontes operacionais.
- Garantir que todo tráfego norte-sul passe por perímetro de segurança unificado (API Gateway + AI Gateway) antes de atingir o plano de agentes.
- Produzir datasets gold adversariais versionados a partir de dados operacionais anonimizados, servindo como base de avaliação contínua vinculada ao ciclo de release.
- Operar runtime de atendimento baseado em evidência: toda recomendação gerada por agente é verificada contra fontes autorizadas antes da entrega.

### 2.2 Não-Objetivos
- Não substitui sistemas transacionais de registro (ERP, CRM); consome e coordena.
- Não define modelo de UI; expõe API e eventos.
- Não prescreve LLM específico; prescreve contrato.
- Não substitui Lakehouse/Warehouse existente; integra-se como consumidor e produtor de dados.

## 3. Princípios Arquiteturais

1. **SKILL.md é soberano.** Nenhum comportamento de agente pode existir fora do que está declarado em seu `SKILL.md`. Código de infraestrutura carrega, valida e interpreta; não estende.
2. **Separação de planos.** Control plane (catálogo, registro de skills, política) é distinto do data plane (execução de agentes, chamadas MCP, troca A2A).
3. **Determinismo declarativo sobre liberdade generativa.** O LLM decide *como* dentro do espaço que o `SKILL.md` permite; nunca *o quê* fora dele.
4. **Contexto é cidadão de primeira classe.** Propaga-se por envelope tipado, não por concatenação de histórico.
5. **Idempotência e reentrância.** Qualquer subagente pode ser reexecutado com o mesmo envelope e produzir o mesmo efeito observável (ou delegar idempotência ao MCP server).
6. **Falha é telemetria.** Erro não é exceção: é evento estruturado com causa, skill-ref e span OpenTelemetry.
7. **Perímetro único de entrada.** Todo tráfego norte-sul atravessa API Gateway e AI Gateway antes de atingir o serviço de agentes. Nenhum agente é exposto diretamente a clientes externos.
8. **Dados e ML como camada de serviço.** Feature Store, Vector DB e Model Registry são serviços compartilhados consumidos por agentes e model serving; nunca embarcados dentro de um agente.
9. **Offline antes de online.** Nenhum artefato (modelo, prompt, índice, política) atinge o runtime sem antes ser avaliado contra o dataset gold adversarial vigente. O plano offline é pré-requisito do online, não complemento opcional.
10. **Evidência sobre geração livre.** Toda recomendação produzida pelo runtime deve citar evidências extraídas de bases autorizadas e verificadas por checker independente. Geração sem evidência resulta em recusa controlada, não em resposta especulativa.
11. **Recusa controlada é comportamento correto.** Quando evidência é insuficiente, conflitante, ou a política proíbe, o sistema emite recusa estruturada com próximo passo (escalar, solicitar dado adicional) — nunca silencia, nunca fabrica.

## 4. Topologia de Agentes

### 4.1 Agente Orquestrador de Business Domain (AOBD)

Entidade única por domínio de negócio (Financeiro, Suprimentos, RH, Operações Clínicas, etc.). Responsabilidades:

- **Interpretação de intenção.** Recebe texto natural do requisitante (humano, sistema upstream, evento de barramento) e produz um `IntentDescriptor` estruturado: `{domain, process_candidate, entities, constraints, urgency, actor}`.
- **Consulta ao Catálogo de Agentes Roteadores (CAR).** Executa matching semântico entre `IntentDescriptor` e o manifesto de cada roteador registrado no domínio.
- **Delegação com envelope.** Emite um `DelegationEnvelope` assinado ao roteador eleito, contendo intenção, contexto inicial, limites de orçamento (tokens, tempo, custo), e SLA.
- **Supervisão de alto nível.** Não executa o processo; observa progresso via eventos A2A e aplica políticas de escalonamento (timeout, abort, reroute).
- **Desambiguação.** Quando a intenção é plurivalente, retorna ao requisitante um conjunto reduzido de hipóteses discretas — não narrativa. Se operado headless, aplica política de tiebreak determinística do `SKILL.md` do próprio AOBD.

O AOBD tem seu próprio `SKILL.md` de segundo nível, descrevendo *como orquestrar*, não *como executar*. Esse skill define: critérios de matching, política de tiebreak, limites de delegação, e comportamento de fallback.

### 4.2 Agente Roteador (AR)

Representa um **processo de negócio discreto** (ex: "Conciliação Fiscal Mensal", "Onboarding de Fornecedor Estrangeiro", "Triagem de Queixa Clínica"). Responsabilidades:

- **Hidratação de Skill.** Ao receber o envelope, carrega o `SKILL.md` correspondente ao processo e o valida contra o schema vigente.
- **Planejamento.** Decompõe o processo em uma sequência (ou DAG) de tarefas executáveis. O DAG é derivado da seção `## Workflow` do `SKILL.md`, não gerado livremente.
- **Ativação de subagentes.** Para cada nó do DAG, resolve o subagente apropriado consultando o subinventário declarado no `SKILL.md` e dispara A2A.
- **Agregação e verificação.** Coleta saídas dos subagentes, valida contra contratos de saída declarados, consolida em um `ProcessOutcome`.
- **Compensação.** Em caso de falha parcial, executa passos de compensação declarados no `SKILL.md` (padrão Saga).

### 4.3 Subagente (SA)

Unidade atômica de execução. Cada subagente corresponde a uma **tarefa única e bem delimitada** (ex: "Extrair dados de NF-e", "Validar CNPJ na Receita", "Calcular retenção de IRRF", "Gerar memorando DOCX"). Responsabilidades:

- **Carregar SKILL.md da tarefa.** Torna-se seu system prompt efetivo.
- **Inspecionar inventário de ferramentas.** Cruza tools disponíveis (servidores MCP conectados) com tools permitidas pelo `SKILL.md`.
- **Executar.** Invoca tools na ordem e sob as condições prescritas. O LLM tem latitude dentro das fronteiras; fora delas, recusa.
- **Emitir resultado tipado.** Saída conforme schema declarado em `## Output Contract` do SKILL.md.

Subagentes são **stateless entre invocações**. Todo estado necessário vem no envelope A2A.

## 5. SKILL.md — Anatomia Canônica

Todo `SKILL.md` da plataforma segue seções obrigatórias e opcionais. Seções obrigatórias formam o contrato mínimo; seções opcionais habilitam capacidades adicionais.

### 5.1 Seções Obrigatórias

```
---
id: urn:skill:<domain>:<process>:<task>
version: semver
kind: orchestrator | router | subagent
owner: <equipe>
stability: alpha | beta | stable | deprecated
---

# <Nome Humano>

## Purpose
Declaração imperativa de uma linha do que este agente faz e do que NÃO faz.

## Activation Criteria
Condições sob as quais este skill deve ser selecionado. Para roteadores,
padrões de intenção; para subagentes, pré-condições do envelope.

## Inputs
Schema tipado (JSON Schema referenciado) do envelope esperado.

## Workflow
Sequência ou DAG de passos. Para subagentes é tipicamente linear;
para roteadores é onde vive a lógica de composição.

## Tool Bindings
Lista declarativa de tools MCP permitidas, com:
- nome lógico
- servidor MCP origem
- condições de uso (when)
- limites (rate, custo máximo, timeout)
- política de retry

## Output Contract
Schema tipado da saída. Saídas fora do schema são rejeitadas pela camada de validação antes de A2A.

## Failure Modes
Enumeração de falhas reconhecidas e ação prescrita (retry, compensar, escalar, abortar).
```

### 5.2 Seções Opcionais

- `## Delegations` — para roteadores: mapa de tarefa → subagente.
- `## Compensation` — passos Saga reversos.
- `## Guardrails` — políticas de conteúdo, PII, jurisdição.
- `## Budget` — teto de tokens, tempo parede, custo em USD.
- `## Examples` — pares entrada/saída usados em avaliação contínua.
- `## Telemetry` — métricas customizadas a emitir.
- `## Data Dependencies` — referências a Feature Store, Vector DB ou Lakehouse requeridas pelo agente para execução, com schemas esperados e políticas de cache.
- `## Model Constraints` — quando o agente requer modelo específico ou classe de modelo (ex: exigência de context window mínimo, suporte a function calling), declara-se aqui em vez de no `AgentBinding`, permitindo validação em tempo de ativação.
- `## Evidence Policy` — para skills que operam no runtime de atendimento: declara bases autorizadas de conhecimento (`KNOWLEDGE_SOURCE` refs), threshold mínimo de relevância para citação, número mínimo de evidências para recomendação, e comportamento de recusa quando evidência é insuficiente.
- `## Gold Refs` — referências ao dataset gold adversarial contra o qual este skill deve ser avaliado antes de promoção, com métricas de aceitação mínima.

### 5.3 Ciclo de Vida do SKILL.md

- Versionado em repositório Git (`skills/<domain>/<kind>/<slug>.md`).
- Validado por linter dedicado no CI: schema, referências a tools existentes, contratos compatíveis com pais e filhos.
- Publicado no **Skill Registry** (OCI-compatível ou etcd) com imutabilidade por hash.
- Carregado em runtime por referência `urn:skill:...@version`. Pinagem obrigatória em produção; `latest` permitido apenas em ambientes de desenvolvimento.

## 6. Catálogo de Agentes Roteadores (CAR)

Índice consultável pelo AOBD. Cada entrada contém:

- URN do skill roteador.
- `Activation Criteria` projetado em embedding vetorial para matching semântico.
- Sinais estruturados: palavras-chave, entidades requeridas, perfil do ator, janela de horário, jurisdição.
- Métricas operacionais correntes: taxa de sucesso, latência p95, custo médio.
- Estado: ativo, canário, deprecated, em manutenção.

O matching no AOBD é híbrido: filtro simbólico sobre sinais estruturados seguido de ranking vetorial. Empates são resolvidos por: skill com maior `stability`, menor custo médio, maior taxa de sucesso recente, nesta ordem.

O CAR é o análogo do service registry em microsserviços, porém indexado por intenção, não por endpoint.

O ranking vetorial do CAR consome embeddings do **Vector DB** (§11). Os `Activation Criteria` de cada roteador são indexados como documentos vetoriais no momento da publicação do skill; o AOBD projeta o `IntentDescriptor` no mesmo espaço e executa busca por similaridade como primeiro passo do matching híbrido.

## 7. Protocolo Agent2Agent (A2A)

### 7.1 Envelope

Unidade de comunicação entre agentes. Campos:

- `envelope_id` — ULID.
- `trace_id`, `span_id`, `parent_span_id` — OpenTelemetry.
- `origin` — URN do agente emissor, versão.
- `target` — URN do agente destino, versão.
- `intent` — `IntentDescriptor` original preservado em toda a cadeia.
- `skill_ref` — skill que o destinatário deve carregar.
- `context` — dicionário tipado de fatos acumulados (resultados de subagentes anteriores, entidades resolvidas).
- `state` — ponteiro para snapshot persistido (ver §8).
- `budget_remaining` — `{tokens, wall_ms, usd}`.
- `deadline` — timestamp absoluto.
- `auth` — token de delegação curto (SPIFFE/JWT) com escopo limitado ao skill alvo.
- `signature` — assinatura do emissor.

### 7.2 Transporte

- **Síncrono:** gRPC para caminhos críticos com deadline < 30s.
- **Assíncrono:** barramento de eventos (NATS JetStream ou Kafka) para fluxos Saga e workloads long-running.
- **Streaming:** server-sent events dentro de uma stream gRPC para progresso incremental de subagentes.

Protocolo é agnóstico de transporte; a seleção é política do AR, declarada no `SKILL.md`.

### 7.3 Semântica de Entrega

- Pelo menos uma vez em barramento assíncrono, com deduplicação no destinatário por `envelope_id`.
- Exatamente uma vez efetiva via idempotency key em tools MCP que declaram suporte.

## 8. Gestão de Contexto e Estado

Contexto e estado são distintos:

- **Contexto** é a informação semântica necessária para a próxima decisão. Viaja no envelope, tamanho-limitado, comprimido (LLM-lingua, sumarização estruturada) quando excede o limite.
- **Estado** é a memória durável do processo. Persistido em **Context Store** (Redis para quente, PostgreSQL + object storage para frio). O envelope carrega apenas ponteiros e um snapshot mínimo hot.

O AOBD abre um `ProcessContext` ao receber a requisição. Todo agente descendente herda e anexa; ninguém sobrescreve. Mutações são append-only com vetor de versão para conflito em execuções paralelas.

Quando um subagente termina, emite um `ContextDelta` — mudanças explícitas em lugar do contexto inteiro — reduzindo banda e ambiguidade.

### 8.1 Cache e Context Stores — Topologia

O bloco **Cache** do AI Mesh materializa-se em três camadas distintas dentro do cluster Kubernetes:

1. **Prompt Cache.** Cache de prefixos de prompt e respostas determinísticas. Implementado em Redis com TTL curto (minutos). Evita chamadas repetidas ao model serving para prompts idênticos. Chave: hash do prompt efetivo (SKILL.md + envelope reduzido). Invalidação automática na publicação de nova versão do skill.

2. **Context Store quente.** Redis com persistência AOF. Armazena `ProcessContext` ativo e snapshots de envelope para processos em andamento. TTL alinhado ao `deadline` do envelope + margem de compensação. Acessado por todos os agentes da cadeia via ponteiro `state` no envelope.

3. **Context Store frio.** PostgreSQL + object storage (S3-compat). Recebe contextos finalizados para auditoria, replay e treinamento. Retenção governada por política jurisdicional declarada no `SKILL.md` do AOBD.

O serviço de agentes (`AgentSvc`) acessa cache e context stores diretamente, sem passar pelo service mesh para latência mínima. Sidecars capturam telemetria dessas conexões, mas não intermediam payload.

## 9. Pipeline Offline — Preparação de Dados e Avaliação

O plano offline é a linha de produção de artefatos que o runtime consome: datasets gold, baselines, índices validados, políticas testadas. Nada chega ao runtime sem aprovação neste plano.

### 9.1 Ingestão e ETL

Fontes primárias: gravações e transcrições de interações, metadados de CRM/URA/WFM, documentos de conhecimento internos, bases regulatórias. O pipeline de ingestão normaliza formatos, resolve encoding, e aplica deduplicação por hash de conteúdo.

Dados ingeridos são armazenados no Lakehouse em camada raw, particionados por fonte, data e tenant. ETL subsequente projeta dados em camada curated com schema tipado e controle de qualidade.

### 9.2 Anonimização e Risco Residual

Todo dado que contenha informação de pessoas naturais ou jurídicas passa por pipeline de anonimização antes de qualquer uso downstream:

- **Detecção de PII.** NER especializado identifica nomes, CPFs, CNPJs, endereços, telefones, dados financeiros, dados de saúde. Modelos de detecção são versionados no Model Registry.
- **Redação e pseudonimização.** PII detectado é substituído por tokens pseudônimos consistentes (mesmo indivíduo recebe mesmo pseudônimo dentro de um dataset para preservar relações sem expor identidade). Dados de saúde e financeiros são redatados integralmente.
- **Avaliação de risco residual.** Após anonimização, classificador de risco residual avalia probabilidade de reidentificação por combinação de quasi-identificadores. Datasets com risco acima de threshold configurável são bloqueados e requerem revisão manual antes de prosseguir.

O DLP (§12.5) opera em conjunto com este pipeline; a classificação de sensibilidade alimenta o catálogo de metadados.

### 9.3 Rotulagem e Dicionário

Dados anonimizados são rotulados em duas dimensões:

- **Rotulagem funcional.** Classificação por jornada do cliente, motivo de contato, canal, resolução, sentimento, complexidade. Esquema de rótulos é mantido em dicionário versionado no repositório Git.
- **Rotulagem adversarial.** Curadoria deliberada de casos de borda: ambiguidades, conflitos de política, informação insuficiente, tentativas de prompt injection, pedidos fora de escopo. Esses casos compõem a fração adversarial do dataset gold.

Rotulagem pode ser manual (especialistas de domínio), semi-automática (LLM propõe, humano valida), ou automática (classificadores de alta confiança validados previamente no harness).

### 9.4 Dataset Gold Adversarial

O dataset gold é o artefato central do plano offline. Características:

- **Versionado.** Cada versão (`gold adversarial vN`) é imutável, assinada e registrada no Catálogo de Metadados com lineage completa até as fontes raw.
- **Estratificado.** Amostragem estratificada por jornada, canal, complexidade, e tipo de caso (normal vs adversarial). Proporção mínima de casos adversariais definida por política.
- **Reproduzível.** Seed de amostragem, parâmetros de anonimização, e versão do dicionário são registrados no dataset. Reprodução bit-exata é possível a partir dos mesmos inputs.
- **Pareado com ground truth.** Cada caso no gold tem resposta esperada (gerada por especialista de domínio e revisada por par).

O dataset gold alimenta diretamente o harness de avaliação e, indiretamente, o gate de release.

### 9.5 Harness de Avaliação Baseline/Regressão

Motor de avaliação que executa skills contra o dataset gold e produz relatórios quantitativos. Componentes:

- **Baseline.** Primeira execução de um skill contra o gold vigente. Estabelece métricas de referência: acurácia, cobertura de evidência, taxa de recusa correta, taxa de falso positivo, latência, custo.
- **Regressão.** Toda nova versão de skill, modelo, prompt, índice ou política é executada contra o mesmo gold. Diferenças em relação ao baseline são computadas e reportadas.
- **Relatórios.** Métricas agregadas e por estrato. Breakdown por tipo de caso (normal vs adversarial). Comparação side-by-side entre versões.
- **Gate de release.** Resultado do harness alimenta decisão binária: aprovado (release para runtime) ou reprovado (retorno para correções + inclusão de novos casos no gold). Gate é automatizado com thresholds configuráveis; regressão acima de N% em qualquer métrica crítica bloqueia release automaticamente.

O harness é executado pelo orquestrador de workflows (Argo/Kubeflow) e registra resultados no MLflow e no Version Registry (§16).

### 9.6 Ciclo de Feedback Offline → Online

Quando o gate reprova uma release:

1. Casos de falha do harness são exportados como candidatos a novos casos gold.
2. Equipe de domínio revisa, rotula ground truth, e publica nova versão do gold.
3. Correções no skill/modelo/prompt são aplicadas e reavaliadas contra o gold atualizado.
4. Ciclo repete até aprovação.

Dados de produção (interações reais pós-release) alimentam o pipeline de ingestão para enriquecer futuras versões do gold — fechando o loop offline-online.

## 10. Integração MCP e Inventário de Ferramentas

### 10.1 Descoberta

Servidores MCP são registrados no **Tool Registry**, com metadados: operações expostas, schemas de entrada/saída, SLA, custo por chamada, requisitos de autenticação, jurisdição de dados.

O Tool Registry é **governado**: toda adição ou modificação de MCP server requer aprovação pelo owner do domínio e validação de conformidade com policies OPA. O registro inclui classificação de sensibilidade das operações (herdada do DLP via Catálogo de Metadados), e flag `requires_trusted_context` para operações que manipulam dados sensíveis ou executam ações irreversíveis.

### 10.2 Vinculação

Na inicialização do subagente:

1. Carrega `SKILL.md`.
2. Resolve cada entrada de `## Tool Bindings` contra o Tool Registry.
3. Constrói o **Permitted Toolset** — subconjunto do inventário global exposto ao LLM.
4. Tools fora do Permitted Toolset são invisíveis para o LLM; mesmo se alucinadas, a camada de execução recusa.

### 10.3 Política de Uso

O `SKILL.md` pode declarar `when` em cada binding — expressão sobre o contexto que condiciona exposição da tool. Ex: uma tool de débito financeiro só é exposta quando `context.approval.status == "granted"`.

A **Policy Engine** (OPA) é consultada em tempo real pelo orquestrador antes de expor tools ao LLM. A policy engine avalia: perfil do ator, escopo da jornada, ferramentas permitidas para a combinação skill+ator+contexto, e limites de uso. O resultado é um conjunto tipado de permissões que restringe o Permitted Toolset dinamicamente — o skill declara o máximo; a policy engine pode reduzir, nunca ampliar.

### 10.4 Observação

Toda chamada MCP produz span OpenTelemetry com: tool, servidor, latência, custo, resultado, skill-ref que autorizou.

### 10.5 MCP Servers e Camada de Dados

Os MCP servers atuam como adapters entre agentes e fontes de dados heterogêneas. Servidores MCP são o ponto de acesso padronizado ao Lakehouse/Warehouse. Nenhum agente ou subagente acessa o data lake diretamente; todo acesso é mediado por tool MCP registrada, garantindo rastreabilidade, controle de acesso e auditoria por span.

O service mesh (sidecars) aplica mTLS e routing policies às conexões entre `AgentSvc` e `ToolServers`, assegurando que a comunicação agente→tool obedece às mesmas políticas de segurança que a comunicação A2A.

## 11. AI Mesh — Infraestrutura

### 11.1 Perímetro: Tráfego Norte-Sul

Todo tráfego externo (apps clientes, usuários, sistemas upstream) atravessa um pipeline de entrada de três estágios antes de atingir o plano de agentes:

**API Gateway.** Ponto de entrada único para todo tráfego norte-sul. Responsabilidades: terminação TLS, autenticação de chamadores (OAuth2/OIDC), rate limiting por tenant, validação de payload, roteamento para o AI Gateway. Implementações de referência: Kong, Envoy Gateway, AWS API Gateway (quando em cloud pública). O API Gateway injeta headers canônicos (`X-Tenant-ID`, `X-Actor-ID`, `X-Trace-ID`) consumidos por toda a cadeia downstream.

**AI Gateway / Model Router (LLM Gateway).** Camada intermediária entre o API Gateway e o serviço de agentes. Responsabilidades:

- **Roteamento de modelo.** Direciona requisições de inferência ao backend de model serving apropriado com base em: modelo solicitado pelo `AgentBinding`, política de fallback (se modelo primário estiver indisponível), e restrições de custo/latência do envelope.
- **Governança de inferência.** Aplica políticas OPA antes de permitir que uma requisição atinja o model serving: verificação de budget restante, validação de guardrails do skill, filtragem de conteúdo.
- **Abstração de provider.** Permite que a plataforma opere com modelos self-hosted (via KServe/vLLM) e APIs externas (OpenAI, Maritaca) sob interface unificada. O agente nunca sabe se o modelo é local ou remoto; o AI Gateway resolve.
- **Cache de inferência.** Intercepta prompts repetidos e retorna respostas cacheadas do Prompt Cache (§8.1) quando aplicável, evitando custo e latência de inferência redundante.
- **Guardrails e Security Filters.** Filtros de segurança pré e pós-inferência integrados ao LLM Gateway. Pré-inferência: detecção de prompt injection, validação de conteúdo do contexto, verificação de classificação DLP. Pós-inferência: detecção de alucinação por heurística (conflito com evidência fornecida), filtragem de PII em saída, verificação de guardrails declarados no `SKILL.md`. Filtragem pós-inferência precede entrega ao orquestrador — respostas que violam filtros são descartadas e geram retry ou recusa.
- **Telemetria.** Emite métricas de uso de modelo por skill, por tenant, por provider — alimentando dashboards de custo e capacity planning.

Implementações de referência: LiteLLM Proxy, Portkey, custom Envoy filter chain com plugin de roteamento.

O IAM autentica no API Gateway e autoriza no AI Gateway. OPA avalia policies em ambos os pontos, com granularidade diferente: no API Gateway, policies de acesso (quem pode chamar); no AI Gateway, policies de uso (quais modelos, quais skills, qual budget).

### 11.2 Kubernetes como Substrato

Cada tipo de agente é um workload dedicado:

- **AOBD** — Deployment, singleton por domínio, HPA por QPS.
- **AR** — Deployment por roteador, HPA por profundidade de fila.
- **SA** — Job ou KEDA-scaled Deployment; subagentes ofensivos em custo rodam como Jobs efêmeros, subagentes de alta frequência como pool quente.

CRDs dedicados:

- `Skill` — referência imutável a um `SKILL.md` versionado.
- `AgentBinding` — associa um Skill a um pod template, a um conjunto de MCP servers, e ao backend de model serving a utilizar.
- `DomainTopology` — declara o AOBD e os AR ativos do domínio.

O **Serviço de Agentes** (`AgentSvc`) é o workload Kubernetes central que hospeda a execução dos três tipos de agente (AOBD, AR, SA). Internamente, o AgentSvc implementa o loop de vida: receber envelope A2A → carregar skill → montar prompt → chamar model serving → executar tool bindings → emitir resultado. Cada instância do AgentSvc é um pod stateless; todo estado reside no Context Store.

### 11.3 Orquestração de Workflows

Para processos que exigem execução de DAGs complexos, long-running ou com dependências temporais (agendamentos, esperas por evento externo, retries com backoff), a plataforma delega a coordenação a um **orquestrador de workflows externo**: Kubeflow Pipelines, Argo Workflows ou Apache Airflow.

O orquestrador de workflows não substitui o Agente Roteador — complementa-o. A divisão de responsabilidades:

- **AR** decide *o quê* executar (decomposição do processo em tarefas, baseada no `SKILL.md`) e *com qual* subagente.
- **Orquestrador de workflows** executa *quando* e *em que ordem* os subagentes são ativados no plano de infraestrutura, aplicando retry, paralelismo, dependências de dados, e gates de aprovação.

O AR emite o DAG como artefato estruturado; o orquestrador de workflows o materializa em um pipeline executável. Cada nó do pipeline é uma chamada ao `AgentSvc` com o envelope A2A do subagente correspondente.

O orquestrador de workflows também coordena tarefas de ML ops: retreino de modelos registrados no Model Registry, atualização de features no Feature Store, pipelines de avaliação contínua de skills (§17), e execução do harness de avaliação offline (§9.5).

### 11.4 Model Serving

Camada dedicada de inferência de modelos, desacoplada do serviço de agentes. Implementações de referência: KServe, Seldon Core, BentoML, vLLM.

Responsabilidades:

- **Servir modelos self-hosted.** Modelos open-weight (LLaMA, Mistral, Sabiá) deployados como InferenceServices Kubernetes com autoscaling por GPU.
- **Gerenciar ciclo de vida de modelos.** Cada InferenceService referencia uma versão no Model Registry. Promoção canária de modelos segue o mesmo fluxo gated de promoção de skills.
- **Consumir features.** O model serving acessa o Feature Store para enriquecer inferência com features pré-computadas quando o modelo requer (ex: modelos fine-tuned que esperam feature vectors como parte do input).
- **Emitir métricas.** Latência de inferência, throughput, utilização de GPU, taxa de erro — exportadas para Prometheus via exporter nativo.

O AI Gateway roteia chamadas de inferência ao model serving; o AgentSvc nunca se conecta diretamente a um pod de modelo. Essa indireção permite load balancing, A/B testing de modelos, e fallback transparente para APIs externas.

### 11.5 Topologia de Dados no Cluster

O cluster Kubernetes hospeda três categorias de stores persistentes que servem o runtime online:

- **PostgreSQL + pgvector.** Store relacional e vetorial combinado. Usado para: persistência do Context Store frio (§8.1), armazenamento do modelo de dados de domínio (§15 — INTERACTION, TURN, EVIDENCE, RELEASE, etc.), índice vetorial quando a escala permite pgvector como Vector DB primário ou secundário.
- **Object Storage (S3-compat).** Armazenamento de blobs: contextos serializados, artefatos de avaliação, datasets gold, gravações/transcrições ingeridas, snapshots de índices. Acessado pelo orquestrador e pelo pipeline offline.
- **Vector DB dedicado (opcional).** Qdrant, Weaviate ou similar quando pgvector não atende à escala ou latência exigidas pelo Retriever (§14). Coexiste com pgvector; a escolha é por domínio/skill, declarada em `## Data Dependencies`.

### 11.6 Service Mesh

Istio ou Linkerd provê mTLS, retry, circuit breaking entre agentes. Autorização por SPIFFE ID emitido por SPIRE; cada pod recebe identidade atrelada ao seu skill-ref.

Os proxies sidecar são injetados em todos os workloads do cluster (AgentSvc, ToolServers, Model Serving). Funções:

- **mTLS transparente.** Toda comunicação intra-cluster é cifrada sem mudança em código de aplicação.
- **Routing policies.** Regras de tráfego (canary, mirror, fault injection) aplicadas por sidecar, configuradas via CRDs do mesh.
- **Telemetria passiva.** Sidecars exportam spans e métricas de rede ao OpenTelemetry Collector automaticamente, sem instrumentação explícita no AgentSvc.

### 11.7 Isolamento

Subagentes que manipulam dados sensíveis rodam em namespaces dedicados com NetworkPolicy restritiva, só podendo alcançar servidores MCP declarados em seu `SKILL.md`. Violação de política é falha dura, não degradação.

### 11.8 Multi-Tenancy

Tenancy é projetada em labels + RBAC + NetworkPolicy, com Skill Registry particionado por tenant e possibilidade de skills compartilhados via federação explícita.

## 12. Camada de Dados e ML

### 12.1 Lakehouse / Data Warehouse

Repositório central de dados estruturados e semi-estruturados consumidos pela plataforma. Os MCP servers são o único ponto de acesso padronizado ao Lakehouse; agentes nunca consultam tabelas diretamente.

O Lakehouse armazena: dados operacionais ingeridos de ERPs/CRMs, resultados de processos finalizados (para analytics e retreino), logs de decisão de agentes (após exportação do log imutável de auditoria), datasets de avaliação derivados dos `## Examples` de skills, e os datasets gold adversariais produzidos pelo pipeline offline (§9).

Implementações de referência: Delta Lake, Apache Iceberg sobre object storage S3-compat.

### 12.2 Catálogo e Metadados

Registro central de todos os ativos de dados disponíveis na plataforma. Cada dataset no Lakehouse, cada feature no Feature Store, e cada modelo no Model Registry possui entrada no catálogo com: schema, owner, linhagem (lineage), classificação de sensibilidade (alimentada pelo DLP — §13.5), e políticas de acesso.

O catálogo é consultado pelo Tool Registry na validação de MCP servers que expõem acesso a dados: o MCP server declara quais datasets consome; o catálogo valida que o skill requisitante tem permissão para aqueles datasets via políticas OPA.

O catálogo também indexa as **Bases Autorizadas de Conhecimento** (`KNOWLEDGE_SOURCE`) consumidas pelo Retriever (§14). Cada base tem: versão do índice, data de última atualização, classificação de confidencialidade, e flag de autorização para uso em recomendações ao cliente final.

Implementações de referência: DataHub, OpenMetadata, Apache Atlas.

### 12.3 Feature Store

Serviço de features pré-computadas consumidas por model serving e diretamente por agentes (quando o `SKILL.md` declara `## Data Dependencies` que referenciam features).

Dois planos:

- **Offline store.** Features batch computadas por pipelines do orquestrador de workflows, armazenadas no Lakehouse. Usadas em retreino e avaliação.
- **Online store.** Features materializadas em Redis/DynamoDB para inferência de baixa latência. O model serving consome do online store; agentes que precisam de features como contexto para decisão (ex: score de risco de fornecedor) acessam via MCP server dedicado.

Implementações de referência: Feast, Tecton, Vertex AI Feature Store.

### 12.4 Model Registry

Registro versionado de todos os modelos utilizáveis pela plataforma: modelos self-hosted (weights, configs, métricas de avaliação) e referências a modelos externos (API key reference no Vault, endpoint, versão).

O `AgentBinding` CRD referencia um modelo no Model Registry por URN + versão. O AI Gateway resolve essa referência para o endpoint concreto de serving. O Model Registry integra-se com MLflow para rastreabilidade de experimentos: cada modelo publicado tem link para o experimento que o produziu.

O orquestrador de workflows acessa o Model Registry para pipelines de retreino e promoção canária de modelos.

### 12.5 Vector DB

Armazenamento de embeddings vetoriais para busca por similaridade semântica. Consumidores:

- **CAR (§6).** Embeddings dos `Activation Criteria` de roteadores, consultados pelo AOBD no matching de intenção.
- **Retriever (§14).** Índices vetoriais das bases autorizadas de conhecimento, consultados pelo Retriever+Reranker no runtime de atendimento.
- **Subagentes com RAG.** Subagentes cujo `SKILL.md` declara dependência de base de conhecimento vetorial acessam o Vector DB via MCP server dedicado, com filtros de tenant e classificação de sensibilidade.
- **Catálogo de Metadados.** Busca semântica sobre descrições de datasets e features.

O AgentSvc conecta-se ao Vector DB diretamente (não via MCP) para o matching do CAR, dado que é operação interna da plataforma, não uma tool de domínio. Para consumo por subagentes e pelo Retriever, o acesso é via MCP server com Permitted Toolset.

Implementações de referência: Qdrant, Weaviate, pgvector (quando escala permite).

## 13. Segurança e Governança

### 13.1 IAM — Identidade e Acesso

Serviço central de identidade que autentica e autoriza todos os atores da plataforma: usuários humanos, sistemas upstream, e agentes internos.

- **Autenticação de entrada.** O API Gateway valida tokens OAuth2/OIDC emitidos pelo IAM. Todo request externo carrega identidade verificada antes de atingir o AI Gateway.
- **Autorização de agentes.** O IAM emite SPIFFE IDs via SPIRE para cada pod de agente. O AI Gateway e o AgentSvc validam identidade do agente antes de aceitar envelopes A2A. Tokens de delegação curta (§7.1, campo `auth`) são emitidos pelo IAM com escopo mínimo: skill alvo + tools declaradas + deadline.
- **Integração com OPA.** IAM fornece claims de identidade que OPA consome para avaliar policies de acesso e uso.

### 13.2 Secrets Manager (Vault)

Gerenciamento centralizado de segredos: API keys de provedores de modelo (OpenAI, Maritaca), credenciais de MCP servers, certificados TLS, tokens de integração com sistemas externos.

- Agentes nunca recebem segredos diretamente. O AgentSvc obtém segredos do Vault em tempo de inicialização do subagente, com escopo limitado às tools declaradas no `SKILL.md`.
- O Vault é o único consumidor do KMS/HSM para operações criptográficas. Chaves de assinatura de skills, chaves de cifração de contexto em trânsito, e chaves de envelope A2A são gerenciadas pelo Vault com backend KMS.
- Rotação automática de segredos com notificação ao AgentSvc para reload sem restart.

### 13.3 KMS / HSM

Serviço de gerenciamento de chaves criptográficas. O Vault consome o KMS para todas as operações de cifração e assinatura. Em ambientes com requisitos de compliance rigoroso (PCI-DSS, LGPD avançado), o KMS é backed por HSM físico ou cloud HSM.

Chaves gerenciadas: assinatura de `SKILL.md` (Sigstore/cosign), cifração de campos sensíveis no envelope A2A, cifração de contexto em repouso no Context Store frio.

### 13.4 Políticas as Code (OPA) — Policy Engine

Open Policy Agent materializa a **Policy Engine** do runtime. Avalia policies declarativas em três pontos de enforcement:

- **AI Gateway.** Policies de uso: qual tenant pode usar qual modelo, budget máximo por requisição, guardrails de conteúdo, restrições jurisdicionais. Policies são carregadas do repositório Git e sincronizadas via OPA bundles.
- **AgentSvc / Orquestrador.** Policies de execução: validação de que o skill referenciado é compatível com o ator (RBAC fine-grained), verificação de que tools requisitadas são permitidas pelo `SKILL.md` e pelas policies de tenant, enforcement de `requires_trusted_context` em tools sensíveis. No runtime de atendimento, o orquestrador consulta a policy engine antes de cada etapa da máquina de estados (§15), recebendo de volta permissões, limites e conjunto de ferramentas permitidas para o contexto corrente.
- **Tool Registry.** Policies de registro: validação de conformidade de novos MCP servers antes de admissão no inventário.

Policies são versionadas no mesmo repositório Git dos skills, revisadas com o mesmo rigor, e rastreadas com o mesmo pipeline CI. Toda versão de policy é registrada no Version Registry (§16) e avaliada no harness offline antes de promoção.

### 13.5 DLP / Classificação de Dados

Serviço de Data Loss Prevention e classificação automática de dados. Integra-se em três pontos:

- **Catálogo de Metadados.** Classifica datasets por sensibilidade (público, interno, confidencial, restrito). A classificação alimenta policies OPA que controlam quais skills podem acessar quais datasets via MCP.
- **Pipeline de contexto.** Conteúdo ingerido de fontes externas (documentos do requisitante, respostas de APIs) passa por scanner DLP antes de ser incorporado ao contexto do envelope. Conteúdo com PII identificado é marcado no contexto; skills com `## Guardrails` que proíbem PII recusam processamento.
- **Pipeline offline (§9.2).** Anonimização de dados operacionais antes de inclusão em datasets gold. DLP classifica campos e alimenta o detector de PII.

### 13.6 Princípios Transversais de Segurança

- **Princípio do menor privilégio por skill.** Tokens de delegação A2A são emitidos com escopo mínimo — apenas o skill alvo, apenas as tools declaradas, apenas o tempo até o `deadline`.
- **Assinatura de skills.** `SKILL.md` publicado no registro é assinado (Sigstore/cosign). Runtime rejeita skill não assinado em produção. Chaves de assinatura gerenciadas pelo Vault com backend KMS.
- **Revisão de skills.** Mudanças em `SKILL.md` de produção exigem revisão por owner do domínio + comitê de risco quando `Tool Bindings` ou `Guardrails` mudam.
- **Auditoria.** Todo envelope, toda chamada MCP, toda decisão de roteamento é persistida em log imutável (append-only, WORM). Retenção por jurisdição.
- **Prompt injection.** Conteúdo externo ingerido é marcado como `untrusted` no contexto; tools sensíveis declaram `requires_trusted_context: true` em seus bindings e são bloqueadas se o contexto contém origem untrusted sem passar por subagente de saneamento. O DLP reforça essa marcação com classificação automatizada. Os guardrails/security filters no LLM Gateway (§11.1) adicionam camada de defesa em profundidade.

## 14. Runtime de Evidência — Retriever, Reranker e Evidence Checker

O runtime de atendimento opera sob o princípio de que toda recomendação deve ser ancorada em evidência extraída de bases autorizadas. Esta seção detalha os componentes especializados que implementam esse princípio.

### 14.1 Retriever + Reranker

Componente responsável pela busca de evidência em bases autorizadas de conhecimento. Posicionado entre o orquestrador e as bases, o Retriever implementa busca híbrida:

- **Busca lexical.** BM25 ou similar sobre índice invertido para matching exato de termos, códigos, identificadores.
- **Busca vetorial.** Embedding da query projetado no espaço vetorial do índice da base, com busca por similaridade (cosine, dot product).
- **Busca híbrida.** Fusão de scores lexical e vetorial com pesos configuráveis por skill (declarados em `## Evidence Policy`).

O **Reranker** aplica modelo cross-encoder nos top-N resultados da busca híbrida, reordenando por relevância contextual com a query e o contexto da interação. O resultado é um conjunto ranqueado de trechos (`EVIDENCE`) com scores de relevância e metadados de confidencialidade herdados da `KNOWLEDGE_SOURCE` de origem.

Fontes consultadas: bases internas (manuais, procedimentos, normas, FAQs), bases regulatórias, bases contratuais — todas registradas no Catálogo de Metadados com classificação de confidencialidade. O Retriever só consulta bases declaradas no `## Evidence Policy` do skill ativo.

### 14.2 Evidence Checker (Verificador de Evidência)

Componente independente do LLM gerador. Recebe o rascunho de recomendação produzido pelo LLM e as evidências que o sustentam. Executa verificações:

- **Consistência.** O rascunho é semanticamente consistente com as evidências citadas? Contradições são sinalizadas.
- **Regras de negócio.** O rascunho viola alguma regra de negócio declarada no `SKILL.md` ou em policies OPA?
- **Conflito entre fontes.** Duas ou mais evidências citadas são mutuamente contraditórias?
- **Cobertura.** As evidências cobrem todas as afirmações do rascunho? Claims não cobertos são sinalizados.
- **Incerteza calibrada.** O evidence checker produz score de confiança calibrado. Scores abaixo de threshold resultam em recusa controlada.

O evidence checker pode ser implementado como LLM separado (SLM dedicado a verificação), como conjunto de regras determinísticas, ou como híbrido. A escolha é por domínio, declarada no `AgentBinding`.

### 14.3 Fluxo Integrado: Orquestrador → Retriever → LLM → Evidence Checker

O fluxo completo no runtime de atendimento segue sequência determinística:

1. Orquestrador recebe solicitação normalizada do API Gateway com contexto da interação (jornada, fila, motivo).
2. Orquestrador consulta Policy Engine com perfil do ator, escopo e jornada. Recebe permissões, limites e ferramentas permitidas.
3. Orquestrador envia query + filtros ao Retriever.
4. Retriever busca em bases autorizadas (lexical/vetorial/híbrida), retorna trechos com metadados e classificação de confidencialidade.
5. Reranker reordena, retorna contexto ranqueado com scores ao orquestrador.
6. Orquestrador compõe prompt para LLM: contexto ranqueado + política aplicável + instruções do skill. Envia via LLM Gateway.
7. LLM Gateway aplica guardrails pré-inferência, roteia ao model serving, recebe rascunho de recomendação com citações internas.
8. Orquestrador encaminha rascunho + evidências ao Evidence Checker.
9. Evidence Checker retorna OK (com incerteza calibrada) ou Falha (com diagnóstico: inconsistência, conflito, cobertura insuficiente).
10. Se evidência suficiente: orquestrador formata recomendação final com evidências e entrega ao API Gateway.
11. Se evidência insuficiente ou conflito: orquestrador emite recusa controlada com próximo passo estruturado (escalar para supervisor, solicitar dado adicional ao requisitante).
12. Orquestrador registra decisão, fontes, versões de artefatos e métricas no log de auditoria.

## 15. Máquina de Estados da Interação

Toda interação processada pelo runtime de atendimento transita por uma máquina de estados com transições determinísticas e estados terminais obrigatórios. A máquina de estados é a projeção operacional do fluxo descrito em §14.3.

### 15.1 Estados

- **Intake.** Solicitação recebida pelo API Gateway, normalizada, headers canônicos injetados. Contexto inicial construído: jornada, canal, motivo, ator.
- **PolicyCheck.** Policy Engine avaliada. Resultado: permissões, limites, ferramentas. Se política proíbe atendimento (ator sem permissão, domínio bloqueado), transição direta a `Refuse`.
- **RetrieveEvidence.** Retriever+Reranker executados. Contexto ranqueado disponível.
- **DraftAnswer.** LLM gera rascunho via LLM Gateway. Guardrails pré e pós-inferência aplicados.
- **VerifyEvidence.** Evidence Checker avalia consistência, regras, conflito, cobertura. Produz veredito com incerteza calibrada.
- **Recommend.** Evidência suficiente e política satisfeita. Recomendação final formatada com citações.
- **Refuse.** Evidência insuficiente ou conflito de política. Recusa controlada com próximo passo estruturado.
- **Escalate.** Risco alto detectado ou suspeita de fraude. Delegação a supervisor humano com contexto completo.
- **LogAndClose.** Estado terminal obrigatório. Registro de decisão, fontes, versões, métricas em log de auditoria. Fechamento do `ProcessContext`.

### 15.2 Transições

```
[*] → Intake
Intake → PolicyCheck
PolicyCheck → RetrieveEvidence
RetrieveEvidence → DraftAnswer
DraftAnswer → VerifyEvidence
VerifyEvidence → Recommend       [evidência_ok && politica_ok]
VerifyEvidence → Refuse          [evidência_insuficiente || conflito_politica]
VerifyEvidence → Escalate        [risco_alto || suspeita_fraude]
Recommend → LogAndClose
Refuse → LogAndClose
Escalate → LogAndClose
LogAndClose → [*]
```

### 15.3 Invariantes

- Todo caminho termina em `LogAndClose`. Não existe caminho que evite registro.
- `VerifyEvidence` é obrigatório. Nenhum rascunho chega ao requisitante sem passar pelo evidence checker.
- `Escalate` preserva contexto completo (incluindo rascunho rejeitado) para o supervisor. O supervisor recebe o estado da máquina, não um resumo narrativo.
- Transições são atômicas e auditadas. Cada transição de estado gera span OpenTelemetry com estado de origem, estado de destino, e condição de transição.

## 16. Modelo de Dados de Domínio

O modelo de dados de domínio captura as entidades persistidas pelo runtime e pelo pipeline offline. Armazenamento primário: PostgreSQL (§11.5).

### 16.1 Entidades Principais

**INTERACTION.** Unidade de atendimento completa. Uma interação é uma sessão entre ator (atendente/sistema) e requisitante (cliente/sistema upstream), do intake ao close.

Atributos: `interaction_id` (PK), `started_at` (datetime), `channel` (voz/chat/email/API), `agent_id` (identificador do agente ou atendente), `customer_hash` (hash irreversível do requisitante para analytics sem exposição de PII), `journey_id` (FK para JOURNEY).

**TURN.** Unidade conversacional dentro de uma interação. Cada turno registra entrada e saída textual, já redatados de PII.

Atributos: `turn_id` (PK), `turn_number` (ordinal), `user_text_redacted`, `output_text_redacted`, `interaction_id` (FK).

**EVIDENCE.** Trecho de conhecimento citado em uma recomendação. Registra proveniência, relevância e classificação.

Atributos: `evidence_id` (PK), `snippet_id` (referência ao trecho na base de conhecimento), `relevance_score` (float produzido pelo reranker), `confidentiality_label` (herdado da KNOWLEDGE_SOURCE), `turn_id` (FK).

**KNOWLEDGE_SOURCE.** Base autorizada de conhecimento registrada no Catálogo de Metadados.

Relacionamento: cada EVIDENCE referencia exatamente uma KNOWLEDGE_SOURCE de origem.

**TOOL_CALL.** Registro de invocação de tool MCP durante uma interação.

Atributos: `tool_call_id`, `tool_name`, `mcp_server`, `input_hash`, `output_hash`, `latency_ms`, `cost_usd`, `interaction_id` (FK), `tool_id` (FK para TOOL).

**TOOL.** Ferramenta MCP registrada no Tool Registry.

**TRACE.** Registro de rastreabilidade OpenTelemetry vinculado à interação. Ponteiro para o trace distribuído no Trace Backend.

**JOURNEY.** Classificação da jornada do requisitante (ex: "Cancelamento", "Reclamação", "Consulta de saldo"). Cada interação é classificada em exatamente uma jornada.

### 16.2 Entidades de Release e Avaliação

**RELEASE.** Versão atômica de deploy no runtime. Cada release empacota versões específicas de quatro configurações.

Atributos: `release_id` (PK), `released_at` (datetime), `environment` (staging/canary/production).

**MODEL_CONFIG.** Configuração de modelo vinculada a uma release: referência ao Model Registry, parâmetros de inferência (temperature, top-p, max tokens).

**PROMPT_CONFIG.** Configuração de prompt vinculada a uma release: referência ao `SKILL.md` versionado, system prompt efetivo, exemplos few-shot.

**INDEX_CONFIG.** Configuração de índice vinculada a uma release: versão do índice vetorial no Vector DB, versão do índice lexical, parâmetros de busca híbrida.

**POLICY_CONFIG.** Configuração de política vinculada a uma release: versão do bundle OPA, referência a guardrails ativos.

**GOLD_CASE.** Caso individual do dataset gold adversarial. Cada caso é avaliado em múltiplas runs.

**EVAL_RUN.** Execução de avaliação que cruza uma release com o dataset gold. Registra métricas por caso e agregadas.

Relacionamentos: cada RELEASE inclui exatamente um MODEL_CONFIG, PROMPT_CONFIG, INDEX_CONFIG e POLICY_CONFIG. Cada EVAL_RUN referencia exatamente uma RELEASE e um ou mais GOLD_CASEs.

### 16.3 Diagrama ER Resumido

```
INTERACTION ||--o{ TURN : contains
INTERACTION }o--|| JOURNEY : classified_as
TURN }o--o{ EVIDENCE : cites
EVIDENCE }o--|| KNOWLEDGE_SOURCE : from
INTERACTION ||--o{ TOOL_CALL : triggers
TOOL_CALL }o--|| TOOL : uses
INTERACTION ||--o{ TRACE : logs
RELEASE ||--o{ MODEL_CONFIG : includes
RELEASE ||--o{ PROMPT_CONFIG : includes
RELEASE ||--o{ INDEX_CONFIG : includes
RELEASE ||--o{ POLICY_CONFIG : includes
GOLD_CASE ||--o{ EVAL_RUN : evaluated_in
RELEASE ||--o{ EVAL_RUN : evaluated_in
```

## 17. Observabilidade

### 17.1 OpenTelemetry

Stack de tracing-first. Cada envelope A2A propaga contexto W3C Trace Context. O **OpenTelemetry Collector** é o ponto de coleta centralizado no cluster, recebendo sinais de três origens:

- **AgentSvc e ToolServers.** Emitem traces e logs diretamente ao Collector via OTLP.
- **Sidecars do service mesh.** Emitem telemetria de rede (latência, erros, volume) ao Collector passivamente.
- **Model Serving.** Emite métricas de inferência (latência, throughput, GPU utilization) diretamente ao Prometheus.

Spans obrigatórios:

- `aobd.interpret_intent`
- `aobd.route`
- `router.plan`
- `router.delegate`
- `subagent.load_skill`
- `subagent.execute`
- `mcp.call.<tool>`
- `a2a.send` / `a2a.receive`
- `aigw.route_model`
- `aigw.policy_eval`
- `aigw.guardrail_pre` / `aigw.guardrail_post`
- `cache.hit` / `cache.miss`
- `retriever.search` / `retriever.rerank`
- `evidence_checker.verify`
- `state_machine.transition` (com atributos `from_state`, `to_state`, `condition`)

Atributos obrigatórios: `skill.urn`, `skill.version`, `domain`, `process`, `tenant`, `actor`, `model.id`, `model.provider`, `release.id`, `interaction.id`.

### 17.2 Pipeline de Sinais

O Collector distribui sinais para backends especializados:

- **Traces → Trace Backend** (Tempo, Jaeger). Armazena traces distribuídos ponta a ponta. Correlação por `trace_id` do envelope.
- **Logs → Log Store** (Loki, Elasticsearch). Logs estruturados de agentes, tools e infraestrutura. Indexados por `skill.urn`, `tenant`, `trace_id`.
- **Métricas → Prometheus.** Métricas de aplicação (taxas de sucesso/falha por skill, custo acumulado) e infraestrutura (CPU, memória, GPU). Model serving exporta diretamente para Prometheus.

### 17.3 Dashboards e Alertas (Grafana)

Dashboards canônicos, provisionados como código:

- **Domain Health** — por domínio: taxa de conclusão, p50/p95/p99 ponta a ponta, custo por processo.
- **Router Health** — fila, taxa de falha, distribuição de subagentes ativados.
- **Subagent Health** — latência, custo por chamada, taxa de violação de contrato de saída.
- **Skill Drift** — comparação de métricas entre versões do mesmo skill.
- **Tool Heatmap** — quais tools MCP são mais usadas, por qual skill, com que custo.
- **Model Serving** — latência de inferência por modelo, throughput, utilização de GPU, taxa de fallback para provider externo.
- **AI Gateway** — volume de requisições, taxa de cache hit, custo por tenant/modelo, rejeições por policy OPA.
- **Perímetro** — tráfego norte-sul no API Gateway, rate limiting ativado, erros de autenticação.
- **Evidence Quality** — taxa de recomendação vs recusa vs escalonamento por jornada, score médio de relevância do reranker, taxa de falha do evidence checker por tipo (inconsistência, conflito, cobertura).
- **Release Health** — métricas por release ativa: comparação com baseline, drift acumulado, error budget restante.

Alertas são derivados dos SLOs declarados nos skills e dos SLOs de infraestrutura. Canais: PagerDuty, Slack, webhook genérico.

### 17.4 MLflow

Todo subagente que envolve decisão generativa registra em MLflow:

- Prompt efetivo (SKILL.md + envelope reduzido).
- Modelo e parâmetros de inferência.
- Saída bruta e saída validada.
- Métricas de avaliação quando exemplos do `## Examples` são executados em CI.
- Lineage: pai (roteador), avô (AOBD), envelope inicial.
- Resultado do evidence checker (quando aplicável).
- Referência à RELEASE e ao EVAL_RUN correspondente.

MLflow torna-se o registro de experimentos comparando versões de `SKILL.md` em ambiente de staging antes de promoção.

### 17.5 SLOs Explícitos

Cada skill declara seus SLOs no header. Violação abre alerta em Grafana e incrementa métrica de error budget. Esgotamento de budget congela promoção de novas versões do skill até revisão.

## 18. Operação Contínua — Version Registry, Drift e Rollout

### 18.1 Version Registry

Registro centralizado de todas as versões de artefatos que compõem o runtime. O Version Registry é distinto dos registros individuais (Skill Registry, Model Registry, etc.) porque captura a **composição**: quais versões de skill, modelo, prompt, índice e política estão ativas conjuntamente em cada ambiente.

Cada entrada no Version Registry é uma **RELEASE** (§16.2) — tupla imutável de MODEL_CONFIG + PROMPT_CONFIG + INDEX_CONFIG + POLICY_CONFIG. O Version Registry mantém histórico completo de releases por ambiente (staging, canary, production), com ponteiro para o EVAL_RUN do harness que aprovou cada release.

O harness de avaliação (§9.5) produz métricas baseline quando uma release é candidata. O Version Registry armazena essas métricas como referência para detecção de drift posterior.

### 18.2 Detecção de Drift

O runtime emite continuamente métricas operacionais que são comparadas contra o baseline da release ativa. O detector de drift opera sobre dois eixos:

- **Drift de dados.** Distribuição das queries de entrada, distribuição dos scores de relevância do retriever, distribuição das jornadas — comparados com distribuições do dataset gold. Detecção: teste de Kolmogorov-Smirnov, PSI (Population Stability Index), ou janela deslizante com threshold configurável.
- **Drift de comportamento.** Taxa de recomendação/recusa/escalonamento, score médio do evidence checker, taxa de violação de output contract, custo médio por interação — comparados com baseline do harness. Detecção: CUSUM, alertas de error budget.

O detector de drift consome métricas do Prometheus e traces do Trace Backend. Quando drift é detectado acima de threshold, emite evento estruturado ao orquestrador de workflows e ao dashboard Grafana. O evento inclui: métrica afetada, magnitude do drift, release ativa, timestamp de início do drift.

### 18.3 Rollout A/B e Rollback

A promoção de releases segue pipeline gated com capacidade de rollback instantâneo:

- **Canário.** Nova release recebe 1% do tráfego. Métricas comparadas com release estável em tempo real. Se SLOs são atendidos por período configurável, tráfego incrementa: 1% → 10% → 50% → 100%.
- **A/B.** Duas releases coexistem com split de tráfego configurável. Métricas são comparadas estatisticamente. A release vencedora é promovida; a perdedora é arquivada.
- **Rollback.** Se drift é detectado ou SLOs são violados durante canário/A/B, a release é revertida automaticamente para a última release estável. Rollback é executado pelo orquestrador de workflows, que atualiza os CRDs de AgentBinding, reconfigura o AI Gateway, e invalida caches. Tempo de rollback alvo: < 60s.

O Version Registry registra cada transição de release com timestamp, motivo (promoção, rollback, drift), e referência ao EVAL_RUN ou alerta que motivou a transição.

### 18.4 Ciclo Integrado: Harness → Registry → Drift → Rollout

```
Harness baseline/regressão → Version Registry (release candidata)
Version Registry → Rollout A/B/canário (promoção gradual)
Runtime → Tracing/Logs/Métricas → Detecção de drift
Drift detectado → Rollback automático (via Version Registry)
Drift detectado → Novos casos gold (via pipeline offline)
```

O ciclo é contínuo e auto-alimentado: drift detectado em produção alimenta o pipeline offline com novos padrões, gerando nova versão do gold, que por sua vez testa a próxima release candidata.

## 19. Fluxo de Execução Canônico

### 19.1 Cenário Genérico: Apuração Fiscal

Usuário submete texto "Preciso fechar a apuração de ICMS de março da filial 07".

1. **Ingresso.** Requisição chega ao **API Gateway**, que autentica via OAuth2/OIDC com IAM, injeta `X-Tenant-ID`, `X-Actor-ID`, `X-Trace-ID`, e encaminha ao **AI Gateway**.
2. **AI Gateway.** Avalia policies OPA (o ator tem permissão para o domínio Financeiro? Budget do tenant permite?). Roteia ao AgentSvc que hospeda o AOBD do domínio Financeiro.
3. **Interpretação.** AOBD carrega seu próprio skill, constrói `IntentDescriptor` = `{process_candidate: "apuracao_icms_mensal", entities: {competencia: "2026-03", filial: "07"}}`. A inferência passa pelo AI Gateway → Model Serving.
4. **Roteamento.** AOBD consulta CAR (embeddings no Vector DB), seleciona `urn:skill:financeiro:router:apuracao_icms@2.3.1`.
5. **Delegação.** Emite `DelegationEnvelope` assinado ao AR. Token de delegação emitido pelo IAM via Vault.
6. **Hidratação.** AR carrega `SKILL.md`, valida schema, lê `## Workflow` — DAG: extrair_notas → consolidar_creditos → consolidar_debitos → calcular_saldo → gerar_guia → arquivar.
7. **Materialização do DAG.** AR emite DAG ao orquestrador de workflows (Argo/Kubeflow), que cria pipeline com dependências, retries e gates.
8. **Execução em DAG.** Para cada nó, orquestrador dispara chamada ao AgentSvc com envelope A2A do subagente correspondente.
9. **Subagente `extrair_notas`.** Carrega seu skill. Tool Bindings expõem tool MCP `erp.fiscal.query`. Sidecar aplica mTLS na conexão com o MCP server. MCP server acessa Lakehouse. LLM compõe consulta dentro das fronteiras permitidas. Retorna `ContextDelta` com conjunto de notas.
10. **Subagentes subsequentes** consomem contexto acumulado do Context Store, enriquecem com features do Feature Store quando necessário, produzem mais deltas.
11. **Compensação se falha.** Ex: `gerar_guia` falha — AR invoca passo de compensação declarado (reverter lançamentos provisórios).
12. **Consolidação.** AR produz `ProcessOutcome`, retorna ao AOBD.
13. **Resposta.** AOBD formata resposta ao ator conforme `Output Contract` do AOBD e fecha `ProcessContext`. Contexto finalizado é arquivado no Context Store frio.

### 19.2 Cenário de Atendimento: Runtime de Evidência

Atendente submete solicitação "Cliente quer saber se pode usar o plano de saúde para procedimento estético na filial São Paulo" com contexto de jornada = "Consulta de cobertura", fila = "Saúde", motivo = "Elegibilidade".

1. **Intake.** API Gateway autentica, injeta headers. AI Gateway avalia policies. Orquestrador recebe solicitação normalizada. Estado: `Intake → PolicyCheck`.
2. **PolicyCheck.** Policy Engine avalia: atendente tem perfil para domínio Saúde? Escopo de jornada permite consulta de cobertura? Ferramentas de consulta de elegibilidade estão habilitadas? Resultado: permissões concedidas, limites de budget definidos, tools `plano.elegibilidade.query` e `regulatorio.ans.query` permitidas. Estado: `PolicyCheck → RetrieveEvidence`.
3. **RetrieveEvidence.** Orquestrador monta query combinando motivo + entidades (procedimento estético, filial SP). Retriever executa busca híbrida (lexical + vetorial) nas bases autorizadas declaradas no `## Evidence Policy` do skill: manual de coberturas, tabela ANS, contratos por filial. Reranker reordena top-20 por relevância. Retorna 5 evidências ranqueadas com scores e labels de confidencialidade. Estado: `RetrieveEvidence → DraftAnswer`.
4. **DraftAnswer.** Orquestrador compõe prompt: evidências ranqueadas + política aplicável + instruções do skill. Envia via LLM Gateway. Guardrails pré-inferência: contexto não contém PII (já redatado), prompt não contém injection. LLM gera rascunho: "Procedimentos estéticos não são cobertos pelo plano básico conforme manual v12.3 seção 4.2. Na filial SP, o contrato corporativo XYZ possui cobertura parcial para procedimentos listados no anexo B." Citações internas: evidence_id E1 (manual), E3 (contrato SP). Guardrails pós-inferência: sem PII em saída, sem alucinação detectável. Estado: `DraftAnswer → VerifyEvidence`.
5. **VerifyEvidence.** Evidence Checker valida: rascunho consistente com E1 e E3? Sim. Regra de negócio violada? Não. Conflito entre fontes? Não. Cobertura: afirmação sobre manual coberta por E1, afirmação sobre contrato SP coberta por E3, ambas acima de threshold de relevância. Incerteza calibrada: 0.12 (abaixo de threshold de recusa). Veredito: OK. Estado: `VerifyEvidence → Recommend`.
6. **Recommend.** Orquestrador formata recomendação final com evidências citadas e entrega ao API Gateway → atendente.
7. **LogAndClose.** Registro: interaction_id, turns, evidências citadas (E1, E3), knowledge_sources, tool_calls, release_id ativa, métricas (latência, custo, scores). Fechamento de ProcessContext. Trace completo exportado.

Se no passo 5 o evidence checker tivesse encontrado conflito entre manual e contrato SP, o estado transitaria para `Refuse`, com recusa controlada: "Não é possível determinar cobertura com segurança — há conflito entre manual geral e contrato da filial SP. Recomendado: escalar para supervisor de benefícios."

Se no passo 5 houvesse indicativo de fraude (ex: padrão de consultas repetidas para o mesmo procedimento com variações mínimas), o estado transitaria para `Escalate`.

Todo o fluxo gera uma trace única no Trace Backend, métricas em Prometheus, experimento em MLflow, entradas em log imutável, e custos contabilizados por tenant.

## 20. Avaliação Contínua

- `## Examples` de cada skill formam sua suíte de regressão.
- Dataset gold adversarial (§9.4) forma a suíte de aceitação.
- CI executa a suíte de regressão em cada PR; regressão em taxa de sucesso ou custo bloqueia merge.
- Harness de avaliação (§9.5) executa suíte de aceitação contra release candidata antes do gate de promoção.
- Em staging, tráfego sombra (shadow traffic) copia envelopes de produção para a nova versão; divergências são medidas antes de promoção.
- Canário em produção: 1% → 10% → 50% → 100% gated por SLOs.
- Pipelines de avaliação são executados pelo orquestrador de workflows, com métricas registradas no MLflow, Model Registry e Version Registry.
- Drift detectado em produção (§18.2) realimenta o pipeline offline com novos casos para o gold.

## 21. Requisitos Não-Funcionais

- **Latência.** Orquestração AOBD < 500ms p95 (excluindo LLM); AI Gateway < 50ms p95 de overhead adicionado; Retriever+Reranker < 200ms p95; Evidence Checker < 300ms p95; execução ponta a ponta guiada pelo SLO declarado no skill.
- **Disponibilidade.** API Gateway e AI Gateway 99.95%; AOBD 99.9% por domínio; AR e SA degradam por skill, não derrubam o domínio. Retriever e Evidence Checker 99.9% (degradação: fallback para recusa controlada).
- **Escalabilidade.** Horizontal por skill; meta de 10^4 processos concorrentes por domínio em topologia padrão. Model serving escala por GPU com autoscaling (KEDA/HPA custom metrics). Retriever escala por réplica de índice.
- **Custo.** Orçamento por envelope enforced; envelopes que excedem são abortados com evento estruturado. AI Gateway contabiliza custo por tenant em tempo real.
- **Portabilidade.** Nenhum componente acoplado a cloud específico; substituível: transporte (gRPC/NATS/Kafka), registro (etcd/OCI), stores (Redis/PG/S3-compat), mesh (Istio/Linkerd), model serving (KServe/Seldon/BentoML/vLLM), AI Gateway (LiteLLM/Portkey/custom), workflow orchestrator (Argo/Kubeflow/Airflow), Vector DB (Qdrant/Weaviate/pgvector).

## 22. Extensibilidade

Pontos de extensão controlados:

- **Novo domínio** — registrar novo AOBD com seu `SKILL.md` de domínio; sem mudança na plataforma.
- **Novo processo** — publicar novo AR `SKILL.md` + entradas no CAR.
- **Nova tarefa** — publicar novo SA `SKILL.md` + `AgentBinding`.
- **Nova ferramenta** — registrar MCP server no Tool Registry governado; habilitar em skills específicas. Registrar datasets consumidos no Catálogo de Metadados.
- **Novo modelo** — registrar no Model Registry, configurar InferenceService no model serving, atualizar AI Gateway routing; skills são agnósticas.
- **Novo dataset** — registrar no Catálogo, classificar via DLP, habilitar em Feature Store ou expor via MCP server.
- **Nova policy** — publicar bundle OPA no repositório Git; AI Gateway e AgentSvc sincronizam automaticamente. Registrar no Version Registry.
- **Nova base de conhecimento** — registrar como KNOWLEDGE_SOURCE no Catálogo, indexar no Vector DB, habilitar em `## Evidence Policy` de skills.
- **Novo dataset gold** — publicar via pipeline offline (§9), registrar no Version Registry, executar harness.

Nada na plataforma requer recompilação para adicionar capacidade de negócio. A unidade de mudança é o `SKILL.md`.

## 23. Decisões Arquiteturais Chave (ADRs resumidos)

- **ADR-001.** SKILL.md em Markdown com frontmatter YAML, não JSON/YAML puro — legibilidade para donos de negócio é requisito de adoção.
- **ADR-002.** Agent2Agent sobre gRPC+NATS, não HTTP REST — semântica de streaming e eventos é primária.
- **ADR-003.** Contexto separado de estado — evita envelopes obesos e permite recuperação pontual de processos longos.
- **ADR-004.** MLflow para experimentos de skills, não apenas de modelos — promoção de skill é decisão equivalente à promoção de modelo.
- **ADR-005.** Kubernetes CRDs em vez de config database — GitOps ponta a ponta, reconciliação declarativa.
- **ADR-006.** OpenTelemetry end-to-end sem tradução proprietária — evita lock-in e permite consolidar com sistemas existentes.
- **ADR-007.** AI Gateway como camada de indireção entre agentes e modelos — desacopla lógica de skill da topologia de serving, habilita fallback, cache e governança de inferência sem mudança em skills.
- **ADR-008.** Perímetro duplo (API Gateway + AI Gateway) em vez de gateway único — separação de responsabilidades: autenticação/rate-limiting no API Gateway, governança de IA no AI Gateway. Permite evolução independente.
- **ADR-009.** Orquestrador de workflows externo (Argo/Kubeflow/Airflow) complementando AR — DAGs complexos, long-running e com dependências temporais são melhor servidos por engines dedicadas do que por lógica custom no roteador.
- **ADR-010.** Vector DB como serviço compartilhado do CAR e de RAG — evita duplicação de índices e centraliza governança de embeddings.
- **ADR-011.** Evidence Checker como componente independente do LLM gerador — separação de geração e verificação elimina conflito de interesse; permite implementações distintas (LLM verificador, regras determinísticas, híbrido) sem acoplar ao modelo principal.
- **ADR-012.** Dataset gold adversarial como gate de release, não apenas como suíte de teste — release sem aprovação no gold é impedida em nível de pipeline, não apenas reportada. O gold é artefato de produção, não de qualidade.
- **ADR-013.** Recusa controlada como comportamento explícito da máquina de estados, não como fallback implícito — `Refuse` e `Escalate` são estados de primeira classe com output contracts próprios, rastreados com mesma fidelidade que `Recommend`.
- **ADR-014.** Version Registry como composição de artefatos (modelo + prompt + índice + policy) em vez de versionamento individual — a unidade de deploy é a RELEASE, não o artefato isolado. Drift e rollback operam sobre releases, não sobre componentes.
- **ADR-015.** pgvector como opção integrada ao PostgreSQL para Vector DB — reduz componentes operacionais em domínios de escala moderada; domínios de alta escala migram para Vector DB dedicado sem mudança em skills (transparente via MCP server).

## 24. Glossário

- **AOBD.** Agente Orquestrador de Business Domain.
- **AR.** Agente Roteador.
- **SA.** Subagente.
- **CAR.** Catálogo de Agentes Roteadores.
- **A2A.** Protocolo Agent2Agent.
- **MCP.** Model Context Protocol.
- **Envelope.** Unidade de mensagem A2A tipada, assinada e rastreada.
- **Skill.** Contrato executável descrito em `SKILL.md`.
- **Permitted Toolset.** Interseção entre inventário MCP disponível e tools declaradas em um `SKILL.md`.
- **ProcessContext.** Estrutura de contexto e estado de uma execução de processo, aberta pelo AOBD, propagada por toda a cadeia.
- **API Gateway.** Ponto de entrada norte-sul; autenticação, rate limiting, roteamento.
- **AI Gateway / LLM Gateway.** Camada de indireção entre agentes e model serving; roteamento de modelo, cache de inferência, guardrails, governança de uso.
- **Model Serving.** Infraestrutura de inferência de modelos (KServe, Seldon, BentoML, vLLM).
- **Lakehouse.** Repositório central de dados estruturados e semi-estruturados.
- **Feature Store.** Serviço de features pré-computadas para inferência e enriquecimento de contexto.
- **Model Registry.** Registro versionado de modelos (weights, configs, métricas, referências a APIs externas).
- **Vector DB.** Armazenamento de embeddings para busca por similaridade semântica.
- **Catálogo de Metadados.** Registro de todos os ativos de dados com schema, lineage e classificação.
- **OPA.** Open Policy Agent; engine de policies as code.
- **Policy Engine.** Materialização da Policy Engine via OPA; avalia permissões, limites e ferramentas em tempo real.
- **DLP.** Data Loss Prevention; classificação e proteção de dados sensíveis.
- **KMS/HSM.** Key Management Service / Hardware Security Module; gerenciamento de chaves criptográficas.
- **Vault.** Secrets manager centralizado.
- **Service Mesh.** Camada de rede (Istio/Linkerd) com sidecars para mTLS, routing e telemetria.
- **Retriever.** Componente de busca híbrida (lexical + vetorial) em bases autorizadas de conhecimento.
- **Reranker.** Modelo cross-encoder que reordena resultados do Retriever por relevância contextual.
- **Evidence Checker.** Verificador independente de consistência, cobertura e conflito entre recomendação gerada e evidências citadas.
- **Guardrails / Security Filters.** Filtros pré e pós-inferência integrados ao LLM Gateway para detecção de injection, PII, alucinação.
- **Dataset Gold Adversarial.** Conjunto versionado de casos de teste (normais + adversariais) com ground truth, usado como gate de release.
- **Harness.** Motor de avaliação que executa skills contra dataset gold e produz relatórios de baseline/regressão.
- **RELEASE.** Tupla imutável de configurações (modelo + prompt + índice + policy) que define o estado do runtime em um dado momento.
- **Version Registry.** Registro de composições de artefatos (releases) com histórico, métricas baseline e estado de promoção.
- **Drift.** Desvio estatisticamente significativo entre métricas operacionais e baseline da release ativa.
- **INTERACTION.** Unidade de atendimento completa (intake ao close) registrada no modelo de dados de domínio.
- **TURN.** Unidade conversacional dentro de uma interação.
- **EVIDENCE.** Trecho de conhecimento citado em recomendação, com score de relevância e label de confidencialidade.
- **KNOWLEDGE_SOURCE.** Base autorizada de conhecimento registrada no Catálogo de Metadados.
- **JOURNEY.** Classificação da jornada do requisitante (ex: cancelamento, reclamação, consulta).