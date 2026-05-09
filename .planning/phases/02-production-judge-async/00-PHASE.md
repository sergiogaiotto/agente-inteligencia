# Onda — Production Judge async (sample 5-10% de interações reais)

## Goal

Habilitar o Verifier multi-dim em produção rodando **assíncrono** numa **amostra (10% default)** das interações reais. Hoje `VERIFIER_V2_ENABLED=true` é caro/lento demais para tráfego real (1 chamada LLM extra + 2-4s de latência por interação). Esta onda introduz um modo de produção: dispatch em background depois de devolver a resposta, amostragem determinística por hash do `interaction_id`, backpressure por capacidade, drenagem em shutdown.

## Why

- **Visibilidade impossível em produção hoje**: ou roda em 100% (caro/lento) ou em 0% (cego). Operador não tem dados sobre regressão de qualidade no tráfego real.
- **Harness é offline**: gate de release por Golden Dataset cobre o que prevemos. Não cobre o que a produção descobre na vida real.
- **`/quality` já tem a UI** para mostrar verifications — só precisa de gente populando a tabela em produção.
- **Custo controlado por sample rate**: 10% em 10k interações/dia ≈ 1k chamadas extras/dia. Ajustável.

## Scope

3 plans em 2 waves. **Wave 1** entrega o motor; **Wave 2** entrega visibilidade pro operador.

| Wave | Plan | Entrega |
|------|------|---------|
| 1 | `01-PLAN-async-dispatcher.md` | Módulo `app/verifier/async_dispatcher.py` com sampling hash-based, set de tasks pendentes, contadores in-process, drain hook |
| 1 | `02-PLAN-engine-integration.md` | Settings novos, novo branch async no chain do `verify_evidence`, lifespan drena pendentes |
| 2 | `03-PLAN-quality-ui-async-stats.md` | `/api/v1/dashboard/verifier/async-stats` + card no `/quality` mostrando sample rate, pending, completed, failed, dropped |

## Out of scope

- **Estratificação por estado** (100% Refuse/Escalate, 10% Recommend) — começamos uniforme. Tuning posterior.
- **Persistência cross-restart de stats** — contadores in-process. Cross-worker (uvicorn multi-worker) e cross-restart ficam para uma onda de telemetria com Redis/Prometheus.
- **OTel context propagation** entre interaction trace e async task — Wave 1 cria span próprio na task. Correlação por `interaction_id` (já vai pra `verifications.interaction_id`). Trace parent-child propagation fica para depois.
- **Filtros de sampling** (ex: só certas skills, só certos channels) — não desta onda.

## Decisões assumidas

Confirmadas pelo usuário em diálogo prévio. Documentadas aqui pra rastreabilidade.

1. **Hash-based sampling** (não `random.random()`). SHA256 dos primeiros 8 bytes do `interaction_id` → bucket `[0, 1)` → comparar com `sample_rate`. Mesma interação sempre tem o mesmo destino, mesmo entre deploys.

2. **Sample rate default = 0.10** (10%). Override por env `VERIFIER_PRODUCTION_SAMPLE_RATE`.

3. **Backpressure por contagem de tasks pendentes**, não por semaphore. Limite default `verifier_max_concurrent_jobs = 20`. Acima disso → drop sample (loga, não espera). Mais simples que semaphore com try-acquire.

4. **Sampling uniforme primeiro**. Estratificação por estado/skill é tuning posterior — quando tivermos dados pra justificar.

5. **Duas flags separadas**:
   - `verifier_v2_enabled` (já existe): habilita o motor.
   - `verifier_production_async` (nova): quando `true`, troca o branch síncrono por dispatch async com sampling.
   
   Back-compat: harness e dev seguem rodando síncrono se `verifier_production_async=false`.

6. **FSM no modo async** usa heurística rasa para `verify_evidence` (média de relevance_score das evidências, threshold 0.3) — mesma do fallback atual quando o verifier explode. Judge multi-dim vira **observabilidade pós-fato**, não decisão de runtime.

## Must-haves (goal-backward)

A onda só está pronta quando:

- [ ] Setando `VERIFIER_PRODUCTION_ASYNC=true` + `VERIFIER_V2_ENABLED=true`, uma interação no `/workspace`:
  - retorna a resposta com a mesma latência de antes do verifier (sem +2-4s do judge);
  - se foi sampleada, eventualmente cria um row em `verifications` table com `interaction_id` correto.
- [ ] Stress-test simples (50 interações em loop): nenhuma trava, nenhuma vaza Task, nenhuma perde row de DB pra task que terminou. Drops só quando excede o cap de concorrência.
- [ ] Mesma interação rodada 2x → mesmo destino (sampled ou não), por causa do hash.
- [ ] Stats endpoint `/api/v1/dashboard/verifier/async-stats` retorna `{sampled, completed, failed, dropped, pending}`.
- [ ] Página `/quality` mostra um card "Production Sample" com os 5 contadores + sample rate corrente + indicação visual quando `pending > 0` (em andamento).
- [ ] Shutdown limpo: `Ctrl+C` no app aguarda até 5s tasks pendentes terminarem, depois fecha. Sem traceback de "task was destroyed".
- [ ] Run com `VERIFIER_PRODUCTION_ASYNC=false` mantém comportamento síncrono atual. Zero regressão.

## Risks

- **Tasks órfãs em crash hard**: `kill -9` mata tudo, incluindo tasks. Aceitável — sample. Sem garantia at-least-once.
- **Memory leak por bug**: se `_pending_tasks.discard` não roda (callback não dispara), set cresce sem limite. Mitigação: callback no `add_done_callback`, garantia do asyncio. Smoke valida o cleanup.
- **Self-preference do judge**: se `verifier_judge_model == agent.model`, judge favorece o draft. Já discutido na onda passada; não muda nesta.
- **Stats cross-worker**: uvicorn com 4 workers tem 4 contadores separados — operador vê só o do worker que respondeu o GET. Documentar. Solução cross-worker fica para onda de Prometheus.
- **Cost spike por config errada**: alguém sobe `VERIFIER_PRODUCTION_SAMPLE_RATE=1.0` sem perceber. Mitigação: log de warning quando rate > 0.5 no startup; card de stats deixa o rate visível.

## Files touched (resumo)

| Arquivo | Wave | Tipo |
|---------|------|------|
| `app/verifier/async_dispatcher.py` | 1 | new |
| `app/agents/engine.py` | 1 | edit |
| `app/core/config.py` | 1 | edit |
| `app/main.py` | 1 | edit (lifespan drain) |
| `app/routes/dashboard.py` | 2 | edit (stats endpoint) |
| `app/templates/pages/quality.html` | 2 | edit (card) |
