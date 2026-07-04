"""Harness de latência do POST /pipelines/{id}/invoke.

Instrumento de ANTES/DEPOIS do plano de tuning (v25.1.2+). Mede, por cenário
de invoke, wall-time e server-time (p50/p95) e a AMPLIFICAÇÃO DE QUERIES
(delta de pg_stat_database.xact_commit) — a métrica estável, independente da
latência do LLM, que pega regressão de round-trips ao Postgres.

Uso (host, app docker de pé na porta 7000):
    python scripts/latency_bench.py                 # cenários default do Aurora
    python scripts/latency_bench.py --n 20 --warmup 3
    python scripts/latency_bench.py --json          # saída machine-readable

Config por env: BENCH_BASE_URL (http://localhost:7000), BENCH_USER_ID
(uuid do usuário p/ assinar o cookie), BENCH_PIPELINE (id do pipeline).
Auth: assina o cookie de sessão via `docker exec agente_app`.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import subprocess
import sys
import time
from urllib.parse import urlparse

BASE_URL = os.environ.get("BENCH_BASE_URL", "http://localhost:7000").rstrip("/")
USER_ID = os.environ.get("BENCH_USER_ID", "08768f10-a3ab-41db-8134-ba7c6768d1b1")
PIPELINE = os.environ.get("BENCH_PIPELINE", "8df2d21e-8417-4ac1-9610-bbadaf7f005d")
PG_CONTAINER = os.environ.get("BENCH_PG_CONTAINER", "agente_postgres")
PG_DB = os.environ.get("BENCH_PG_DB", "agente_inteligencia")
PG_USER = os.environ.get("BENCH_PG_USER", "agente")
APP_CONTAINER = os.environ.get("BENCH_APP_CONTAINER", "agente_app")

# Cenários = perfis de pipeline que queremos vigiar. O tuning muda estes números;
# a suíte prova que nenhum regride (round-trips) e reporta a latência.
SCENARIOS = [
    {"name": "declarativo (limite)", "args": {
        "tipo": "limite", "mensagem": "Qual o limite do cliente 1001?", "cd_cliente": 1001}},
    {"name": "multi-agente (analise)", "args": {
        "tipo": "analise",
        "mensagem": "Analise o credito do cliente 1001 para aumento de limite",
        "cd_cliente": 1001}},
]


def _sh(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout.strip()


def _cookie() -> str:
    out = _sh([
        "docker", "exec", APP_CONTAINER, "python", "-c",
        f"from app.core.auth import sign_session; print(sign_session('{USER_ID}'))",
    ])
    return out.strip()


def _xact_commit() -> int:
    """Transações commitadas no DB (≈ round-trips) — proxy de amplificação."""
    out = _sh([
        "docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-t", "-c",
        f"SELECT xact_commit FROM pg_stat_database WHERE datname='{PG_DB}';",
    ])
    try:
        return int(out.strip())
    except ValueError:
        return -1


def _conn() -> http.client.HTTPConnection:
    u = urlparse(BASE_URL)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=180)


def _invoke(conn: http.client.HTTPConnection, cookie: str, args: dict) -> tuple[float, dict]:
    """Wall-time via conexão KEEP-ALIVE reusada — sem o custo de connect por
    request (o ~210ms de connect ao localhost via Docker Desktop no Windows
    não é o app e mascararia o ganho)."""
    body = json.dumps({"args": args})
    path = f"/api/v1/pipelines/{PIPELINE}/invoke"
    headers = {"Content-Type": "application/json", "Cookie": f"user_id={cookie}"}
    t0 = time.perf_counter()
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    wall = (time.perf_counter() - t0) * 1000.0
    return wall, json.loads(raw)


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[k]


def run(n: int, warmup: int) -> list[dict]:
    cookie = _cookie()
    if not cookie:
        print("ERRO: não obtive o cookie de sessão (app docker de pé?).", file=sys.stderr)
        sys.exit(2)
    results = []
    conn = _conn()
    for sc in SCENARIOS:
        for _ in range(warmup):
            try:
                _invoke(conn, cookie, sc["args"])
            except Exception:
                conn = _conn()
        walls, servers, roundtrips = [], [], []
        first_steps = None
        for _ in range(n):
            xc0 = _xact_commit()
            try:
                wall, d = _invoke(conn, cookie, sc["args"])
            except Exception as e:
                print(f"  [{sc['name']}] invoke falhou: {e}", file=sys.stderr)
                conn = _conn()
                continue
            xc1 = _xact_commit()
            walls.append(wall)
            servers.append(float(d.get("duration_ms") or 0))
            if xc0 >= 0 and xc1 >= 0:
                roundtrips.append(xc1 - xc0)
            if first_steps is None:
                first_steps = [
                    (s.get("agent_name", "")[:30], s.get("status"), s.get("duration_ms"),
                     bool(s.get("verification")))
                    for s in d.get("pipeline_steps", []) if s.get("status") == "completed"
                ]
        results.append({
            "scenario": sc["name"], "n": len(walls),
            "wall_p50": _pct(walls, 50), "wall_p95": _pct(walls, 95),
            "server_p50": _pct(servers, 50), "server_p95": _pct(servers, 95),
            "roundtrips_p50": statistics.median(roundtrips) if roundtrips else None,
            "roundtrips_max": max(roundtrips) if roundtrips else None,
            "steps": first_steps or [],
        })
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8, help="medições por cenário")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = run(a.n, a.warmup)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"\nLatência do invoke — N={a.n} warm (base={BASE_URL} pipeline={PIPELINE[:8]})")
    print("=" * 78)
    for r in res:
        print(f"\n▸ {r['scenario']}  (n={r['n']})")
        print(f"    wall    p50={r['wall_p50']:8.0f}ms  p95={r['wall_p95']:8.0f}ms")
        print(f"    server  p50={r['server_p50']:8.0f}ms  p95={r['server_p95']:8.0f}ms")
        print(f"    round-trips Postgres  p50={r['roundtrips_p50']}  max={r['roundtrips_max']}")
        for name, status, dur, judged in r["steps"]:
            print(f"      · {name:<30} {str(dur):>9}ms  judge={'sim' if judged else 'não'}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
