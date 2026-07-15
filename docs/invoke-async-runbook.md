# Runbook — Ativação do Invoke Assíncrono (202) em produção

> Onda 6 está 100% codada (job store durável, idempotência, reaper, webhook,
> métricas RED). Este runbook cobre o último degrau: **ligar
> `invoke_async_enabled` e validar em produção**. Nenhuma migração manual é
> necessária — tabela `invoke_jobs`, índices e colunas aplicam no `init_db()`
> do boot (idempotente).

## 1. Pré-checks (antes de ligar)

| Check | Como | Por quê |
|---|---|---|
| Fila acumulada | `GET /api/v1/pipelines/{pid}/jobs?status=queued` nos pipelines ativos | Jobs enfileirados com a flag OFF **executam e pagam LLM** assim que religar (o reaper despacha até 50/tick) |
| `MAESTRO_SECRET_KEY` presente | conferir env do container | Webhook de jobs criados por cookie/UI assina com ela; sem ela o fallback é a string fraca `maestro-webhook` (já obrigatória p/ Federação A2A na VPS) |
| Acesso root/admin | login na UI | `GET/PUT /settings` são role-gated |
| Single-worker | confirmar uvicorn com 1 worker | O zumbi-check do reaper usa set in-process como autoridade — multi-worker marcaria jobs legítimos como `lost` |

## 2. Ativação

Configurações → aba **Parâmetros** → grupo **“🔁 Invoke assíncrono (202)”** →
ligar **“Habilitar invoke assíncrono”** → Salvar.

- Efeito é em **runtime, sem restart** (`apply_settings_to_env` + cache clear).
- Mudar **só** essa chave (footgun histórico do PUT /settings; `exclude_unset`
  protege desde #422, mas a disciplina continua).
- Conferir a trilha: `audit_log` com `action=settings_saved`.
- Prova imediata: `POST /api/v1/pipelines/{pid}/invoke/async` deixa de
  responder `403 invoke_async_disabled`.

Parâmetros vizinhos (defaults): retenção de terminais **72h**, concorrência
**4**, deadline por job **30 min**.

## 3. Checklist de validação

1. **Happy path**: `POST /invoke/async` → `202` + `Location` + `Retry-After: 2`
   → polling `GET /{pid}/jobs/{job_id}` até `completed` com `result`.
2. **Idempotência**: repetir com o mesmo `Idempotency-Key` + mesmo corpo →
   `200` com o MESMO `job_id`; corpo diferente → `409 idempotency_key_reuse`.
   Sem key, retry de proxy cria job duplicado (e paga LLM) — documentado, por
   isso integrações devem SEMPRE enviar o header.
3. **Webhook**: receptor de teste → payload leve (sem `result`), assinatura
   HMAC-SHA256 em `X-Maestro-Signature` verificável; derrubar o receptor na 1ª
   tentativa → retry (3x, backoff 1s/2s/3s); URL interna → 400 (SSRF guard).
   Webhook é **best-effort sem fila durável**: polling é a fonte de verdade.
4. **Deadline**: job acima de `invoke_job_timeout_minutes` → `failed` por
   timeout, vaga liberada, custo parcial no ledger.
5. **Restart no meio**: `running` órfão vira `lost` (nunca re-executa);
   `queued` retoma; `lost` notifica webhook.
6. **Kill-switch**: desligar a flag congela a fila (queued fica queued;
   higiene continua); religar retoma o despacho — tudo sem restart.
7. **Concorrência**: com cap 4, o 5º job fica `queued` e entra em ~60s
   (tick do reaper).
8. **Retenção**: terminal com mais de 72h é apagado pelo reaper — polling
   atrasado recebe `404` (integrações devem buscar o resultado em até 72h).
9. **IDOR**: `GET` de job de outro dono → `404` idêntico ao inexistente;
   leitura por API key respeita escopo por-key e projeção de verbosidade.
10. **RED**: séries `maestro_invocations_total{kind="invoke_async"}`,
    `..._errors_total` e `..._duration_seconds` em `GET /metrics`; painel
    “Rate — invocações/s por caminho” no Grafana já plota o caminho.
11. **LGPD**: invoke com `customer_ref` → `forget` do titular alcança o job
    (`invoke_jobs.customer_hash`).

## 4. Limitações conhecidas (por design)

- **Webhook sem retry durável**: restart no meio das 3 tentativas perde a
  notificação; o payload traz `status_url` e o cliente deve pollar.
- **Retenção 72h apaga o `result_payload`** — silencioso para quem polla tarde.
- **Não escalar para multi-worker** sem revisitar o reaper (ver pré-check).
- A suíte da Onda 6 roda contra FakePool — a validação em produção é a
  primeira execução real do SQL de claim/reaper contra asyncpg (mesma classe
  de risco das SQLs LGPD). Se surgir gap, abrir PR de fix isolado por achado.
