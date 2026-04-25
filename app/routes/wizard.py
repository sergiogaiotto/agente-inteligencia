"""Rotas do Wizard IA — geração assistida de agentes e skills."""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.core.llm_providers import get_provider
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/wizard", tags=["wizard"])


class WizardAgentRequest(BaseModel):
    description: str
    domain: Optional[str] = ""
    provider: str = "openai"
    model: Optional[str] = ""  # vazio → provider usa default da config


class WizardSkillRequest(BaseModel):
    description: str
    kind: str = "subagent"
    domain: Optional[str] = ""
    provider: str = "openai"
    model: Optional[str] = ""


class WizardRefineRequest(BaseModel):
    current_content: str
    instruction: str
    field: str = "all"
    provider: str = "openai"
    model: Optional[str] = ""


@router.post("/agent")
async def wizard_agent(data: WizardAgentRequest):
    """Wizard IA: gera configuração completa de agente a partir de descrição livre."""
    try:
        llm = get_provider(data.provider, model=(data.model or None))
        response = await llm.generate([
            {"role": "system", "content": """Você é um arquiteto de agentes de IA. 
Dado uma descrição do usuário, gere a configuração completa de um agente.

Responda APENAS com JSON válido (sem markdown, sem ```), contendo:
{
  "name": "Nome curto e descritivo do agente",
  "description": "Descrição detalhada do que o agente faz",
  "kind": "aobd|router|subagent",
  "domain": "domínio de negócio (ex: financeiro, rh, operacoes)",
  "system_prompt": "System prompt completo e detalhado para o agente, com persona, capacidades, restrições e formato de resposta",
  "suggested_skills": ["lista de skills que o agente precisaria"],
  "suggested_tools": ["lista de ferramentas MCP sugeridas"]
}

Regras:
- kind=aobd para orquestradores de domínio que interpretam intenção
- kind=router para processos de negócio que decompõem em tarefas
- kind=subagent para tarefas atômicas e específicas
- O system_prompt deve ser rico, com instruções claras, formato de saída e guardrails"""},
            {"role": "user", "content": data.description},
        ])
        content = response["content"].strip()
        if content.startswith("```"):
            import re
            m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if m: content = m.group(1).strip()
        result = json.loads(content)
        return {"status": "ok", "agent": result}
    except json.JSONDecodeError:
        return {"status": "ok", "agent": {"name": "", "description": data.description, "kind": "subagent", "domain": data.domain, "system_prompt": content, "suggested_skills": [], "suggested_tools": []}}
    except Exception as e:
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


@router.post("/skill")
async def wizard_skill(data: WizardSkillRequest):
    """Wizard IA: gera SKILL.md canônico completo a partir de descrição livre."""
    try:
        llm = get_provider(data.provider, model=(data.model or None))
        response = await llm.generate([
            {"role": "system", "content": f"""Você é um arquiteto de skills para plataforma multi-agente.
Gere um SKILL.md completo seguindo a anatomia canônica.

O SKILL.md deve conter EXATAMENTE esta estrutura:

---
id: urn:skill:{data.domain or 'geral'}:{data.kind}:SLUG_AQUI
version: 0.1.0
kind: {data.kind}
owner: equipe-ia
stability: alpha
---

# Nome do Skill

## Purpose
Declaração imperativa do que este agente faz e do que NÃO faz.

## Activation Criteria
Condições sob as quais este skill deve ser selecionado.

## Inputs
Schema tipado do envelope esperado em formato JSON Schema.

## Workflow
Sequência de passos do workflow. Para subagentes, linear. Para roteadores, DAG.

## Tool Bindings
Lista de tools MCP permitidas com condições de uso.

## Output Contract
Schema tipado da saída esperada.

## Failure Modes
Enumeração de falhas e ação prescrita.

## Evidence Policy
Bases autorizadas e thresholds de evidência (quando aplicável).

## Guardrails
Políticas de conteúdo, PII, jurisdição.

## Budget
Limites de tokens, tempo e custo.

## Examples
Pares entrada/saída para avaliação.

Gere o SKILL.md completo em formato markdown. Seja específico e detalhado."""},
            {"role": "user", "content": data.description},
        ])
        return {"status": "ok", "skill_md": response["content"]}
    except Exception as e:
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


@router.post("/refine")
async def wizard_refine(data: WizardRefineRequest):
    """Wizard IA: refina/melhora um campo ou conteúdo existente."""
    try:
        llm = get_provider(data.provider, model=(data.model or None))
        response = await llm.generate([
            {"role": "system", "content": "Você é um especialista em refinamento de configurações de IA. Melhore o conteúdo conforme a instrução do usuário. Responda APENAS com o conteúdo melhorado, sem explicações adicionais."},
            {"role": "user", "content": f"Campo: {data.field}\n\nConteúdo atual:\n{data.current_content}\n\nInstrução de melhoria:\n{data.instruction}"},
        ])
        return {"status": "ok", "refined": response["content"]}
    except Exception as e:
        raise HTTPException(500, f"Erro no wizard: {str(e)}")


@router.get("/models")
async def list_available_models():
    """Lista modelos disponíveis por provedor."""
    return {
        "openai": [
            {"id": "gpt-4o", "name": "GPT-4o", "context": "128K", "tier": "flagship"},
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "context": "128K", "tier": "efficient"},
            {"id": "gpt-4-turbo", "name": "GPT-4 Turbo", "context": "128K", "tier": "legacy"},
            {"id": "gpt-4.1", "name": "GPT-4.1", "context": "1M", "tier": "flagship"},
            {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "context": "1M", "tier": "efficient"},
            {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano", "context": "1M", "tier": "nano"},
            {"id": "o4-mini", "name": "o4 Mini (reasoning)", "context": "200K", "tier": "reasoning"},
            {"id": "o3", "name": "o3 (reasoning)", "context": "200K", "tier": "reasoning"},
            {"id": "o3-mini", "name": "o3 Mini (reasoning)", "context": "200K", "tier": "reasoning"},
            {"id": "o1", "name": "o1 (reasoning)", "context": "200K", "tier": "reasoning"},
            {"id": "o1-mini", "name": "o1 Mini (reasoning)", "context": "128K", "tier": "reasoning"},
        ],
        "maritaca": [
            {"id": "sabia-4", "name": "Sabiá-4", "context": "128K", "tier": "flagship"},
            {"id": "sabia-3", "name": "Sabiá-3", "context": "32K", "tier": "flagship"},
            {"id": "sabia-3-2025-01-15", "name": "Sabiá-3 (Jan/25)", "context": "32K", "tier": "flagship"},
            {"id": "sabia-2-medium", "name": "Sabiá-2 Medium", "context": "16K", "tier": "efficient"},
            {"id": "sabia-2-small", "name": "Sabiá-2 Small", "context": "8K", "tier": "small"},
        ],
        "ollama": [
            {"id": "Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest", "name": "Gaia 4b", "context": "128K", "tier": "flagship"},
            {"id": "gemma4:e4b", "name": "Gemma 4 4B", "context": "128K", "tier": "flagship"},
            {"id": "gemma3:4b", "name": "Gemma 3 4B", "context": "128K", "tier": "efficient"},
            {"id": "gemma3:1b", "name": "Gemma 3 1B", "context": "32K", "tier": "small"},
            {"id": "gemma3:12b", "name": "Gemma 3 12B", "context": "128K", "tier": "flagship"},
        ],
    }
