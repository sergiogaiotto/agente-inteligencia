"""Seed de um cenário REAL para testar o contrato de `args` do invoke nos 3 modos:
determinístico (x-uso:param → inputs.X), LLM (x-uso:llm → prosa) e híbrido.

Idempotente: usa IDs fixos e apaga antes de recriar. Marca tudo com domain='__argtest__'.
Topologia:
    Roteador (raiz, tem ## Inputs com x-uso) --conditional--> Trilha Prioritaria
                                             --conditional--> Trilha Comum
Regra Prioritaria: inputs.tier == 'gold' or 'urgente' in input_lower
Regra Comum:       not (inputs.tier == 'gold' or 'urgente' in input_lower)
"""
import asyncio
import json

from app.core.database import (
    init_db, skills_repo, agents_repo, mesh_repo, pipelines_repo,
    pipeline_membership, _get_pool,
)

# IDs fixos (idempotência)
SK = "aaaaaaaa-0000-0000-0000-000000000001"
ROOT = "aaaaaaaa-0000-0000-0000-0000000000a0"
PRIO = "aaaaaaaa-0000-0000-0000-0000000000a1"
COMM = "aaaaaaaa-0000-0000-0000-0000000000a2"
PID = "aaaaaaaa-0000-0000-0000-0000000000f0"
C1 = "aaaaaaaa-0000-0000-0000-0000000000c1"
C2 = "aaaaaaaa-0000-0000-0000-0000000000c2"

SKILL_MD = """---
id: argtest.roteador
name: Roteador de Credito
version: 1.0.0
---
# Roteador de Credito

## Purpose
Recebe um pedido e roteia por tier/urgencia.

## Inputs
```json
{
  "type": "object",
  "properties": {
    "tier": {"type": "string", "x-uso": "param"},
    "valor": {"type": "number", "x-uso": "param", "default": 0},
    "canal": {"type": "string", "x-uso": "param", "default": "web"},
    "observacao": {"type": "string", "x-uso": "llm"}
  },
  "required": []
}
```

## Workflow
1. Encaminhar o pedido para a trilha adequada.
"""


async def _wipe():
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute("DELETE FROM mesh_connections WHERE source_agent_id = ANY($1::text[])", [ROOT, PRIO, COMM])
        await con.execute("DELETE FROM pipeline_agents WHERE pipeline_id=$1", PID)
        await con.execute("DELETE FROM pipelines WHERE id=$1", PID)
        await con.execute("DELETE FROM agents WHERE id = ANY($1::text[])", [ROOT, PRIO, COMM])
        await con.execute("DELETE FROM skills WHERE id=$1", SK)


def _agent(aid, name, kind, sysprompt, skill_id=None):
    return {
        "id": aid, "name": name, "kind": kind, "domain": "__argtest__",
        "skill_id": skill_id, "llm_provider": "gpt-oss-120b",
        "model": "openai/gpt-oss-120b", "task_type": "tool_calling",
        "system_prompt": sysprompt, "status": "active", "temperature": 0.3,
        "allow_general_knowledge": True, "response_language": "pt-BR",
    }


async def main():
    await init_db()
    try:
        from app.core.config import apply_settings_to_env
        await apply_settings_to_env()
    except Exception as e:
        print("apply_settings_to_env:", e)

    await _wipe()

    # 1. skill do roteador (## Inputs com x-uso)
    await skills_repo.create({
        "id": SK, "urn": "argtest.roteador", "name": "Roteador de Credito",
        "kind": "orchestrator", "domain": "__argtest__", "version": "1.0.0",
        "raw_content": SKILL_MD,
    })

    # 2. agentes
    await agents_repo.create(_agent(
        ROOT, "Roteador ArgTest", "router",
        "Voce e um roteador. Responda SOMENTE com a frase exata: 'Pedido recebido.' "
        "Nunca cite nomes de agentes, trilhas ou blocos JSON.", skill_id=SK))
    await agents_repo.create(_agent(
        PRIO, "Trilha Prioritaria ArgTest", "subagent",
        "Voce e a trilha PRIORITARIA. Comece a resposta com '[PRIORITARIA]' e diga que "
        "tratara o caso com prioridade."))
    await agents_repo.create(_agent(
        COMM, "Trilha Comum ArgTest", "subagent",
        "Voce e a trilha COMUM. Comece a resposta com '[COMUM]' e diga que seguira o fluxo padrao."))

    # 3. pipeline + membership
    await pipelines_repo.create({
        "id": PID, "name": "Teste Contrato Args", "status": "rascunho",
        "domain": "__argtest__", "entry_agent_id": ROOT,
        "description": "Cenario de teste dos 3 modos de args (determin/LLM/hibrido).",
    })
    for aid in (ROOT, PRIO, COMM):
        await pipeline_membership.set(aid, PID)

    # 4. arestas condicionais (inputs.X + keyword)
    expr_prio = "inputs.tier == 'gold' or 'urgente' in input_lower"
    expr_comm = "not (inputs.tier == 'gold' or 'urgente' in input_lower)"
    await mesh_repo.create({
        "id": C1, "source_agent_id": ROOT, "target_agent_id": PRIO,
        "connection_type": "conditional", "config": json.dumps({"expr": expr_prio}),
    })
    await mesh_repo.create({
        "id": C2, "source_agent_id": ROOT, "target_agent_id": COMM,
        "connection_type": "conditional", "config": json.dumps({"expr": expr_comm}),
    })

    # 5. publicar + selar o contrato (via a mesma rotina do endpoint de transicao)
    from app.routes.pipelines import _seal_args_contract
    pool = _get_pool()
    async with pool.acquire() as con:
        await con.execute("UPDATE pipelines SET status='publicado' WHERE id=$1", PID)
    await _seal_args_contract(PID)

    # 6. verificacao
    p = await pipelines_repo.find_by_id(PID)
    from app.routes.agents import get_agent_inputs_schema
    live = await get_agent_inputs_schema(ROOT)
    print("=== SEED OK ===")
    print("pipeline_id:", PID)
    print("status:", p["status"])
    print("contract_version:", p["contract_version"])
    print("contract_hash:", p["contract_hash"])
    print("args_contract SELADO:", p["args_contract"])
    print("live inputs_schema:", json.dumps(live.get("inputs_schema"), ensure_ascii=False))
    print("execution_mode raiz:", live.get("execution_mode"))


if __name__ == "__main__":
    asyncio.run(main())
