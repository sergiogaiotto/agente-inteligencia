# Onda 7 — LLM Routing por Task Type via LiteLLM

## Vision

Inverter o paradigma de seleção de LLM: em vez do operador escolher
**provider+model** diretamente, escolhe o **tipo de tarefa**
(Tool Calling / Reasoning / Instruct / Classification). A plataforma
resolve provider+model via tabela de roteamento configurável em
`/settings`. Roteamento default segue a heurística informada pelo
operador (Tool Calling=Azure, demais=Maritaca).

Reforço: nesta onda também acontece o cleanup do **OpenAI público**
— `OPENAI_API_KEY` é descontinuada em favor de `AZURE_OPENAI_API_KEY`.
A escolha "OpenAI" vira semanticamente "Azure OpenAI".

## Decisões do operador

1. **4 categorias** (sem custom em v1): Tool Calling, Reasoning,
   Instruct, Classification.
2. **Routing puro** (sem override manual no agent_form em v1).
3. **Defaults**:
   - Tool Calling → `azure/gpt-4o`
   - Reasoning → `maritaca/sabia-4`
   - Instruct → `maritaca/sabia-4`
   - Classification → `maritaca/sabia-4`
4. **LiteLLM permanece opt-in** (`LLM_GATEWAY_ENABLED=false` default).
   Roteamento atua mesmo sem gateway — fallback gracioso pra chamada
   direta usando o provider/model resolvido via routing.
5. **Multimodal** (imagem etc): Instruct precisa lidar com isso.
   Solução: novo campo `multimodal_fallback` no routing. Quando o
   resolver detecta imagem no input E o modelo resolvido é text-only,
   roteia pro fallback (default: `azure/gpt-4o`).
6. **Sabiá-4** já está catalogada — confirmado em
   [wizard.py:170](app/routes/wizard.py:170).

## Scope (5 waves)

| Wave | Entrega | Disruptivo? | Estimativa |
|------|---------|-------------|------------|
| **1** Backend foundation | módulo `llm_routing.py`, settings_store entries, endpoint GET/PUT, helper `resolve_llm_for_task`, catálogo enriquecido com `multimodal: bool` | Não | ~1h |
| **2** Settings UI | tab nova "Roteamento LLM" com 4+1 dropdowns + descrições | Não | ~45min |
| **3** Agent form refactor | schema agent ganha `task_type`, step 2 vira 4 cards de tarefa, save resolve provider/model | **Sim** | ~2h |
| **4** Engine integration | engine resolve LLM via task_type + detecta imagem → multimodal fallback; cleanup principal de OpenAI direto | Médio | ~1h |
| **5** Migration + Cleanup OpenAI | task_type='tool_calling' default pra agentes existentes; remove `openai_api_key` do config; preflight ajusta | Baixo | ~45min |

## Out of scope

- **Custom task types** (operador adicionando novos tipos). v1 fixo.
- **Override manual no agent_form** (escapar do routing). v1 sem isso.
- **Multi-step routing** (cadeia que escolhe modelo por step). Scope futuro.
- **Cost-aware routing** (escolhe mais barato quando qualidade equivalente).
- **A/B testing automático entre modelos**. Onda dedicada.

## Comportamento por LiteLLM gateway

| `LLM_GATEWAY_ENABLED` | `agent.task_type` | Comportamento |
|------|------|------|
| true | setado | LiteLLM roteia via config + auth central |
| true | None (legacy agent) | LiteLLM proxy do agent.llm_provider/model |
| false | setado | Plataforma resolve via routing settings, chama provider direto |
| false | None | Comportamento atual (chama provider direto sem routing) |

## Multimodal fallback (resolver pseudocódigo)

```python
async def resolve_llm_for_task(task_type, has_image=False):
    routing = await load_routing()  # cached
    resolved = routing.get(task_type, DEFAULT_ROUTING[task_type])
    provider, model = resolved.split("/", 1)
    if has_image and not is_multimodal(provider, model):
        fallback = routing.get("multimodal_fallback") or "azure/gpt-4o"
        provider, model = fallback.split("/", 1)
    return provider, model
```

Detecção `has_image`: engine olha `attachments` no input — se algum tem
`type` contendo "image" ou MIME `image/*`, ativa fallback.

`is_multimodal`: lookup numa lista hardcoded inicial. Modelos conhecidos
multimodais: `gpt-4o`, `gpt-4.1`, `claude-sonnet-4`, `gemini-2.5-pro`.
Atualizável manual conforme catálogo cresce.

## Files touched

| Arquivo | Wave | Tipo |
|---------|------|------|
| `app/llm_routing.py` | 1 | new |
| `app/routes/wizard.py` | 1 | edit (campo multimodal) |
| `app/routes/dashboard.py` ou novo router | 1 | edit (endpoints routing) |
| `app/templates/pages/settings.html` | 2 | edit (tab nova) |
| `app/core/database.py` | 3 | edit (coluna task_type em agents) |
| `app/templates/pages/agent_form.html` | 3 | edit (step 2 refactor) |
| `app/routes/agents.py` | 3 | edit (save resolve) |
| `app/agents/engine.py` | 4 | edit (resolve via task_type + cleanup OpenAI) |
| `app/core/config.py` | 5 | edit (remove openai_api_key) |
| `.env.example` | 5 | edit (remove OPENAI_API_KEY) |
| `app/agents/preflight.py` | 5 | edit (ajusta mapping) |
| `app/core/llm_providers.py` | 4-5 | edit (OpenAI vira alias Azure) |

## Risks

- **Schema migration de agents** (Wave 3): se algum agent não tem
  task_type, fallback pra llm_provider+model atual. Migration popula
  task_type='tool_calling' como default seguro.
- **Multimodal detection**: false positive (operador anexa imagem mas
  só quer descrição em texto, e o modelo text-only consegue) é raro
  mas possível. Mitigação: opcional override no agent_form em onda
  futura.
- **OPENAI_API_KEY removed mas ainda no `.env` real**: detecta no
  startup e loga warning explicando que vai usar Azure.

## Estimativa total

~5.5h trabalho focado, ~3 sessões. Fatiamento:
- Sessão atual: Wave 1 + Wave 2 (~2h, sem risco)
- Próxima sessão: Wave 3 + Wave 4 (~3h, disruptivo — testes manuais)
- Última sessão: Wave 5 + push (~45min, cleanup)
