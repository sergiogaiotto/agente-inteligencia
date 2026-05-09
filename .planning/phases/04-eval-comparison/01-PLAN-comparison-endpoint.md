---
wave: 1
depends_on: []
files_modified:
  - app/routes/dashboard.py
autonomous: true
estimated_diff_lines: ~120
---

# Plan 01 — Endpoint `GET /eval-runs/compare`

## Objective

Implementar `GET /api/v1/eval-runs/compare?a=<id>&b=<id>` que recebe 2 IDs, carrega ambos `eval_runs`, valida pré-condições (existência, status, gold_version), e retorna dict com:
- `run_a`, `run_b`: sumário leve (id, run_type, gate_result, accuracy, dimensions, judge_model, gold_version, total_cases, created_at).
- `comparable`: bool. `comparable_reason`: string explicando quando false.
- `deltas`: métricas agregadas (b - a) com sinal "is_improvement" por métrica.
- `by_category_deltas`: dict por categoria com deltas das 3 dimensões + accuracy.
- `divergent_cases`: até 20 cases com flip de `passed`, ordenados regressões → melhorias.

## Why

Backend isolado primeiro porque:
1. Endpoint pode ser smoked sem UI (curl direto).
2. UI da Wave 2 só consome o response — quanto mais estável o contrato, menor o churn de template.
3. A lógica de validação + cruzamento de cases é pura — testa fácil em smoke.

## Tasks

<task id="1" type="edit">
<file>app/routes/dashboard.py</file>
<location>seção "Harness §9.5", após `list_eval_runs` (linha ~563)</location>
<change>
Adicionar 4 helpers + 1 endpoint:

```python
# ── Comparação side-by-side §9.5 ───────────────────────────────────

# Cada métrica tem direção: 'up' = maior é melhor; 'down' = menor é melhor.
# UI usa pra escolher cor (green vs rose) do delta.
_METRIC_DIRECTIONS = {
    "accuracy": "up",
    "accuracy_unweighted": "up",
    "avg_factuality": "up",
    "avg_completeness": "up",
    "avg_tone": "up",
    "contract_compliance_rate": "up",
    "correct_refusal_rate": "up",
    "safety_violation_rate": "down",
    "hallucination_rate": "down",
    "false_positive_rate": "down",
    "avg_latency_ms": "down",
}


def _summary_of_run(run: dict) -> dict:
    """Sumário leve de um eval_run para o response (sem details cruas)."""
    return {
        "id": run.get("id"),
        "release_id": run.get("release_id"),
        "run_type": run.get("run_type"),
        "gold_version": run.get("gold_version"),
        "status": run.get("status"),
        "gate_result": run.get("gate_result"),
        "gate_reason": run.get("gate_reason"),
        "judge_used": bool(run.get("judge_used")),
        "judge_model": run.get("judge_model"),
        "total_cases": run.get("total_cases"),
        "passed": run.get("passed"),
        "failed": run.get("failed"),
        "accuracy": run.get("accuracy"),
        "avg_factuality": run.get("avg_factuality"),
        "avg_completeness": run.get("avg_completeness"),
        "avg_tone": run.get("avg_tone"),
        "safety_violation_rate": run.get("safety_violation_rate"),
        "contract_compliance_rate": run.get("contract_compliance_rate"),
        "hallucination_rate": run.get("hallucination_rate"),
        "correct_refusal_rate": run.get("correct_refusal_rate"),
        "false_positive_rate": run.get("false_positive_rate"),
        "avg_latency_ms": run.get("avg_latency_ms"),
        "created_at": run.get("created_at"),
    }


def _compute_delta(a: float | None, b: float | None, direction: str) -> dict:
    """Delta b-a com flag is_improvement quando ambos não-null."""
    if a is None or b is None:
        return {"a": a, "b": b, "delta": None, "is_improvement": None}
    delta = b - a
    is_improvement = (delta > 0) if direction == "up" else (delta < 0) if delta != 0 else None
    return {
        "a": a, "b": b,
        "delta": round(delta, 4),
        "is_improvement": is_improvement,
    }


def _aggregate_deltas(run_a: dict, run_b: dict) -> dict:
    """Deltas agregados pra todas métricas em _METRIC_DIRECTIONS."""
    return {
        m: _compute_delta(run_a.get(m), run_b.get(m), direction)
        for m, direction in _METRIC_DIRECTIONS.items()
    }


def _by_category_deltas(run_a: dict, run_b: dict) -> dict:
    """Deltas por categoria. Lê dimension_breakdown (já parseado por
    list_eval_runs ou parseia aqui). Categoria presente em apenas um
    dos lados aparece com null no outro."""
    def _cats(run):
        db = run.get("dimension_breakdown") or {}
        if isinstance(db, str):
            try: db = json.loads(db)
            except: db = {}
        return (db.get("by_category") or {})

    cats_a = _cats(run_a)
    cats_b = _cats(run_b)
    all_cats = sorted(set(cats_a) | set(cats_b))
    out = {}
    for cat in all_cats:
        a_cat = cats_a.get(cat) or {}
        b_cat = cats_b.get(cat) or {}
        out[cat] = {
            "total_a": a_cat.get("total"), "total_b": b_cat.get("total"),
            "passed_a": a_cat.get("passed"), "passed_b": b_cat.get("passed"),
            "accuracy": _compute_delta(a_cat.get("accuracy"), b_cat.get("accuracy"), "up"),
            "avg_factuality": _compute_delta(a_cat.get("avg_factuality"), b_cat.get("avg_factuality"), "up"),
            "avg_completeness": _compute_delta(a_cat.get("avg_completeness"), b_cat.get("avg_completeness"), "up"),
            "avg_tone": _compute_delta(a_cat.get("avg_tone"), b_cat.get("avg_tone"), "up"),
        }
    return out


def _divergent_cases(run_a: dict, run_b: dict, limit: int = 20) -> list:
    """Cruza details[].case_id; retorna casos onde passed flip.
    Ordena: regressões (a passou, b falhou) antes de melhorias."""
    def _details(run):
        d = run.get("details") or []
        if isinstance(d, str):
            try: d = json.loads(d)
            except: d = []
        return d if isinstance(d, list) else []

    by_id_a = {c.get("case_id"): c for c in _details(run_a) if c.get("case_id")}
    by_id_b = {c.get("case_id"): c for c in _details(run_b) if c.get("case_id")}
    common_ids = set(by_id_a) & set(by_id_b)

    flips = []
    for cid in common_ids:
        a, b = by_id_a[cid], by_id_b[cid]
        passed_a, passed_b = bool(a.get("passed")), bool(b.get("passed"))
        if passed_a == passed_b:
            continue
        flips.append({
            "case_id": cid,
            "category": a.get("category") or b.get("category") or "(sem categoria)",
            "expected_state": a.get("expected_state") or b.get("expected_state"),
            "regression": passed_a and not passed_b,
            "a": {
                "passed": passed_a,
                "actual_state": a.get("actual_state"),
                "factuality": a.get("factuality"),
                "completeness": a.get("completeness"),
                "tone": a.get("tone"),
                "safety": a.get("safety"),
                "failure_reasons": a.get("failure_reasons", []),
            },
            "b": {
                "passed": passed_b,
                "actual_state": b.get("actual_state"),
                "factuality": b.get("factuality"),
                "completeness": b.get("completeness"),
                "tone": b.get("tone"),
                "safety": b.get("safety"),
                "failure_reasons": b.get("failure_reasons", []),
            },
        })

    # Regressões primeiro, depois melhorias.
    flips.sort(key=lambda f: (not f["regression"], f["case_id"]))
    return flips[:limit]


@router.get("/eval-runs/compare")
async def compare_eval_runs(a: str, b: str):
    """Compara dois eval_runs. Valida gold_version + status='completed'."""
    if a == b:
        raise HTTPException(400, "Os dois IDs precisam ser diferentes")

    run_a = await eval_runs_repo.find_by_id(a)
    run_b = await eval_runs_repo.find_by_id(b)
    if not run_a or not run_b:
        raise HTTPException(404, "Um ou ambos eval_runs não encontrados")

    # dimension_breakdown e details vêm como TEXT JSON; parsear.
    for r in (run_a, run_b):
        _parse_json_field(r, "dimension_breakdown", {})
        _parse_json_field(r, "details", [])

    # Validações
    comparable = True
    reason = None
    if run_a.get("status") != "completed" or run_b.get("status") != "completed":
        comparable = False
        reason = (
            f"runs precisam estar completed: "
            f"a.status={run_a.get('status')!r}, b.status={run_b.get('status')!r}"
        )
    elif run_a.get("gold_version") != run_b.get("gold_version"):
        comparable = False
        reason = (
            f"datasets diferentes: a={run_a.get('gold_version')!r}, "
            f"b={run_b.get('gold_version')!r}. Comparar runs em datasets "
            "diferentes não tem significado estatístico."
        )

    response = {
        "run_a": _summary_of_run(run_a),
        "run_b": _summary_of_run(run_b),
        "comparable": comparable,
        "comparable_reason": reason,
    }
    if comparable:
        response["deltas"] = _aggregate_deltas(run_a, run_b)
        response["by_category_deltas"] = _by_category_deltas(run_a, run_b)
        response["divergent_cases"] = _divergent_cases(run_a, run_b, limit=20)
    else:
        response["deltas"] = {}
        response["by_category_deltas"] = {}
        response["divergent_cases"] = []
    return response
```
</change>
<acceptance>
- 4 helpers privados (_summary_of_run, _compute_delta, _aggregate_deltas, _by_category_deltas, _divergent_cases) + 1 endpoint.
- HTTPException 400 para a==b.
- HTTPException 404 quando algum ID não existe.
- comparable=false com reason quando status≠completed ou gold_version difere.
- Deltas com is_improvement consistente com _METRIC_DIRECTIONS.
- Divergent cases ordenados regressões primeiro.
</acceptance>
</task>

## Verification

- [ ] Smoke: `_compute_delta(0.8, 0.85, "up")` → `{a:0.8, b:0.85, delta:0.05, is_improvement:True}`.
- [ ] Smoke: `_compute_delta(0.05, 0.10, "down")` → `is_improvement:False` (sub mais alta = pior).
- [ ] Smoke: `_compute_delta(None, 0.5, ...)` → `{delta:None, is_improvement:None}`.
- [ ] Smoke: `_divergent_cases` com 5 cases (3 mesmo passed, 1 regressão, 1 melhoria) → retorna 2 cases, regressão primeiro.
- [ ] Smoke: `_aggregate_deltas` para 2 runs sintéticos cobre todas as 11 métricas em _METRIC_DIRECTIONS.
- [ ] Endpoint registrado em router; smoke async com mock repo cobrindo: a==b → 400; ids inexistentes → 404; status≠completed → comparable=false; gold_version diferente → comparable=false; tudo OK → comparable=true com 4 chaves de payload.

## must_haves

- Plan 02 (UI) consome o response sem precisar de processamento adicional.
- Endpoint só lê do DB — não muta nada.
- Deltas têm semântica clara via `is_improvement` (UI não precisa decidir direção).

## Notes

- O endpoint usa o helper `_parse_json_field` que já existe na mesma rota (linha ~548). Mantém um único parser pra TEXT JSON em todo o módulo.
- `eval_runs_repo.find_by_id` é o método padrão do `Repository` — funciona transparente.
- Não validar tipo dos IDs (UUID): se o DB não tem, vai retornar None → 404. UI valida formato antes do request.
