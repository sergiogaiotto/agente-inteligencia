"""Schemas Pydantic — todas entidades da especificação."""
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Any


def _norm_reasoning_effort(v):
    """Normaliza reasoning_effort: vazio → None; valida low|medium|high."""
    v = (v or "").strip().lower() or None
    if v and v not in ("low", "medium", "high"):
        raise ValueError("reasoning_effort deve ser low|medium|high ou null")
    return v


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
    # Escape hatch do princípio grounded-by-default (2026-06-06). Quando True,
    # o agente PODE usar conhecimento geral/paramétrico do modelo (ex: agente de
    # brainstorming). Default False = comportamento global: responde SÓ com base
    # em evidências (anexos/RAG/tools). É a única porta de "solicitado CLARAMENTE".
    # Ver app/agents/engine.py (_build_grounding_directive + _grounding_guard).
    allow_general_knowledge: Optional[bool] = False
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    # Esforço de raciocínio (low|medium|high) p/ modelos de reasoning (gpt-oss, o1/o3).
    # null = default do modelo. Só é enviado p/ providers da família OpenAI.
    reasoning_effort: Optional[str] = Field(default=None)
    accepts_images: Optional[bool] = False
    accepts_documents: Optional[bool] = False
    # Frase humana mostrada no execution_log quando o agente está processando
    # ("Orquestrando seu pedido", "Escolhendo o especialista", etc.). Limite curto
    # pra evitar logs poluídos. NULL/vazio = não injeta nada (back-compat).
    processing_message: Optional[str] = Field(default=None, max_length=140)
    # Idioma de resposta (BCP-47: "pt-BR", "en-US", ...). NULL/vazio = herda
    # settings.default_response_language (pt-BR padrão). Engine prepende
    # instrução no system_prompt — LLM responde nesse idioma mesmo quando
    # evidências vêm em outros (ex: Tavily retorna inglês, resposta sai pt-BR).
    # Pattern blinda contra valores arbitrários — UI usa dropdown fechado.
    response_language: Optional[str] = Field(
        default=None,
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
        description="BCP-47 tag (pt-BR, en-US, es-ES, ...) ou null pra herdar default global",
    )

    @field_validator("reasoning_effort")
    @classmethod
    def _ve_reasoning_effort(cls, v):
        return _norm_reasoning_effort(v)

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
    allow_general_knowledge: Optional[bool] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    reasoning_effort: Optional[str] = Field(default=None)
    accepts_images: Optional[bool] = None
    accepts_documents: Optional[bool] = None
    processing_message: Optional[str] = Field(default=None, max_length=140)
    response_language: Optional[str] = Field(
        default=None,
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
    )

    @field_validator("reasoning_effort")
    @classmethod
    def _ve_reasoning_effort(cls, v):
        return _norm_reasoning_effort(v)


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
    # Memória de conversa multi-turno (2026-06-06). 'auto' (default) reconstrói
    # a janela da sessão (escopada por camada: router médio / aobd leve / SA off)
    # e a reinjeta no LLM + nos sinais do gate. 'none' = stateless (função pura,
    # p/ integrações idempotentes). 'client'/'summary' reservados (hoje = auto).
    # Só age quando há session_id. Ver app/agents/conversation_memory.py.
    context_mode: Optional[str] = "auto"

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
    # Memória de conversa multi-turno (2026-06-06). Igual ChatMessage: 'auto'
    # (default) reconstrói a janela da sessão e reinjeta no LLM + gate; 'none'
    # = stateless (função pura, p/ integrações idempotentes via API). Só age
    # quando session_id está presente. Ver app/agents/conversation_memory.py.
    context_mode: Optional[str] = "auto"

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
    # Contrato de Decisão estruturado (36.1.0, ADITIVO): {campo: valor} validado
    # contra o ## Decisions do agente que respondeu (a linha DECISAO não vem
    # mais no texto — este campo é a via de MÁQUINA). None = sem contrato/linha.
    decision: Optional[dict[str, str]] = None

class MeshConnectionCreate(BaseModel):
    source_agent_id: str; target_agent_id: str
    connection_type: str = "sequential"; config: Optional[str] = "{}"

# ── Estúdio de Pipelines (PR1) ──
class PipelineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    domain: Optional[str] = None
    color: Optional[str] = "teal"
    description: Optional[str] = None
    # Tuning 26.0.0: roteamento rápido opt-in (pula a chamada LLM do router).
    fast_routing: Optional[bool] = False
    # Tuning 26.1.0: postura de auditoria (inherit|sync|async|disabled).
    audit_posture: Optional[str] = Field(
        default="inherit", pattern="^(inherit|sync|async|disabled)$")

class PipelineUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    domain: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    fast_routing: Optional[bool] = None
    audit_posture: Optional[str] = Field(
        default=None, pattern="^(inherit|sync|async|disabled)$")

class PipelineStatusChange(BaseModel):
    status: str

class PipelineAddAgent(BaseModel):
    agent_id: str

class PipelineEntrySet(BaseModel):
    """Define (ou limpa) o ponto de entrada do pipeline. agent_id=null → automático."""
    agent_id: Optional[str] = None

class PipelineInvokeRequest(BaseModel):
    """Invoca um pipeline pela ENTIDADE (contrato API-first selado — Trilha A PR-A2)."""
    message: Optional[str] = None
    input: Optional[str] = None  # alias amigável p/ message (texto livre)
    # Parâmetros ESTRUTURADOS (dict) — validados/coagidos contra o ## Inputs do
    # agente-raiz ANTES de executar (422 nomeando cada campo: required ausente,
    # tipo, enum, chave fora do contrato). Dobrados na entrada como bloco
    # "## Parâmetros estruturados" (raiz LLM lê como contexto; raiz declarativa
    # consome como inputs). Opcional — texto livre em 'message' segue válido.
    # NÃO confundir com 'input' (alias de TEXTO LIVRE de 'message').
    args: Optional[dict] = None
    # Pré-visualização (plan): quando true, o /invoke RESOLVE os args (coage,
    # aplica defaults do schema, valida) e devolve o payload resolvido + a
    # proveniência de cada campo (caller|default) SEM executar o pipeline (não
    # gasta LLM). Ignorado no /invoke/stream (esse sempre executa).
    dry: Optional[bool] = False
    session_id: Optional[str] = None
    channel: Optional[str] = "api"
    # Memória de conversa multi-turno (paridade com AgentInvokeRequest.context_mode):
    # 'auto' (default) reconstrói a janela da sessão e reinjeta no LLM + gate;
    # 'none' = stateless (função pura, p/ integrações idempotentes via API). Só age
    # quando session_id está presente. Ver app/agents/conversation_memory.py.
    context_mode: Optional[str] = "auto"
    # Detalhe da resposta: full | summary | minimal. Ausente → default por auth
    # (sessão→full; X-API-Key→platform_settings.api_invoke_default_verbosity).
    verbosity: Optional[str] = None
    # Anexos — DOIS transportes aceitos por item (37.0.0), ramificados pelo
    # campo content_base64:
    #   upload-ref (fluxo UI/modal em 2 passos): saída do POST /workspace/upload
    #     {filename, content_type, size, text_content, path};
    #   base64 (single-call p/ API): {filename, content_type?, content_base64}
    #     — mesmo decoder do invoke de agente; limites 5×10MB valem para o
    #     RAMO base64 (upload-refs seguem sem cap — fluxo da UI; endurecer é
    #     escopo da fatia de superfície/posse). Violação → 422 nomeado. No
    #     /invoke/async o ramo base64 ainda não é aceito (422).
    # Lista crua de propósito (união pydantic rejeitaria shapes parciais que a
    # UI já envia hoje). O engine roteia cada anexo aos agentes que aceitam
    # doc/imagem (dispatcher) — agentes que não aceitam ignoram.
    attachments: Optional[list] = None
    # Webhook de conclusão (35.6.0, SÓ no /invoke/async): URL notificada quando
    # o job termina (padrão fallback: este campo SOBREPÕE o webhook_url default
    # da API-key). Payload LEVE sem result (o receptor busca via GET autenticado)
    # + assinatura HMAC-SHA256 em X-Maestro-Signature (segredo = sha256 da key).
    # Validada com guarda SSRF no aceite E no envio. Ignorado no sync/stream.
    callback_url: Optional[str] = None
    # Cliente-final (35.9.0, LGPD-2): identificador do titular dos dados
    # (CPF/id/email). Guardado só como HASH (customer_hash) na interaction —
    # pivô do direito ao esquecimento (POST /privacy/forget). None = sem pivô.
    customer_ref: Optional[str] = None

class PlaygroundRunCreate(BaseModel):
    """Uma execução do Playground a persistir no histórico do usuário.

    Campos escalares = o CARTÃO que a UI lista. ``thread`` (opcional) carrega o payload
    COMPLETO ({result, timings, http}) p/ restaurar os painéis ao clicar — gravado à
    parte (``playground_run_threads``), com guarda de tamanho. A rota escopa ao user
    autenticado e trunca a mensagem defensivamente.
    """
    pipeline_id: Optional[str] = None
    pipeline_name: Optional[str] = None
    message: Optional[str] = None
    verbosity: Optional[str] = None
    status: Optional[str] = None
    size_bytes: Optional[int] = None
    duration_ms: Optional[int] = None
    # Thread COMPLETA da execução ({result, timings, http}) p/ restaurar os painéis
    # ao clicar no histórico. Guardada em tabela separada (TEXT/json.dumps) com guarda
    # de tamanho na rota. Opcional — sem ela, o clique só restaura a requisição.
    thread: Optional[dict] = None

class KnowledgeSourceCreate(BaseModel):
    name: str; description: Optional[str] = None
    source_type: Optional[str] = "manual"
    confidentiality_label: str = "internal"
    # Onda Tabular: kb_mode declara tipo de conteúdo.
    # text = só RAG; tabular = só DuckDB (sem chunks); hybrid = ambos (default).
    kb_mode: str = "hybrid"

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
    # Per-conector (39.0.0): 'inherit' | 'on' | 'off' — compõe com o toggle
    # global MCP_PER_TOOL_ENABLED (ver per_tool_enabled_for no runtime).
    per_tool_mode: Optional[str] = "inherit"

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
    per_tool_mode: Optional[str] = None

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
    """Alvo do harness (Pacote C): exatamente UM de agent_id | pipeline_id.
    agent_id era obrigatório; virou opcional para o modo pipeline — a rota
    valida o XOR (mantém 422/404 amigáveis em vez de erro de schema)."""
    release_id: str
    agent_id: Optional[str] = None
    pipeline_id: Optional[str] = None
    gold_version: str = "latest"; run_type: str = "baseline"

_MIN_PASSWORD_LEN = 8  # política mínima de senha (SKILL.md §1 / CWE-521)


class UserCreate(BaseModel):
    username: str
    password: str = Field(..., min_length=_MIN_PASSWORD_LEN)
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

    @field_validator("password")
    @classmethod
    def _password_policy(cls, v):
        # None/"" = "não alterar a senha" (o handler só grava se truthy); qualquer
        # senha NOVA precisa cumprir o mínimo.
        if v and len(v) < _MIN_PASSWORD_LEN:
            raise ValueError(f"senha deve ter ao menos {_MIN_PASSWORD_LEN} caracteres")
        return v

class UserLogin(BaseModel):
    username: str
    password: str

class DomainCreate(BaseModel):
    name: str
    description: Optional[str] = ""