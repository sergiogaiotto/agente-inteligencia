---
wave: 1
depends_on: [01-PLAN-async-dispatcher.md]
files_modified:
  - app/core/config.py
  - app/agents/engine.py
  - app/main.py
autonomous: true
estimated_diff_lines: ~80
---

# Plan 02 — Engine integra dispatcher async + lifespan drain

## Objective

Adicionar 3 settings novos e um novo branch no chain de `verify_evidence` do `execute_interaction` que dispara dispatch async com sampling. FSM segue com heurística rasa pra não bloquear resposta. Lifespan do FastAPI drena tasks pendentes antes de fechar o pool.

## Why

Os settings precisam existir antes do branch lê-los. O branch precisa ficar **antes** do branch síncrono no chain (precedência: async sample > sync). O drain no lifespan é o que evita perda de samples em shutdown limpo.

## Tasks

<task id="1" type="edit">
<file>app/core/config.py</file>
<location>seção verifier (linha ~108-120, depois de `verifier_max_tokens`)</location>
<change>
Adicionar:

```python
# ── Verifier production mode (async sampling) ──
# Quando True, o branch verifier do engine não bloqueia mais a resposta:
# {sample_rate}% das interações são julgadas em background. Resposta
# segue com heurística rasa em verify_evidence (evidence_score-based).
# Útil em produção — 100% sync é caro e lento.
verifier_production_async: bool = False
verifier_production_sample_rate: float = 0.10  # 10%
verifier_max_concurrent_jobs: int = 20  # backpressure
```
</change>
<acceptance>
- 3 settings novos. Override por env (VERIFIER_PRODUCTION_ASYNC, etc).
- Defaults conservadores: production_async=False (zero risco até alguém ligar).
</acceptance>
</task>

<task id="2" type="edit">
<file>app/agents/engine.py</file>
<location>chain do verify_evidence (linha ~770-820), entre `if pipeline_context...` e `elif _pg_settings.verifier_v2_enabled:`</location>
<change>
Inserir novo branch ANTES do branch sync existente:

```python
elif _pg_settings.verifier_v2_enabled and _pg_settings.verifier_production_async:
    # ─── Production sample async (§14.2) ─────────────────────────
    # Não bloqueia a resposta. Amostra X% das interações; tasks
    # pendentes drenadas no shutdown (lifespan).
    from app.verifier.async_dispatcher import dispatch as _dispatch_async, should_sample
    if should_sample(ctx.interaction_id, _pg_settings.verifier_production_sample_rate):
        _dispatch_async(
            draft=draft,
            evidences=evidences,
            output_contract=skill_data.get("output_contract") or "",
            guardrails=skill_data.get("guardrails") or "",
            user_question=user_input,
            profile=exec_profile,
            interaction_id=ctx.interaction_id,
            max_concurrent=_pg_settings.verifier_max_concurrent_jobs,
        )
    # FSM precisa decidir agora — heurística rasa (mesma do fallback).
    avg_score = (sum(e.relevance_score for e in evidences) / len(evidences)) if evidences else 0.5
    await fsm.run_verify_evidence({"ok": avg_score >= 0.3, "confidence": avg_score})
    # verification dict no result fica None — judge é pós-fato.
elif _pg_settings.verifier_v2_enabled:
    # ─── Verifier v2 síncrono (modo dev/harness) ──
    ...
```

A ordem importa: o branch novo deve aparecer **antes** do `elif _pg_settings.verifier_v2_enabled:` puro. Caso contrário, com ambas flags True o branch sync ganha e bloqueia a resposta.

`verification` na função pai segue `None` — o `_serialize_verification(None)` em `_build_result` produz `verification=None` no dict do result, que é o comportamento correto (sample async é pós-fato; nem o request response, nem o /workspace, têm visibilidade dela).
</change>
<acceptance>
- Branch novo entra ANTES do sync.
- `verification` permanece `None` quando entra no branch async.
- Sem flag, comportamento idêntico ao de hoje (zero regressão).
- Com flag e sample positivo, dispatch é chamado mas não awaited.
- Heurística do FSM permanece simples (avg evidence_score >= 0.3).
</acceptance>
</task>

<task id="3" type="edit">
<file>app/main.py</file>
<location>função `lifespan`</location>
<change>
Antes de `await close_db()` no finally, drenar tasks pendentes:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    try:
        yield
    finally:
        # Drena tasks async do verifier antes de fechar o pool — evita
        # AttributeError em INSERT contra pool já fechado.
        try:
            from app.verifier.async_dispatcher import drain
            await drain(timeout=5.0)
        except Exception as e:
            logger.warning(f"verifier drain falhou no shutdown: {e}")
        await close_db()
```

Adicionar `import logging` no topo se já não estiver, e `logger = logging.getLogger(__name__)` se também faltar (já costuma ter — verificar).
</change>
<acceptance>
- Shutdown com tasks pendentes aguarda até 5s.
- Shutdown sem pendentes não trava (drain retorna 0 imediato).
- Falha do drain não impede close_db.
</acceptance>
</task>

<task id="4" type="edit">
<file>app/core/config.py</file>
<location>final do método `__init__` ou validador da Settings (se existir)</location>
<change>
Adicionar warning de log quando `verifier_production_sample_rate > 0.5` no startup. Pode ser via Pydantic validator OU log explícito ao primeiro `get_settings()`:

```python
# No final do arquivo, após class Settings:
def _maybe_warn_high_sample_rate(s: Settings) -> None:
    """Log de aviso quando rate > 50% — defesa contra config errada."""
    import logging
    if s.verifier_production_async and s.verifier_production_sample_rate > 0.5:
        logging.getLogger(__name__).warning(
            f"VERIFIER_PRODUCTION_SAMPLE_RATE={s.verifier_production_sample_rate} "
            "está alto (>50%). Custo de LLM extra pode ser proibitivo. "
            "Considere reduzir se isso não for intencional."
        )

# Wrap get_settings com o warning na primeira chamada:
_settings_warned = False

@lru_cache()  # já existe — não duplicar
def get_settings() -> Settings:
    s = Settings()
    global _settings_warned
    if not _settings_warned:
        _maybe_warn_high_sample_rate(s)
        _settings_warned = True
    return s
```

NOTA: o `get_settings` atual já tem `@lru_cache`, então só executa o warning uma vez. Verificar a estrutura exata antes de editar.
</change>
<acceptance>
- Log de WARNING aparece quando rate > 0.5 no boot do app.
- Não loga em runs normais.
- Não loga 2x.
</acceptance>
</task>

## Verification

- [ ] `VERIFIER_V2_ENABLED=true VERIFIER_PRODUCTION_ASYNC=true VERIFIER_PRODUCTION_SAMPLE_RATE=1.0` → toda interação amostra; `verifications` table cresce em background sem afetar latência da resposta.
- [ ] `VERIFIER_V2_ENABLED=true VERIFIER_PRODUCTION_ASYNC=true VERIFIER_PRODUCTION_SAMPLE_RATE=0.0` → nenhuma interação amostra; `verifications` não cresce.
- [ ] `VERIFIER_V2_ENABLED=true VERIFIER_PRODUCTION_ASYNC=false` → comportamento síncrono original (latência igual ao Wave 1 da onda anterior).
- [ ] Shutdown com `Ctrl+C` durante interação amostrada → drain log aparece, sem traceback.
- [ ] Setando `VERIFIER_PRODUCTION_SAMPLE_RATE=0.8` no env → log warning no startup.

## must_haves

- Branch novo NÃO modifica `verification` que é retornado em result (continua None).
- Heurística para FSM no modo async = mesma do fallback atual quando o verifier explode.
- `verification = None` no result quando modo async — Plan 02 da onda anterior já lida com isso (Harness não conta esses casos no judge_used).

## Notes

- O modo async ignora completamente `result["verification"]` — o judge é pós-fato. O `/quality` page lê de `verifications` table direto, não do response do engine. Funcional.
- Se em produção alguém quiser ver a verification da interação atual no `/workspace`, precisa esperar a task drenar — mas como o `/workspace` é dev/test, esse caso é irrelevante (lá usa-se sync).
- `enrich_input` e o resto do fluxo do engine não mudam — só o branch de verify_evidence.
