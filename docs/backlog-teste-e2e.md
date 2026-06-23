# Backlog — correções e melhorias do teste E2E (como usuário)

> Origem: sessão de teste ponta-a-ponta da plataforma como **usuário root**, no deploy
> `http://vps.falagaiotto.com.br:8080` (v15.3.0), em 2026-06-23. Conduzido via navegador
> headless (Playwright) + chamadas de API com o cookie de sessão.
>
> Este documento é o **ponto de partida** para a próxima sessão: o que já foi corrigido,
> o que ainda falta (código + config), e como retomar o teste.

## ✅ Validado e funcionando (não mexer)

Login, **23/23 telas** carregam sem erro de JS; Guia Interativo (21 módulos / 22 ajudas);
Workspace (FSM completa + tool MCP + trace + XLSX); criação de agente (wizard UI) e skill
(editor manual); **pipeline selado** (criar → conectar → invocar pela UI → publicar);
**RAG** ponta-a-ponta (ingest → embed/pgvector → retrieval grounded); regras condicionais
(Galeria 23 vars + Simulador); **governança do Catálogo** (submit → fila → decidir); CRUD
do Golden Dataset; rate-limit (60/min) atuando.

## ✅ Já corrigido — PR #427 (15.3.1)

| ID | O que | Arquivo |
|----|-------|---------|
| C1b | Wizard de skill cai no fallback (azure) quando `skill_generation` dá 401 (antes 500) | `app/routes/wizard.py` |
| C3 | `POST /catalog/entries/{id}/archive` (deprecated/draft → archived) — permite limpar entry publicada | `app/routes/catalog.py` |
| C4 | `eval-runs/execute` valida release/agente → 404 (não cria mais eval_run "lixo") | `app/routes/dashboard.py` |
| C6 | Tradutor NL→Jinja auto-quota literais (`pix` → `'pix'`) antes de validar | `app/agents/conditional_suggest.py`, `app/routes/mesh.py` |
| C7 | Pré-check do Catálogo mostra `ok`/`n/a` quando passa (não o texto de falha) | `app/catalog/prechecks.py` |

---

## ⏳ PENDENTE — CONFIG no deploy (não é código)

> Feito direto no ambiente, em **Configurações** (UI) ou variável/DB. Não trocar Settings
> via API sem cuidado: `PUT /settings` sem `exclude_unset` já zerou segredos antes
> (footgun conhecido).

- [ ] **C1 · Chave OpenAI do `skill_generation` inválida (401).** Modelo
  `openai_public/gpt-4.1` retorna *"Incorrect API key: sk-proj-…"* — é o "1" âmbar do chip.
  **Ação:** atualizar a API key OpenAI pública em Configurações → Plataforma, **ou**
  repontar o papel `skill_generation` para um provider saudável (gpt-oss/azure) em
  Configurações → Roteamento LLM. *(Com o C1b já cai no fallback, mas a causa raiz fica.)*
- [ ] **C2 · Verifier multi-dim desligado** (`VERIFIER_V2_ENABLED=false`) → tela
  **Qualidade vazia**, sem checagem de factuality/safety/contrato nas respostas.
  **Ação:** ligar `VERIFIER_V2_ENABLED=true` e setar `VERIFIER_JUDGE_MODEL` para um
  modelo saudável (ex.: azure/gpt-4o) se quiser o quality-gate em produção.
- [ ] **O3 · Rotar a senha do usuário `sergio.gaiotto`** (apareceu em transcript de chat).
- [ ] **O4 · Apagar o `eval_run` "lixo"** criado pelo teste de validação
  (`id 0ed9d67f-da1e-4feb-8411-d847838e351d`, accuracy 0.0). Sem DELETE de eval_runs →
  remover via DB (`DELETE FROM eval_runs WHERE id = '0ed9d67f-...'`). *(C4 evita novos.)*

## ⏳ PENDENTE — CÓDIGO (próximo(s) PR)

- [x] ~~**C5 (P2, UI) · Publicar pipeline no Catálogo usa `prompt()` nativo**~~ →
  ✅ **Feito no PR #428 (15.4.0).** Modal in-app (Alpine) em `mesh_flow.html` com campo
  de versão + validação semver visível + erro do servidor inline; estado legado
  `publishingCatalog` removido; teste de varredura trava a regressão.
- [ ] **C8 (P2, investigar) · Agente "Pesquisar Internet" retorna `FONTE: Simulada`.** A
  busca web parece **simulada/canned** (não real via Tavily MCP).
  **Ação:** confirmar se o Tavily MCP está retornando dados reais ou se é modo demo; se for
  pra valer, corrigir a integração / credencial do MCP.
- [ ] **O1 (P3, segurança) · Cookie de sessão `user_id` = UUID cru, sem assinatura** → quem
  souber o valor está autenticado. **Ação:** considerar token assinado (HMAC) ou sessão
  server-side. Toca `app/routes/frontend.py` (`_get_user`), `app/core/auth.py`
  (`require_user`), e a emissão do cookie no login (`app/routes/users.py`). Mudança com
  blast-radius — exige migração compatível (aceitar cookie antigo durante transição).
- [ ] **O2 (P3, cosmético) · `GET /api/v1/rag/health` usa o campo legado
  `qdrant_collection`** (o backend é pgvector desde a Onda Q).
  **Ação:** renomear para `pgvector_collection` (ou `vector_store`), com cuidado com
  consumidores que esperam a chave antiga (UI de `/infra`).
- [ ] **C9 (P3, backend, pré-existente) · `from-pipeline` pode dar 422 se o nome do
  pipeline não produz slug** (nome 100% não-ASCII/emoji → `slugify` vazio → `make_urn`
  levanta → HTTP 422 "URN inválido"). Achado na revisão do C5; o erro **já aparece inline**
  no modal, então não é silencioso. **Ação (opcional):** em `app/routes/catalog.py` (~L238-240),
  cair num slug determinístico derivado do `pipeline_id` quando `slugify(name)` vier vazio.
- [ ] **(P3, operacional) · Sem DELETE para `releases` e `eval_runs`** (só `gold-cases` tem).
  **Ação:** avaliar adicionar `DELETE /api/v1/releases/{id}` e `DELETE /api/v1/eval-runs/{id}`
  (ou um archive), para permitir limpeza — hoje uma release/eval de teste fica irremovível.

## 🚀 Deploy

As correções do #427 **só entram no `vps.falagaiotto`** após **merge do #427** + **redeploy**
do VPS (pull da imagem/código novo + restart). O footer da UI deve passar de **15.3.0** para
**15.3.1** quando o deploy estiver atualizado.

## 🔁 Como retomar o teste E2E

- **Auth:** o app exige login (cookie `user_id` = UUID do usuário). O assistente **não digita
  senha** (regra de segurança) — o usuário loga e fornece o valor do cookie `user_id`
  (DevTools → Application → Cookies). Injetar via Playwright:
  `context.add_cookies([{ "name": "user_id", "value": "<uuid>", "url": "<base>" }])`.
- **Driver:** navegador headless via Playwright (`python -m playwright install chromium`),
  persistindo `storage_state` entre passos. Rate-limit de 60/min → **pacear** as requisições.
- **Convenção:** prefixar entidades de teste com `TESTE-` e **limpar no fim** (DELETE de
  agentes/pipelines/conexões/bases/entries; para entry publicada: deprecate → archive → delete).
