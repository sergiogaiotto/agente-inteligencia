# Onda — Pre-flight check em agent create/edit

## Goal

Mover validação semântica de configuração de agente do **runtime de invoke** (onde o erro vai pro usuário final) para o **momento de save** (onde vai pra quem está configurando). Fail fast, fix fast.

## Why

Hoje o pipeline de validação é:

1. **Pydantic schema** ([schemas.py:5-32](app/models/schemas.py:5)): só types e ranges (length, regex, ge/le).
2. **Save** ([agents.py:29-62](app/routes/agents.py:29)): persiste tudo que passa pelo schema, sem checagem semântica.
3. **Invoke runtime** ([engine.py:630-635, 752](app/agents/engine.py:630)): captura provider sem API key, modelo 404, etc — mas a mensagem de erro vai pro **usuário final** que invocou, não pra quem configurou o agente.

Resultado: erros de configuração só aparecem quando alguém tenta usar o agente, e quem vê o erro não é quem pode consertar. Operador descobre que `OPENAI_API_KEY` está vazio porque um cliente reclamou — não porque o sistema avisou.

A onda inverte isso: 9 checks puros/locais rodam no `POST /preflight` (ou no save), retornam lista estruturada de problemas com fix hint. Step "Revisão" do form (que hoje é cosmético) ganha lista de checks acima do botão Salvar. Errors bloqueiam save; warnings/info informam.

## Scope

3 plans, 2 waves. Backend isolado primeiro (testável); UI consome depois.

| Wave | Plan | Entrega |
|------|------|---------|
| 1 | `01-PLAN-preflight-checks-backend.md` | Módulo `app/agents/preflight.py` com 9 checks; endpoint `POST /api/v1/agents/preflight`; integração nos saves (errors bloqueiam) |
| 2 | `02-PLAN-preflight-ui-review-step.md` | Lista de checks na step "Revisão"; auto-roda; botão Save desabilita com errors |

## Out of scope

- **Smoke call ao LLM** (1-token "ping" pra validar auth/model real). UX é delicada (loading, timeouts, rate-limit) e custa $$. Vira onda futura "Agent connection test".
- **Mesh validation** (cycles, kind alignment, dangling refs). Tem complexidade própria (BFS cycle detection, kind→kind compatibility matrix). Onda dedicada com painel próprio.
- **Validação síncrona on-blur** dos campos (model/provider/skill) com debounce. Polui DB com queries; pre-flight no save + ao entrar na revisão é suficiente.

## Decisões assumidas

Confirmadas pelo usuário em diálogo prévio.

1. **Errors bloqueiam save, sem override.** Power-user pode editar via PATCH direto. Warnings passam (com aviso). Info é só info.
2. **Sem smoke call ao LLM.** Onda futura.
3. **Sem mesh validation.** Onda futura.
4. **Pre-flight roda 2 vezes**: ao entrar na step Revisão (auto, com loader) e ao clicar Save (re-roda, bloqueia se mudou). Não roda em on-blur dos campos.
5. **Endpoint sem ID** (`POST /agents/preflight`) — recebe payload completo do form. Edit envia o estado atual do form. Mais simples que `POST /agents/{id}/preflight`.

## Lista de checks (9)

| Id | Check | Severidade | Custo |
|----|-------|------------|-------|
| C1 | API key configurada para `llm_provider` | error | settings only |
| C2 | `skill_id` aponta para SKILL.md que parseia OK | error | DB query |
| C3 | MCP tools do SKILL resolvem no Tools registry | warning | DB query |
| C4 | Output Contract parseia como JSON quando claim JSON | warning | string parse |
| C5 | Inputs declarados cobrem `{{inputs.X}}` dos api_bindings | warning | regex |
| C6 | System prompt não-trivial OU skill vinculada (anti pass-through) | warning | string check |
| C7 | Model bate com lista conhecida do provider | info | lookup local |
| C8 | Versão segue semver | info | regex |
| C9 | Temperature `> 1.5` | info | comparação |

## Must-haves (goal-backward)

A onda só está pronta quando:

- [ ] `POST /api/v1/agents/preflight` com payload de agente novo retorna `{checks: [...], blocked: bool}`.
- [ ] Provider sem API key (`OPENAI_API_KEY=""`) → check C1 com severity=error, blocked=true, fix_hint aponta pra `/settings`.
- [ ] Skill_id apontando pra ID inexistente → check C2 com severity=error, blocked=true.
- [ ] SKILL.md vinculado com YAML frontmatter quebrado → check C2 com severity=error.
- [ ] Skill com `## Tool Bindings` declarando "Tavily MCP Server" mas Registry sem essa entrada → check C3 com severity=warning.
- [ ] `POST /api/v1/agents` com payload que falha em C1 retorna 422 com lista de checks; não persiste o agente.
- [ ] `POST /api/v1/agents` com payload que tem warnings mas zero errors persiste normalmente.
- [ ] Step "Revisão" do form de agente mostra lista visual de checks com ícones por severidade, fix hint clicável quando aplicável.
- [ ] Botão "Salvar" desabilitado quando há check error; tooltip explica.
- [ ] Re-rodar preflight manualmente via botão "Verificar novamente".
- [ ] Edit de agente existente (`PUT /agents/{id}`) também passa pelo preflight.
- [ ] Pre-flight passa rápido (< 100ms para agente típico em DB local).

## Risks

- **C7 falsos positivos**: catálogo de modelos do provider muda. Mitigação: severity=info, não bloqueia. Lista local mantida em `model_catalog.py` simples; quando furar, atualizar.
- **C4 JSON strict**: contratos com comentários ou JS-style falham parse. Mitigação: tolerar — se parse normal falha, tentar `# remover comentários` antes de re-parsing; se ainda falha, warning suave.
- **Latência**: 2 DB queries serial (skill + tools_repo). ~30-50ms total. OK pra UI síncrona; se virar problema, paralelizar com `asyncio.gather`.
- **Schema drift entre `AgentCreate` e payload do preflight**: usuário pode mandar campos extras. Mitigação: usar `AgentCreate` como dependência do endpoint; Pydantic já filtra.

## Files touched

| Arquivo | Wave | Tipo |
|---------|------|------|
| `app/agents/preflight.py` | 1 | new |
| `app/routes/agents.py` | 1 | edit |
| `app/models/schemas.py` | 1 | edit (resposta do preflight) |
| `app/templates/pages/agent_form.html` | 2 | edit |
