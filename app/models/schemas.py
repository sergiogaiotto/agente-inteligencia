"""Schemas Pydantic — todas entidades da especificação."""
from pydantic import BaseModel, Field
from typing import Optional

class AgentCreate(BaseModel):
    name: str = Field(..., min_length=2)
    description: Optional[str] = None
    kind: str = Field(default="subagent", pattern="^(aobd|router|subagent)$")
    domain: Optional[str] = None
    skill_id: Optional[str] = None
    llm_provider: str = "openai"
    model: str = "gpt-4.1"
    system_prompt: Optional[str] = "Você é um agente inteligente."
    version: Optional[str] = "1.0.0"
    status: Optional[str] = "active"
    config: Optional[str] = "{}"
    require_evidence: Optional[bool] = True

class AgentUpdate(BaseModel):
    name: Optional[str] = None; description: Optional[str] = None
    kind: Optional[str] = None; domain: Optional[str] = None
    skill_id: Optional[str] = None; llm_provider: Optional[str] = None
    model: Optional[str] = None; system_prompt: Optional[str] = None
    config: Optional[str] = None; status: Optional[str] = None
    version: Optional[str] = None
    require_evidence: Optional[bool] = None

class SkillCreateRaw(BaseModel):
    raw_content: str = Field(..., min_length=10)
    tags: Optional[str] = "[]"

class SkillCreateManual(BaseModel):
    name: str; kind: str = "subagent"; domain: Optional[str] = None
    version: str = "0.1.0"; purpose: Optional[str] = None
    activation_criteria: Optional[str] = None; workflow: Optional[str] = None
    tool_bindings: Optional[str] = "[]"; output_contract: Optional[str] = None
    failure_modes: Optional[str] = None; raw_content: str
    tags: Optional[str] = "[]"

class ChatMessage(BaseModel):
    agent_id: str; message: str; session_id: Optional[str] = None
    channel: str = "api"; journey: Optional[str] = ""
    attachments: Optional[list] = None
    mode: Optional[str] = "agent"

class MeshConnectionCreate(BaseModel):
    source_agent_id: str; target_agent_id: str
    connection_type: str = "sequential"; config: Optional[str] = "{}"

class KnowledgeSourceCreate(BaseModel):
    name: str; description: Optional[str] = None
    source_type: Optional[str] = "manual"
    confidentiality_label: str = "internal"

class ToolCreate(BaseModel):
    name: str
    mcp_server: Optional[str] = None
    mcp_server_type: Optional[str] = "http"
    description: Optional[str] = None
    operations: str = "[]"
    input_schema: Optional[str] = None
    output_schema: Optional[str] = None
    cost_per_call: float = 0
    sensitivity: str = "internal"
    requires_trusted_context: bool = False
    auth_requirements: Optional[str] = None
    auth_token: Optional[str] = None
    auth_config: Optional[str] = "{}"
    sla: Optional[str] = "{}"

class ToolUpdate(BaseModel):
    name: Optional[str] = None
    mcp_server: Optional[str] = None
    mcp_server_type: Optional[str] = None
    description: Optional[str] = None
    operations: Optional[str] = None
    input_schema: Optional[str] = None
    output_schema: Optional[str] = None
    cost_per_call: Optional[float] = None
    sensitivity: Optional[str] = None
    requires_trusted_context: Optional[bool] = None
    auth_requirements: Optional[str] = None
    auth_token: Optional[str] = None
    auth_config: Optional[str] = None
    sla: Optional[str] = None

class GoldCaseCreate(BaseModel):
    dataset_version: str = "v1"; case_type: str = "normal"
    journey: Optional[str] = None; channel: str = "api"
    complexity: Optional[str] = None
    input_text: str; expected_output: str
    expected_state: str = "Recommend"

class ReleaseCreate(BaseModel):
    name: str; environment: str = "staging"
    model_config_data: Optional[str] = "{}"
    prompt_config: Optional[str] = "{}"
    index_config: Optional[str] = "{}"
    policy_config: Optional[str] = "{}"

class CAREntryCreate(BaseModel):
    skill_urn: str; domain: str
    activation_keywords: str = "[]"
    required_entities: str = "[]"

class RunEvalRequest(BaseModel):
    release_id: str; agent_id: str
    gold_version: str = "latest"; run_type: str = "baseline"

class UserCreate(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = ""
    email: Optional[str] = ""
    role: str = "comum"
    domains: Optional[str] = "[]"

class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    domains: Optional[str] = None
    password: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class DomainCreate(BaseModel):
    name: str
    description: Optional[str] = ""