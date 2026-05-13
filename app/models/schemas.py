"""Schemas Pydantic — todas entidades da especificação."""
from pydantic import BaseModel, Field
from typing import Optional, Any

class AgentCreate(BaseModel):
    name: str = Field(..., min_length=2)
    description: Optional[str] = None
    kind: str = Field(default="subagent", pattern="^(aobd|router|subagent)$")
    domain: Optional[str] = None
    skill_id: Optional[str] = None
    # Onda 7: task_type substitui o paradigma direto de provider+model.
    # Quando setado, save resolve via routing. Aceita NULL p/ back-compat
    # (legacy direto via llm_provider/model).
    task_type: Optional[str] = Field(
        default=None,
        pattern="^(tool_calling|reasoning|instruct|classification)$",
    )
    llm_provider: str = "azure"
    model: str = "gpt-4o"
    system_prompt: Optional[str] = "Você é um agente inteligente."
    version: Optional[str] = "1.0.0"
    status: Optional[str] = "active"
    config: Optional[str] = "{}"
    require_evidence: Optional[bool] = True
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    accepts_images: Optional[bool] = False
    accepts_documents: Optional[bool] = False
    # Frase humana mostrada no execution_log quando o agente está processando
    # ("Orquestrando seu pedido", "Escolhendo o especialista", etc.). Limite curto
    # pra evitar logs poluídos. NULL/vazio = não injeta nada (back-compat).
    processing_message: Optional[str] = Field(default=None, max_length=140)

class AgentUpdate(BaseModel):
    name: Optional[str] = None; description: Optional[str] = None
    kind: Optional[str] = None; domain: Optional[str] = None
    skill_id: Optional[str] = None; llm_provider: Optional[str] = None
    model: Optional[str] = None; system_prompt: Optional[str] = None
    config: Optional[str] = None; status: Optional[str] = None
    version: Optional[str] = None
    task_type: Optional[str] = Field(
        default=None,
        pattern="^(tool_calling|reasoning|instruct|classification)$",
    )
    require_evidence: Optional[bool] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    accepts_images: Optional[bool] = None
    accepts_documents: Optional[bool] = None
    processing_message: Optional[str] = Field(default=None, max_length=140)


class PreflightCheckResult(BaseModel):
    """Resultado de um check individual do pre-flight de agente."""
    id: str
    severity: str = Field(..., pattern="^(error|warning|info)$")
    title: str
    detail: str
    fix_hint: Optional[str] = None
    field: Optional[str] = None  # campo do form que o operador pode editar


class PreflightReport(BaseModel):
    """Resultado agregado dos 9 checks. blocked=True quando há error."""
    checks: list[PreflightCheckResult] = Field(default_factory=list)
    has_errors: bool = False
    has_warnings: bool = False
    blocked: bool = False

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

class InvokeOptions(BaseModel):
    timeout_ms: Optional[int] = None
    dry_run: Optional[bool] = False

class AttachmentInput(BaseModel):
    """Anexo binário enviado serializado em base64 no body do /invoke.
    Filtragem por accepts_images/accepts_documents do agente acontece server-side."""
    filename: str
    content_type: Optional[str] = None  # auto-detectado do filename se ausente
    content_base64: str

class AgentInvokeRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    context: Optional[dict[str, Any]] = None
    session_id: Optional[str] = None
    channel: Optional[str] = "api"
    journey: Optional[str] = ""
    message: Optional[str] = None
    options: Optional[InvokeOptions] = None
    attachments: Optional[list[AttachmentInput]] = None  # máx 5 itens, 10MB cada

class AgentInvokeResponse(BaseModel):
    session_id: Optional[str] = None
    agent_id: str
    status: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    trace_id: Optional[str] = None
    duration_ms: float = 0
    evidence_score: Optional[float] = None
    errors: list = Field(default_factory=list)
    rejected_attachments: list = Field(default_factory=list)  # anexos filtrados (mime não aceito, oversize)

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
    """Caso do Golden Dataset.

    Campos legados (mantidos para back-compat com casos pre-enriquecimento):
        dataset_version, case_type, journey, channel, complexity,
        input_text, expected_output, expected_state.

    Enriquecimento:
        - category: taxonomia semântica (ex: "atendimento", "compliance", "vendas").
          Usado para breakdown de acurácia no relatório.
        - weight: peso na média ponderada (default 1.0). Casos críticos podem
          pesar mais (ex: 5.0). Range [0.1, 10.0].
        - expected_pattern: regex Python opcional. Quando presente, o evaluator
          usa re.search(pattern, output, IGNORECASE) em vez de similarity check
          contra expected_output.
        - red_flags: lista de strings que NUNCA devem aparecer no output.
          Match case-insensitive substring; qualquer match → caso falha.
          Persistido como JSON list em coluna TEXT.
    """
    dataset_version: str = "v1"
    case_type: str = "normal"
    journey: Optional[str] = None
    channel: str = "api"
    complexity: Optional[str] = None
    input_text: str
    expected_output: str
    expected_state: str = "Recommend"
    # ── Enriquecimento Golden Dataset ──
    category: Optional[str] = None
    weight: float = Field(default=1.0, ge=0.1, le=10.0)
    expected_pattern: Optional[str] = None
    red_flags: list[str] = Field(default_factory=list)

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