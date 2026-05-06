"""Diagnóstico end-to-end do caminho MCP.

Útil para entender por que uma chamada MCP não está acontecendo em
produção. Reproduz exatamente o mesmo fluxo de `execute_interaction`
(parse da SKILL → resolução de tools → build do prompt → tool_choice
forçado quando aplicável → chamada LLM → execução de tool_calls),
mas retorna TODOS os artefatos intermediários em um único JSON —
permitindo ao operador ver de fora o que o LLM respondeu e se o
Tavily (ou qualquer MCP server) foi realmente chamado.

Uso:
    POST /api/v1/mcp-diagnostics/probe
    { "agent_id": "...", "query": "sua pergunta" }
"""

import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.database import agents_repo, skills_repo, tools_repo
from app.skill_parser.parser import parse_skill_md

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp-diagnostics", tags=["mcp-diagnostics"])


class ProbeRequest(BaseModel):
    agent_id: str
    query: str = Field(..., min_length=1)
    execute_tool: bool = Field(
        default=True,
        description="Se True, executa os tool_calls retornados pelo LLM. Se False, só reporta o que o LLM respondeu.",
    )


@router.post("/probe")
async def probe(req: ProbeRequest):
    """Reproduz o caminho MCP completo e retorna artefatos intermediários."""
    from app.mcp.runtime import (
        parse_tool_bindings,
        match_with_registry,
        build_openai_tools,
        execute_tool_call,
    )
    from app.agents.engine import DeepAgentHarness
    from app.core.llm_providers import get_provider
    from app.core.config import get_settings

    t0 = time.time()

    # 1) Agent + Skill
    agent = await agents_repo.find_by_id(req.agent_id)
    if not agent:
        raise HTTPException(404, f"Agente '{req.agent_id}' não encontrado")

    skill_data: dict = {}
    parsed_skill_urn = None
    if agent.get("skill_id"):
        skill_row = await skills_repo.find_by_id(agent["skill_id"])
        if skill_row and skill_row.get("raw_content"):
            parsed = parse_skill_md(skill_row["raw_content"])
            parsed_skill_urn = parsed.frontmatter.id
            skill_data = {
                "purpose": parsed.purpose,
                "workflow": parsed.workflow,
                "output_contract": parsed.output_contract,
                "guardrails": parsed.guardrails,
                "tool_bindings": parsed.tool_bindings,
                "_name": parsed.name,
            }
    agent["_parsed_skill"] = skill_data

    # 2) Tool bindings: parse + match registry
    parsed_bindings: list[dict] = []
    mcp_tools: list[dict] = []
    if skill_data.get("tool_bindings"):
        parsed_bindings = parse_tool_bindings(skill_data["tool_bindings"])
        if parsed_bindings:
            mcp_tools = await match_with_registry(parsed_bindings, tools_repo)

    # redact secrets for response — substitui token por fingerprint do plaintext
    # (consistente para o mesmo segredo, mas não-reversível). auth_config inteiro
    # é redacted porque pode conter chaves OAuth2 e PEMs.
    from app.core.secrets import fingerprint as _secret_fp

    def _redact_tool(t: dict) -> dict:
        out = dict(t)
        if out.get("auth_token"):
            fp = _secret_fp(out["auth_token"])
            out["auth_token"] = f"<fp:{fp}>" if fp else "<present>"
        ac = out.get("auth_config")
        if ac:
            out["auth_config"] = "<redacted>"
        return out

    # 3) Build openai_tools
    openai_tools = build_openai_tools(mcp_tools)

    # 4) Harness — usa para detectar force_tool e construir o system prompt
    harness = DeepAgentHarness(agent, max_iterations=1, mcp_tools=mcp_tools)
    system_prompt = harness._build_system_prompt()
    force_tool = harness._should_force_tool_call()

    tool_choice: Any = "auto"
    forced_tool_name: str | None = None
    if force_tool and openai_tools:
        if len(openai_tools) == 1:
            forced_tool_name = openai_tools[0]["function"]["name"]
            tool_choice = {"type": "function", "function": {"name": forced_tool_name}}
        else:
            tool_choice = "required"

    # 5) LLM call real
    llm_info: dict = {"invoked": False}
    tool_exec_results: list[dict] = []
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        provider = get_provider(
            agent.get("llm_provider", "openai"),
            model=agent.get("model"),
        )
        llm = provider.get_langchain_llm()
        if openai_tools:
            if tool_choice == "auto":
                llm_bound = llm.bind_tools(openai_tools)
            else:
                llm_bound = llm.bind_tools(openai_tools, tool_choice=tool_choice)
        else:
            llm_bound = llm

        msgs = [SystemMessage(content=system_prompt), HumanMessage(content=req.query)]
        llm_start = time.time()
        resp = await llm_bound.ainvoke(msgs)
        llm_ms = round((time.time() - llm_start) * 1000, 1)

        tool_calls = getattr(resp, "tool_calls", None) or []
        content = getattr(resp, "content", "") or ""
        llm_info = {
            "invoked": True,
            "latency_ms": llm_ms,
            "content_len": len(content),
            "content_preview": content[:500],
            "tool_calls_count": len(tool_calls),
            "tool_calls": [
                {
                    "name": tc.get("name"),
                    "args_preview": json.dumps(tc.get("args") or {}, ensure_ascii=False)[:400],
                    "id": tc.get("id"),
                }
                for tc in tool_calls
            ],
        }

        # 6) Executa tool_calls de verdade se solicitado
        if req.execute_tool and tool_calls:
            for tc in tool_calls:
                tname = tc.get("name") or ""
                targs = tc.get("args") or {}
                exec_start = time.time()
                try:
                    result_text = await execute_tool_call(
                        tname, targs, mcp_tools, timeout=30
                    )
                    tool_exec_results.append({
                        "tool_name": tname,
                        "args": targs,
                        "latency_ms": round((time.time() - exec_start) * 1000, 1),
                        "result_len": len(result_text or ""),
                        "result_preview": (result_text or "")[:800],
                        "error": None,
                    })
                except Exception as e:
                    tool_exec_results.append({
                        "tool_name": tname,
                        "args": targs,
                        "latency_ms": round((time.time() - exec_start) * 1000, 1),
                        "error": str(e)[:300],
                    })
    except Exception as e:
        llm_info = {"invoked": False, "error": str(e)[:400]}

    duration_ms = round((time.time() - t0) * 1000, 1)

    return {
        "agent": {
            "id": agent.get("id"),
            "name": agent.get("name"),
            "kind": agent.get("kind"),
            "model": agent.get("model"),
            "provider": agent.get("llm_provider"),
            "skill_id": agent.get("skill_id"),
            "skill_urn": parsed_skill_urn,
        },
        "bindings": {
            "parsed_count": len(parsed_bindings),
            "parsed_names": [b.get("name") for b in parsed_bindings],
            "registry_matched_count": len(mcp_tools),
            "registry_matched": [_redact_tool(t) for t in mcp_tools],
            "openai_tools_count": len(openai_tools),
            "openai_tool_names": [t["function"]["name"] for t in openai_tools],
        },
        "prompt": {
            "system_len": len(system_prompt),
            "has_tool_catalog": "Ferramentas Disponíveis" in system_prompt,
            "has_regra_critica": "REGRA CRÍTICA" in system_prompt,
            "preview_tail": system_prompt[-600:],
        },
        "force_detection": {
            "workflow_present": bool(skill_data.get("workflow")),
            "workflow_preview": (skill_data.get("workflow") or "")[:300],
            "should_force": force_tool,
            "tool_choice": tool_choice if isinstance(tool_choice, str) else forced_tool_name,
            "forced_tool_name": forced_tool_name,
        },
        "llm": llm_info,
        "tool_execution": tool_exec_results,
        "duration_ms": duration_ms,
    }


@router.get("/recent-tool-calls")
async def recent_tool_calls(limit: int = 10):
    """Últimas entradas em tool_calls — útil para ver histórico real."""
    from app.core.database import tool_calls_repo
    calls = await tool_calls_repo.find_all(limit=limit)
    # Redact/clip
    for c in calls:
        for k in ("request_headers", "input_data", "output_data", "response_body"):
            if k in c and isinstance(c[k], str) and len(c[k]) > 300:
                c[k] = c[k][:300] + "…"
    return {"count": len(calls), "calls": calls}
