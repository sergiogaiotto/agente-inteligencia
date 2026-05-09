---
wave: 1
depends_on: []
files_modified:
  - app/agents/engine.py
autonomous: true
estimated_diff_lines: ~40
---

# Plan 01 — Engine expõe `verification` no resultado

## Objective

`execute_interaction` deve devolver, em todo retorno bem-sucedido, um campo `verification` no dict-resultado com as dimensões do Verifier v2 (factuality, completeness, tone, safety), `ok`, `confidence`, `contract_compliant`, `unsupported_claims`, `judge_model`, `duration_ms`. Hoje essa informação só vai para `verifications` table e para o FSM em forma reduzida (`{ok, confidence}`).

## Why

- Sem isso, o Harness (Plan 02) precisaria reler `verifications` por `interaction_id` — frágil quando persistência falha (engine.py:184-186 captura silenciosamente).
- O `/workspace` ganha visibilidade imediata das dimensões no trace (sem mudar UI agora; o campo fica disponível para uso futuro).
- Não muda nenhuma API pública (apenas adiciona campo opcional ao dict-resultado).

## Tasks

<task id="1" type="edit">
<file>app/agents/engine.py</file>
<location>linha ~768-793 (bloco `elif _pg_settings.verifier_v2_enabled:`)</location>
<change>
Capturar o `VerificationResult` retornado por `_verifier.verify(...)` em variável local de escopo da função (não apenas dentro do `try`). Hoje a variável `verification` só existe dentro do `try` e some no `except`.

Após o try/except, garantir que `verification` está definida (mesmo que como `None` em caso de fallback) e propagar para `_build_result` via novo parâmetro keyword.
</change>
<acceptance>
- `verification` é instância de `VerificationResult` ou `None`.
- Em caso de exceção do verifier, `verification = None` (e o fallback heurístico atual continua rodando para o FSM).
- Em caso de `verifier_v2_enabled=false`, `verification = None`.
- Em caso de `pipeline_context or skip_evidence`, `verification = None` (não rodou o verifier).
</acceptance>
</task>

<task id="2" type="edit">
<file>app/agents/engine.py</file>
<location>função `_build_result` (linha ~839)</location>
<change>
Adicionar parâmetro keyword `verification: VerificationResult | None = None`.

No dict de retorno, adicionar:
```python
"verification": _serialize_verification(verification),
```
onde `_serialize_verification` é uma função módulo-nível (nova) que converte `VerificationResult` em dict serializável (compatível com JSON/HTTP):
- `None` → `None`
- caso contrário → dict com chaves: `ok`, `confidence`, `dimensions` (4 dimensões com `score` e `reason`), `contract_compliant`, `contract_errors`, `unsupported_claims`, `judge_model`, `duration_ms`, `risk_high`.

Não incluir `issues` (já usado no FSM) nem `fraud_suspected` (reservado).
</change>
<acceptance>
- `result["verification"]` é dict ou `None`.
- Quando dict, contém todas as chaves listadas e nenhuma a mais.
- Tipos serializáveis (sem dataclass cru, sem dataclass aninhada).
</acceptance>
</task>

<task id="3" type="edit">
<file>app/agents/engine.py</file>
<location>chamada de `_build_result` em `execute_interaction` (todas as 3 ocorrências)</location>
<change>
Passar `verification=verification` em todas as chamadas de `_build_result` (incluindo os early returns por API key faltando e por policy_check rejeitado — nesses casos passa `None`).
</change>
<acceptance>
- Todas as chamadas a `_build_result` passam o kwarg.
- Early returns mantêm `verification=None`.
</acceptance>
</task>

<task id="4" type="edit">
<file>app/agents/engine.py</file>
<location>função `_build_result`, dentro do dict `trace`</location>
<change>
Adicionar item ao `diagnostics` quando `verification` está presente:
- `ok=true` + factuality≥4 → `{"level": "success", "text": "Verifier: factuality {score}, ok"}`
- `ok=false` + qualquer dimensão abaixo de threshold → `{"level": "warning", "text": "Verifier: {motivo}"}`
- `unsupported_claims` não vazio → `{"level": "warning", "text": "{N} claim(s) sem suporte de evidência"}`

Esses diagnostics aparecem no painel do `/workspace` sem precisar mudar template.
</change>
<acceptance>
- Diagnostics existentes preservados.
- Nada quebra quando `verification=None`.
</acceptance>
</task>

<task id="5" type="test">
<file>tests/test_engine_verification_exposure.py</file>
<location>novo arquivo (criar `tests/__init__.py` se não existir)</location>
<change>
Teste de unidade focado em `_serialize_verification` (puro, sem rodar engine inteiro):

1. `_serialize_verification(None)` retorna `None`.
2. `_serialize_verification(VerificationResult(...completo...))` retorna dict com chaves esperadas, sem campos extras como `issues`.
3. `_serialize_verification(VerificationResult(...mínimo...))` (apenas `ok=False`) retorna dict com `dimensions={}`, `unsupported_claims=[]`.
</change>
<acceptance>
- 3 testes passam.
- Não depende de DB/LLM.
</acceptance>
</task>

## Verification (acceptance criteria do plan)

- [ ] `result = await execute_interaction(...)` num agente com `VERIFIER_V2_ENABLED=true` retorna `result["verification"]` populado.
- [ ] `result["verification"]["dimensions"]["factuality"]["score"]` é float ou `None`.
- [ ] `result["verification"]` é `None` para agentes em pipeline (`pipeline_context` setado).
- [ ] Smoke test: rodar uma interação no `/workspace` e ver no JSON do trace o novo campo (via DevTools).
- [ ] `pytest tests/test_engine_verification_exposure.py` passa.
- [ ] Nenhum teste existente regrediu.

## must_haves (goal-backward)

- O Plan 02 (harness) consegue ler `result["verification"]` direto, sem reler `verifications` table.
- Nenhuma mudança de assinatura quebra callers existentes (`/api/v1/interactions/execute`, pipeline, mesh).

## Notes

- `VerificationResult` está definido em [app/verifier/runtime.py:32-48](app/verifier/runtime.py:32). Tem 11 campos hoje — só os 7 mais relevantes vão para o trace HTTP.
- O `_LegacyVerifier` (quando `VERIFIER_V2_ENABLED=false`) também retorna `VerificationResult` mas com `dimensions={}` — `_serialize_verification` precisa lidar com isso (dict vazio é OK).
- Não tocar no schema de `verifications` table.
- Não tocar no FSM (`fsm.run_verify_evidence` continua recebendo o dict reduzido como hoje).
