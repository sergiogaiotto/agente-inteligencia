# Testes E2E via Comet — Plano de Cenários, Achados e Recomendações

> **Propósito:** documento autossuficiente para **retomar os testes ponta-a-ponta da plataforma pela TELA (via Comet/Chromium)** numa nova sessão. Contém o setup, o estado atual, o resultado + achados do **Cenário A** (já executado) e os **cenários recomendados (B–I)** com objetivo, o que cada um exercita, passos e o que observar.
>
> Criado em **2026-07-08** por Claude (sessão de teste E2E). Plataforma: **Maestro / AI Mesh**. Deploy sob teste: **`http://vps.falagaiotto.com.br:8080/`** (v29.0.x).

---

## 0. Como retomar (setup do teste via Comet)

### Ambiente
- **URL:** `http://vps.falagaiotto.com.br:8080/` (VPS). Direto (sem Caddy) seria `:7000`; use o `:8080`.
- **Usuário:** `sergio.gaiotto` (role **root**). **A senha fica no cofre do usuário — o assistente NÃO digita senha** (regra de segurança). Opções de login:
  1. O usuário faz o login no Comet; o assistente assume a sessão já autenticada; **ou**
  2. Se o assistente precisar re-logar por API, pedir a senha na hora — não guardar.
- **LLM da VPS (reais):** roteados por `task_type` no **Configurações → Roteamento LLM**: `tool_calling/reasoning/instruct/skill_generation → gpt-oss-120b` (hub Claro), `classification → gpt-oss-20b`, `judge/multimodal_fallback → azure/gpt-4o`. Embeddings: **qwen3** (hub Claro). Tudo alcançável da VPS.

### Conexão do navegador
- **Comet é Chromium** → aceita a extensão da Chrome Web Store. Instalar/logar a extensão **"Claude for Chrome"** (mesma conta). Só abrir o Comet **não basta** — a extensão precisa estar conectada. Verificar com `list_connected_browsers`.
- Se as ferramentas de browser estiverem "deferred", carregar via ToolSearch: `tabs_context_mcp, navigate, computer, read_page, tabs_create_mcp, browser_batch, find, form_input, javascript_tool, resize_window`.

### GOTCHAS de automação via Comet (aprendidos no Cenário A — importantes!)
1. **`window.prompt()` NATIVO bloqueia a automação.** O botão **"+ Novo" (pipeline)** abre um `prompt()` nativo do navegador ("Nome do novo pipeline"). Enquanto ele está aberto, **CDP trava** (clique/screenshot dão timeout — parece "freeze" mas NÃO é). **Solução:** sobrescrever antes de clicar:
   ```js
   window.prompt = () => "Nome do Pipeline";
   ```
   depois clicar "+ Novo". (Isso é também um **achado** — ver §2: trocar por modal in-page.)
2. **Testar a API pela própria sessão do browser** (autenticada por cookie), via `javascript_tool`:
   ```js
   function ck(n){var m=document.cookie.split('; ').find(c=>c.startsWith(n+'='));return m?decodeURIComponent(m.split('=')[1]):null;}
   var csrf=ck('csrf_token');
   var r=await fetch('/api/v1/pipelines/<PIPELINE_ID>/invoke',{method:'POST',credentials:'include',
     headers:Object.assign({'Content-Type':'application/json'},csrf?{'X-CSRF-Token':csrf}:{}),
     body:JSON.stringify({message:"..."})});
   return {http:r.status, body: await r.json()};
   ```
   Para verbosidade total do trace: `.../invoke?verbosity=full` e ler `steps[i].trace.evidence_count/evidence_sources/execution_log`.
3. **`<select>` nativos NÃO reagem a clique sintético na opção** (Skill Vinculada, Esforço de raciocínio, Tipo de skill). Usar **`form_input`** com o ref (aceita value OU texto da opção). Pegar o ref com `find`.
4. **A janela do Comet redimensiona sozinha** (1416→1253→1227…). Coordenadas fixas quebram. **Fixar** com `resize_window(1440,900)` e **preferir `find`+refs** a coordenadas. Reler screenshot após cada ação.
5. **Canvas do Fluxograma é pesado** com muitos nós ("Mesh completo" = 35+). **Selecionar um pipeline pequeno primeiro** alivia o canvas antes de operar.
6. **Drag-to-connect (arestas):** arraste da **bolinha de saída** (círculo à direita do nó) até o nó destino → abre modal in-page "Nova conexão". Se a bolinha já tem aresta, mire o **centro exato** dela (zoom ajuda), senão abre o editor da aresta existente.

### Convenções do teste
- **Modo observação:** testar e **coletar achados**; **não implementar correções durante o teste** salvo pedido explícito do usuário. Ao investigar bug, usar a **codebase local** (está em `main`, mesma versão da VPS).
- **Severidade dos achados:** 🔴 bug/bloqueio · 🟠 fricção · 🟡 polimento/UX · 🟢 positivo.
- **Idioma:** pt-BR (UI e conteúdo).
- Ao achar bug de código, o fix segue as convenções do repo (branch `fix/*` ou `feat/*`, `--base main`, bump `APP_VERSION`, teste pytest, PR). VPS só reflete após **deploy**.

---

## 1. Estado atual (2026-07-08)

- **Repo `main` = v29.0.1** (fix do parser mergeado, PR #518). Local pareado com origin/main.
- **VPS:** rodava **v29.0.0** durante o teste (rodapé/`GET /api/health`). **O fix #518 exige DEPLOY na VPS.**
- **Cenário A CONSTRUÍDO na VPS** (ver §2). Roteamento condicional validado; RAG dos especialistas bloqueado pelo bug do parser (corrigido no repo, pendente deploy).

### ⏱️ Primeiro passo da nova sessão
1. Abrir a VPS, conferir a **versão** (rodapé ou `GET /api/health`).
2. **Se VPS ≥ 29.0.1:** reexecutar o **Cenário A** (mensagem negativa + positiva) e conferir no trace `evidence_count > 0` e `evidence_sources` com o KB → confirma os Especialistas **usando os playbooks** (fecha o gap nº1).
3. **Se VPS ainda 29.0.0:** pedir o deploy ao usuário; enquanto isso, seguir para os cenários que não dependem do fix (B, E, F…).

### ✅ ATUALIZAÇÃO 2026-07-08 (sessão de retomada) — Cenário A REVALIDADO na VPS 29.0.1
- VPS confirmada em **v29.0.1** (rodapé); fix #518 **deployado**.
- **Teste decisivo PASSOU** (`POST /pipelines/29ffab0f.../invoke?verbosity=full`, sessão-cookie):
  - Msg **negativa** → Triagem `NEGATIVO` → Engajamento `skipped_conditional` → **Acolhimento completed, `trace.evidence_count:1`, `trace.evidence_sources:["Acolhimento - Sentimento Negativo"]`**. Resposta ancorada no playbook (registro prioritário + escalonamento), sem mais pedir "acesso à base". ~9,3s.
  - Msg **positiva** → Triagem `POSITIVO` → Acolhimento `skipped_conditional` → **Engajamento completed, `trace.evidence_count:1`, `trace.evidence_sources:["Engajamento - Rentabilizacao (Positivo)"]` (score 0.95)**. Resposta ancorada (plano premium + consultoria). ~9,3s.
- **Gap nº1 (🔴) FECHADO nos DOIS caminhos.** O 🟡 "grounding leniente com evidence_count:0" fica **superado para este cenário** (agora há evidência); revalidar o comportamento-limite no **Cenário G**.
- Nota de observabilidade (🟢/neutro): `evidence_count`/`evidence_sources` vivem em `step.trace.*` (consistente nos dois testes); o nível-topo do step NÃO os carrega — usar sempre `trace`.

---

## 2. Cenário A — "Atendimento com Triagem de Sentimento" (EXECUTADO)

**Objetivo:** atender clientes entendendo o motivo com **análise de sentimento**. A **Triagem** classifica e roteia condicional: **negativo → Especialista de Acolhimento** (mapeia o problema + propõe abordagem); **positivo → Especialista de Engajamento** (engaja em novos serviços/rentabilização). Cada especialista tem seu **KB** (playbook). A Triagem entrega **sentimento + texto original verbatim**.

### Artefatos criados na VPS (IDs para reuso)
| Tipo | Nome | ID / observação |
|---|---|---|
| KB (texto) | Acolhimento - Sentimento Negativo | `ee1b4458-5237-433c-b099-8ba3dc3737bd` (1 chunk) |
| KB (texto) | Engajamento - Rentabilizacao (Positivo) | 1 chunk |
| Skill | Triagem de Sentimento… | `da175635-94cd-4b99-bda9-b980d13be9d2` (kind router) |
| Skill | Especialista de Acolhimento… | `1a36e95b-d635-4c42-aafa-ea0b4ce86a22` (subagent) |
| Skill | Especialista de Engajamento… | `7fa5268d-a395-40d9-acea-424c086d6c08` (subagent) |
| Agente | Triagem de Sentimento | Classification → gpt-oss-20b, temp 0.1 |
| Agente | Especialista Acolhimento | `d55c61a5-744f-4397-9c95-518254adc62c` · Reasoning → gpt-oss-120b · grounded |
| Agente | Especialista Engajamento | Reasoning → gpt-oss-120b · grounded |
| Pipeline | Atendimento - Triagem de Sentimento | **`29ffab0f-09de-4753-b8b9-30c7e7f1cf93`** (rascunho) |

**Fluxo:** `Início → Triagem → [condicional 'negativo' in output_lower] → Acolhimento` · `[condicional 'positivo' in output_lower] → Engajamento` (fan-out). "O que o próximo recebe: **Tudo (padrão)**" → o especialista recebe SENTIMENTO + TEXTO_ORIGINAL.

### Resultado do teste via API
✅ **Roteamento condicional CORRETO nos 2 sentidos:**
- Msg **negativa** → Triagem completed → Engajamento `skipped_conditional` → **Acolhimento completed**.
- Msg **positiva** → Triagem completed → **Engajamento completed** → Acolhimento `skipped_conditional`.

### Achados do Cenário A
> 🔴 = bug/bloqueio · 🟠 = fricção · 🟡 = polimento/UX · 🟢 = positivo

**Runtime / RAG**
- 🔴 **[nº1 — CORRIGIDO #518, pendente deploy VPS] KB vinculado na skill não chegava ao retriever.** Especialistas rodavam com `evidence_count:0` e pediam "acesso à base" — não usavam o playbook. **Causa:** `_parse_evidence_policy`/`_parse_output_shape` extraíam o YAML com `re.sub(r"\n```\s*$","",body)` (só tira a fence no fim absoluto). O wizard injeta `---` após a fence → fence+`---` vazam → `yaml.safe_load` estoura `ScannerError` → fallback `{raw}` → `sources` perdido → `_declares_sources=False` → engine PULA o RAG do especialista em pipeline (`_pipeline_should_self_retrieve`). Era o **bug #244** (migração incompleta). **Fix:** reusar `_extract_fenced_yaml_body`. **Ao reconfirmar na VPS pós-deploy, este é o teste decisivo.**
- 🟡 **Grounding leniente:** `require_evidence:1` + `allow_general_knowledge:0` + `evidence_count:0` mesmo assim passou `VerifyEvidence → Recommend (evidence_ok)` e respondeu pedindo "acesso à base" ao cliente. Esperava-se recusa clara ou fallback melhor. **Revalidar após o fix nº1** (pode ser efeito colateral de 0 evidência).
- 🟡 `entry_agent_id` do pipeline = `null` no `GET /pipelines`, embora o Fluxograma mostre "entrada automática" e o invoke resolva a raiz. Funciona, mas confunde observabilidade.

**Bases de Conhecimento**
- 🟢 Fluxo Nova Base → Ingerir claro; ingestão rápida; embeddings qwen3 OK (KB1 495 tok/3996ms).
- 🟠 Criar KB é **2 passos** ("Registrar" cria vazia → "Ingerir" separado). Poderia ingerir no mesmo modal.
- 🟡 "Tipo de conteúdo" default = **Misto**; para playbook de texto, **Texto** é melhor — default poderia ser contextual.
- 🟡 Clutter: KB "asdf" vazia pré-existente com aviso permanente; sem limpeza/arquivamento fácil.

**Skills (wizard "IA, me ajude")**
- 🟢 **EXCELENTE:** gera SKILL.md canônico e preciso — Output Contract (JSON Schema), Activation Criteria, `## Inputs`, `kind` correto, auto-infere Execution Profile e tipo. Vincular Fontes RAG por chip é intuitivo.
- 🟠 Seletor de **TIPO** (Especialista/Triagem/Maestro) é `<select>` nativo — clique sintético na opção não registra (usar form_input). **Validar em uso humano real.**
- 🟡 Durante a geração, o editor SKILL.md pisca com o **template vazio (0 chars)** antes de popular — parece que falhou. Manter spinner.
- 🟡 Geração gpt-oss-120b leva ~18–25s; "Gerando…" sem barra/ETA.

**Agentes (wizard 4 passos + Mentor)**
- 🟢 Bem estruturado: tipo, Skill Vinculada c/ preview, Conhecimento (RAG + "conhecimento geral"), Tipo de Tarefa mostrando o modelo, Temperatura, Esforço de raciocínio, System Prompt com composers, Revisão "9 checks limpos", painel Mentor com Prontidão %. Revisão explicita grounding por agente.
- 🟠 `<select>` nativos (Skill Vinculada, Esforço de raciocínio) exigiram form_input. **Validar onChange no uso humano.**
- 🟡 Chip de Domínio: 1º clique (form assentando) não pegou; 2º sim.
- 🟡 Ao escolher "Reasoning", surge "Esforço de raciocínio" e o layout desloca.
- 🟡 **Sentimento NEUTRO sem rota:** a Triagem classifica POSITIVO/NEGATIVO/**NEUTRO**, mas o pipeline só roteia neg/pos. NEUTRO fica órfão — precisa de rota **default (else)**. (Testar no Cenário A v2 ou num cenário dedicado.)

**Pipeline / Fluxograma**
- 🟢 **Construção funciona bem:** painel AGENTES NO PIPELINE + INCLUIR AGENTES (busca/filtros/"+"), lifecycle, Publicar no Catálogo, Roteamento rápido, Auditoria da resposta.
- 🟢 **Drag-to-connect + modal in-page "Nova conexão" é EXCELENTE:** Tipo (Sequencial/Paralela/**Condicional**/Default-else) + "O que o próximo recebe". **Construtor de regra condicional ótimo:** tradutor NL→regra, cards (Palavra-chave/Conteúdo/Decisão final/Tamanho/Parâmetro exato/Tem anexo), "escrever à mão", **simular com dados**.
- 🟢 **Entrada auto-definida** (Início→Triagem vira "entrada automática" ao criar a 1ª conexão). Fan-out com badge "⚠ fan-out".
- 🟠 **Tradutor NL→regra errou:** "quando o sentimento for NEGATIVO" gerou `is_refuse` (recusa) — não conhece campos custom (SENTIMENTO). Melhoria: consultar o Output Contract da skill anterior.
- 🟠 **Card "Conteúdo" enganoso:** só faz link/PDF/imagem (`contains_url/pdf/image`), não "contém o texto X". O match de texto está em **"Palavra-chave"** (`'x' in output_lower`, "Onde: na resposta"). Renomear/mesclar.
- 🟠 **"+ Novo" usa `window.prompt()` NATIVO** — UX pobre, bloqueia thread, sem validação (nome vazio/duplicado), quebra automação. **Trocar por modal in-page.**
- 🟡 Drag-to-connect exige precisão (aresta existente vs bolinha). Zoom ajuda.
- 🟡 Escape não fecha o modal de edição de conexão (tive que Salvar).

---

## 3. Cenários recomendados (B–I) — cobrem a largura da plataforma

> Cada cenário exercita **capacidades distintas** não cobertas no A. Sugestão de prioridade: **B, E, C, F** (alto valor/distintos) → depois **D, G, H, I**. Os que dependem do fix nº1 (RAG) estão marcados.

### Cenário B — Orquestração por Maestro (cadeia sequencial multi-etapa)
- **Objetivo:** um **Maestro (AOBD)** coordena uma cadeia **sequencial** (não condicional). Ex.: *"Onboarding de cliente"*: Coletar necessidade → Verificar elegibilidade → Gerar proposta → Redigir mensagem final ao cliente.
- **Exercita:** agente tipo **Maestro**; arestas **sequenciais**; múltiplos passos encadeados; "O que o próximo recebe" (**Tudo** vs **só a resposta**); passthrough; decomposição do Maestro.
- **Passos:** criar 1 Maestro + 3–4 Especialistas (podem reusar skills simples), pipeline linear, invocar com um caso.
- **Observar:** o Maestro decompõe/coordena? cada passo recebe o output anterior corretamente? custo/tokens acumulam por passo? trilha por agente na Rastreabilidade; latência da cadeia.

### Cenário C — RAG-Tabela (dados estruturados / DuckDB) 🔵 usa dados tabulares
- **Objetivo:** agente consulta uma **TABELA** (não texto). Ex.: *"Consulta de limite de crédito"* por segmento/UF/score. A VPS já tem a tabela **Aurora `TB_ANALISE_CREDITO`** (usada no cenário Aurora) — reusar ou ingerir uma nova CSV/XLSX como **Tabular**.
- **Exercita:** KB tipo **Tabular** (DuckDB); `## Data Tables` no SKILL.md (**binding declarativo, query PARAMETRIZADA — não text-to-SQL**); Catálogo de Dados; **PII mascarada** na saída; Tier 2 (text-to-SQL) só se o toggle estiver ON.
- **Passos:** criar/registrar KB tabular → skill declarativa com `## Data Tables` (via wizard "Vincular Tabelas") → agente → invocar com filtros.
- **Observar:** query parametrizada roda sem LLM gerando SQL? PII sai mascarada? resultado tabular; erro gracioso se filtro vazio.

### Cenário D — API declarativa + MCP (ferramentas externas)
- **Objetivo:** agente que chama **API externa** (ex.: **Consultar CEP** declarativo) e/ou uma **tool MCP** (ex.: **Tavily** search). Ex.: *"Enriquecer endereço via CEP"* ou *"Pesquisar na internet e resumir"*.
- **Exercita:** `## API Bindings` (`execution_mode: declarative`); **MCP per-tool** (toggle em Configurações); invocação real de tool; tools não-resolvidas viram **warning** (não alucina); allowlist/OPA.
- **Passos:** no wizard de skill, "Vincular API (endpoint)" (há GET Consultar CEP/DDD/IBGE) ou "Vincular Ferramentas MCP" (Tavily/Context7) → agente → invocar.
- **Observar:** a API/tool é chamada de verdade (não alucinada)? binding declarativo aparece no trace? nomes de tool que não casam no Registry viram warning explícito?

### Cenário E — Args selados / contrato (invoke estruturado + roteamento determinístico)
- **Objetivo:** pipeline invocado com **args estruturados** (`## Inputs`) em vez de texto livre. Ex.: *"Cotação"* com `{produto, quantidade, canal}`; **roteamento condicional DETERMINÍSTICO por `inputs.canal`** (sem LLM no router).
- **Exercita:** contrato de **args** (`## Inputs` do agente-raiz), **modo dry** (`/invoke` com `dry:true`), **envelope param selado** (`x-uso: param`), roteamento condicional por **`inputs.X`** (card "Parâmetro exato" — determinístico), **fast_routing** (pular LLM do router), validação **422** nomeada.
- **Passos:** skill-raiz com `## Inputs`; pipeline; invocar `dry:true` (ver resolução+proveniência sem executar), depois real; aresta condicional por `inputs.canal`.
- **Observar:** `dry` resolve args/defaults/proveniência sem gastar LLM? args inválidos → 422 nomeando o campo? router pula o LLM quando as arestas são 100% por args (badge/limite ~62ms)?

### ✅ Cenário F — EXECUTADO 2026-07-08 (VPS 29.0.1)

**Método:** key criada pela sessão-cookie do browser; invocada como **frontend externo real via `curl` SEM cookie** (o cookie vence o X-API-Key em `require_user` → precisa ser sem-cookie p/ exercitar `request.state.api_key_id`). Key de teste `e2e-cenario-f` (`c28775c4-…`, prefixo `ag_live_p7M5`).

| # | Sub-teste | Resultado |
|---|---|---|
| T1 | **Contenção de privilégio P0** (`X-API-Key` em rotas de escalação) | 🟢 `GET /api-keys`, `/settings`, `/users`, `/domains` → **403 `api_key_forbidden_route` / `escalation_or_secret_route`** |
| T2 | **Descoberta** permitida à key | 🟢 `GET /pipelines`, `/pipelines/{id}/inputs-schema` → 200; sem-auth → 401 |
| T3 | **Invoke + envelope versionado** (P1-B) | 🟢 `schema_version:1`, `output_is_json:true`, **`data` = objeto JSON parseado**, `verbosity:summary` (default ciente-de-auth p/ key) |
| T4 | **Atribuição por-key** (F12) | 🟢 interação com `metadata.via="api_key"`, `api_key_name="e2e-cenario-f"`; `/history` distingue via-key vs cookie/ui |
| T6 | **Guards de orçamento** (hardening adversarial) | 🟢 budget `0`/`-1.5` → 400 "positivo"; **`NaN`/`Infinity` → 400 "número finito"**; janela `week` → 400; `0.5/month` → 201 |
| T5 | **Gate published-only** | 🟢 `published_only ON` + DRAFT via key → **403 `pipeline_not_published`**; após **publicar** → **200**. (Publicar habilita `next_states:[aposentado, rascunho]` = despublicável) |
| T7 | **Quota de custo F6 (402)** | 🟠 **Mecânica FIADA** (toggle ON, `PUT /budget` grava, invoke passa, `last_used` atualiza, débito roda sem erro) — mas **`spent_usd` fica $0 porque gpt-oss custa US$ 0** → o **402 NÃO dispara ao vivo**. Enforcement é code-verified (`enforce_budget`: `spent>=budget`→402) + 34 testes unitários. **Para tripar ao vivo: step roteado a Azure/OpenAI (multimodal→gpt-4o) OU seed no ledger.** → tentar no Cenário H. |

**Achado 🟡 F-obs:** em `verbosity=summary` (default da key) a resposta usa a chave `steps` (trimada, sem `cost_usd`/trace por step) — enquanto `verbosity=full` usa `pipeline_steps` com trace completo. Consistente, mas dois nomes de campo dependendo do preset pode confundir consumidores. (Custo por-step some no summary; o débito server-side usa o resultado interno completo, então funciona.)

**Veredito F:** 🟢 governança de API externa (P0 contenção, published-only, envelope versionado, atribuição, guards de orçamento) **sólida e comprovada ponta-a-ponta**. Única lacuna: o **402 ao vivo** depende de custo real (gpt-oss=$0).

> ⚠️ **CHECKLIST DE RESTAURAÇÃO (reverter ao final de TODOS os testes):**
> 1. `PUT /settings {api_key_invoke_published_only:false}` (era OFF)
> 2. `PUT /settings {api_key_cost_budget_enabled:false}` (era OFF)
> 3. Despublicar `29ffab0f`: `POST /pipelines/29ffab0f…/status {status:"rascunho"}` (era rascunho)
> 4. Revogar key de teste `c28775c4-ae64-41cb-b93a-befc3470da08` (`DELETE /api-keys/{id}`)

---

### Cenário F (original) — Publicação no Catálogo + consumo via API Key (governança externa)
- **Objetivo:** **publicar** um pipeline (ex.: o Cenário A) no **Catálogo**, gerar uma **API Key**, invocar via **`X-API-Key`** como "frontend externo"; exercitar **quota de custo (F6)**, **published-only** e **CORS**.
- **Exercita:** lifecycle Rascunho→**Publicado**; Catálogo/trust; **API Keys** (Configurações → API Keys); **F6 — orçamento de custo por key** (toggle em Plataforma + teto por key); gate **published-only**; **atribuição por-key** na metadata; **envelope versionado** (`schema_version`, `data`, `output_is_json`); CORS allowlist.
- **Passos:** publicar o pipeline; criar API Key com orçamento (ex.: US$ 0,50/mês); invocar via `X-API-Key`; ligar o toggle de quota e estourar o teto (→ **402**); testar rascunho vs publicado com published-only ON.
- **Observar:** key invoca só publicado? orçamento debita e bloqueia 402 com corpo claro? metadata da interação mostra `via:api_key`? envelope tem `schema_version`? (NB: gpt-oss custa US$ 0 — usar Azure na rota ou seedar o ledger p/ testar o 402).

### Cenário G — Grounding / anti-alucinação (recusa por falta de evidência)
- **Objetivo:** agente **grounded SEM KB relevante** recebe pergunta fora do escopo → deve **RECUSAR** (não inventar). Contraprova: agente com `allow_general_knowledge=1` responde do conhecimento geral.
- **Exercita:** `grounding_strict` (Plataforma), `VerifyEvidence`, **recusa por falta de evidência**, escape hatch por agente ("Permitir conhecimento geral").
- **Passos:** agente grounded sem KB → perguntar algo factual fora de escopo → observar recusa. Depois ligar "conhecimento geral" → observar resposta.
- **Observar:** recusa **clara** vs alucinação? confirma/derruba o achado 🟡 do Cenário A (grounding leniente com 0 evidência)?

### ✅ Cenário G — EXECUTADO 2026-07-08 (VPS 29.0.1) — grounding / anti-alucinação

**Método:** invoke direto por-agente (`POST /agents/{id}/invoke`, sessão-cookie) com a MESMA pergunta fora de escopo ("Quantas luas tem Júpiter?"), variando o grounding.

| Agente | Config | Resultado |
|---|---|---|
| `_Q-meu-KB_` (`216d1396`) | `require_evidence:1`, `allow_general:0` (grounded) | 🟢 **"⚠ Recusa controlada: Evidência insuficiente para recomendação segura. Próximo passo: Escalar para supervisor…"** — recusa clara, **não alucina**. `evidence_score:0.5`, `final_state:LogAndClose` |
| `PO` (`52a3352d`) | `require_evidence:0`, `allow_general:1` (não-grounded) | 🟢 **"Júpiter tem 95 luas conhecidas"** — responde do conhecimento geral (correto) |

**Veredito G:** 🟢 o guardrail anti-alucinação e o **escape hatch por-agente** funcionam. Grounded + zero evidência → **recusa controlada** (não "peço acesso à base"); non-grounded → responde livre.

**Resolve o 🟡 do Cenário A:** o mecanismo de recusa dura EXISTE e funciona. O "peço acesso à base" visto no Cenário A pré-fix era sintoma do **KB não chegando** (bug #518) — o especialista tinha `require_evidence:1` mas a resposta improvisada não acionava a recusa dura porque o output-contract da skill moldava outra saída. Com o KB chegando (pós-#518) e evidência presente, o problema não reaparece. **Nuance para o backlog:** a recusa dura ("⚠ Recusa controlada") aparece no invoke DIRETO; dentro de pipeline, o especialista molda a saída pelo seu Output Contract — vale padronizar a mensagem de recusa também no caminho pipeline.

**Obs. de shape:** invoke por-agente devolve `outputs` (não `output`/`data`), `status:"ok"`, `evidence_score`, `errors[]` — envelope diferente do invoke de PIPELINE (`schema_version`/`data`/`output`). Dois contratos de resposta distintos entre agente e pipeline.

---

### Cenário H — Multimodal / anexos
- **Objetivo:** agente que **aceita documentos/imagens** recebe um anexo → extrai/resume. Ex.: *"Analisar a fatura anexada"* (PDF) ou *"Descrever a imagem"*.
- **Exercita:** toggle **"aceita anexos"** (imagens/documentos); **roteamento multimodal** (`multimodal_fallback → azure/gpt-4o` quando há imagem e o modelo da task é text-only); **dispatcher de anexos** por agente na cadeia (só entrega ao SA que aceita).
- **Passos:** agente com anexos ON; invocar via UI (modal Executar) ou API com `attachments` (base64); numa cadeia, ver o dispatcher entregar só ao agente certo.
- **Observar:** o anexo chega ao agente certo? multimodal roteia pro modelo com visão? a Rastreabilidade mostra "anexo entregue" no SA?

### ⚠️🔴 Cenário H — EXECUTADO 2026-07-08 (VPS 29.0.1) — multimodal QUEBRADO no caminho mesh

**Método:** PNG 64×64 (metade vermelha / metade azul, 140 bytes) enviado como `attachments:[{filename,content_type:image/png,content_base64}]` ao agente callable **`_Analise de anexo` (aobd `ae87947c`)** via X-API-Key (curl, 150s timeout — o invoke pelo browser estoura o CDP de 45s).

**Resultado 🔴:** o anexo foi **aceito** (`rejected_attachments:[]`) e cardado como `category:image, routed_to:vision, extracted_chars:16`, MAS o agente respondeu *"Não há informações da imagem disponíveis… não é possível descrevê-la"* e o subagente disse *"nenhum arquivo ou imagem foi enviado"*.

**Causa observada:** TODOS os agentes da cadeia executaram em **modelo TEXT-ONLY** apesar do label de trace dizer azure/gpt-4o:
| Agente na cadeia | Label no trace | `agent_model` REAL executado |
|---|---|---|
| `_Analise de anexo` (aobd) | azure/gpt-4o | **openai/gpt-oss-20b** |
| `analise imagem` (SA) | azure/gpt-4o | **openai/gpt-oss-20b** |
| `_Categorizar Imagem` (SA) | azure/gpt-4o | **openai/gpt-oss-120b** |

Como o modelo resolvido é text-only, `_build_user_message_content` (`engine.py:4390`) **DESCARTA a imagem** (`mesh.vision.image_dropped_text_only_model`) — nenhum modelo de visão vê os pixels. A extração degradou pra `extracted_chars:16` (ruído), então o LLM literalmente não recebeu imagem.

**Não é config de ambiente:** `GET /dashboard/llm-routing` confirma `multimodal_fallback: azure/gpt-4o` (correto) e Azure é alcançável da VPS. O swap `resolve_llm_for_task(has_image=True)` → azure/gpt-4o (`llm_routing.py:305`) e a detecção `detect_image_in_attachments` (`llm_routing.py:340`) EXISTEM; o swap `has_image` roteado no `engine.py:1912` disparou pro agente-raiz, mas na prática **os agentes da cadeia ficaram no modelo text-only da task** → imagem descartada. **Hipótese (requer confirmação por log de runtime, não visível na VPS):** o dispatch de subagentes na mesh não propaga `has_image`/attachments à resolução do LLM, ou `detect_image_in_attachments` não casou o formato interno do anexo. **Reproduzível E2E; root-cause + fix = tarefa de código separada.**

**Impacto:** a plataforma anuncia "aceita imagens / multimodal" (toggle `accepts_images`, task `instruct` diz "Aceita imagens"), mas o fluxo real de análise de imagem retorna "nenhuma imagem enviada". **Bloqueia também o teste do 402 F6 ao vivo** (esse caminho seria a fonte de custo Azure > 0).

**✅ ROOT-CAUSE + FIX (2026-07-08, v29.0.2, branch `fix/api-invoke-image-base64-dropped`):**
- **Causa REAL (provada por isolamento + log de runtime):** o decoder `_decode_attachments` (`app/routes/agents.py`, usado pelo invoke de `/agents` e `/pipelines`) montava o anexo interno só com `{name, type, size, content}` — **descartava os bytes/base64 da imagem**, guardando apenas o texto markitdown (`"ImageSize: 64x64"` ≈ 16 chars). Como `_attachment_image_data_url` (engine) lê `content_base64`/`image_b64`/`abs_path`, e nenhum estava presente, o `_build_user_message_content` caía no ramo text-only e **descartava a imagem MESMO com o modelo roteado corretamente pro `azure/gpt-4o`**. O caminho **workspace/UI não sofria** porque grava o arquivo e passa `abs_path` (por isso "funcionava na tela"). O swap `has_image → azure/gpt-4o` sempre funcionou (confirmado no log: `Onda 7 routing: ... has_image=True → azure/gpt-4o`); o defeito era só a perda do base64.
- **Fix:** `_decode_attachments` passa a incluir `content_base64` para anexos de imagem (documentos seguem só com `content` textual).
- **Verificado ao vivo (docker local, v29.0.2):** o log passou de imagem-descartada para **`mesh.vision.images_attached … provider:azure model:gpt-4o image_count:1 decision:"attached"`** — a imagem agora chega ao modelo de visão. Teste de regressão `TestDecodeAttachmentsPreservesImageBytes` em `tests/test_multimodal_vision.py` (falha sem o fix, passa com). **Pendente: deploy do #PR na VPS + reexecutar o Cenário H (com Azure alcançável → resposta de visão real, e possível fonte de custo p/ o 402 F6).**
- **Observação separada (NÃO é este bug):** subagentes downstream sem `accepts_images` recebem `has_image=False` (gpt-oss) — é o design do dispatcher (forwarding opt-in por capacidade). Só o SA que declara `accepts_images` recebe a imagem.

---

### Cenário I — Avaliação (Harness §9.5) + Observabilidade/Qualidade
- **Objetivo:** rodar o **Harness de Avaliação** contra casos gold de um agente; inspecionar **Observabilidade/Qualidade/Histórico** (traces, audit, atribuição, alucinações).
- **Exercita:** harness (accuracy/factuality/tone/gate multi-dimensional), **LLM-as-Judge**, **Avaliação (pré-deploy)**, **Qualidade (produção)** com auditoria/re-julgar A/B/explorador de alucinações/export, **Histórico**, guard de amostra pequena.
- **Passos:** usar a demo `demo-v1` (9 casos) ou criar gold cases; rodar eval; abrir Qualidade e Observabilidade.
- **Observar:** o gate combina as dimensões? guard de amostra pequena aparece ("provisório")? dá pra re-julgar e exportar? traces por-key/por-pipeline?

---

## 4. Backlog consolidado de melhorias (do Cenário A) — para o relatório do produto

**Corrigir (código):**
1. 🔴 (feito #518) parser `evidence_policy`/`output_shape` — fence + trailing content. **Deploy na VPS pendente.**
2. 🟠 `+ Novo` pipeline → trocar `window.prompt()` por **modal in-page** com validação de nome.
3. 🟠 Tradutor NL→regra condicional → considerar o **Output Contract** da skill anterior (evitar `is_refuse` para "sentimento negativo").
4. 🟠 Card "Conteúdo" → adicionar **"contém o texto X"** (ou renomear; hoje só link/PDF/imagem) e/ou mesclar com "Palavra-chave".
5. 🟡 Grounding: revisar comportamento com `evidence_count:0` + `require_evidence:1` (recusa clara vs pedir "acesso à base" ao cliente). **Revalidar após #518.**

**UX / polimento:**
6. 🟠 Criar KB em 1 passo (ingerir no mesmo modal).
7. 🟡 Editor SKILL.md: manter spinner durante a geração (não piscar template vazio).
8. 🟡 Wizard skill/agente: `<select>` nativos → confirmar onChange no uso humano; considerar dropdown do design system.
9. 🟡 Roteamento condicional: guiar a criação de uma rota **default (else)** para o caso NEUTRO (senão fica órfão).
10. 🟡 `entry_agent_id` null no GET quando a entrada é auto-detectada — materializar/expor para observabilidade.
11. 🟡 Progresso/ETA nas gerações longas (gpt-oss reasoning ~20s).
12. 🟡 Limpeza/arquivamento de KBs de teste vazias (clutter "asdf").
