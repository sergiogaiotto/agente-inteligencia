"""Pydantic models do catálogo — input/output validados.

Tabelas persistem JSON em colunas TEXT por convenção do projeto. Os models
expõem campos nativos (list/dict); serialização para banco fica nos endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.catalog.lifecycle import ENTRY_STATES, REVIEW_STATES
from app.catalog.urn import VALID_KINDS, is_valid_urn

EntryKind = Literal["agent", "skill", "application", "recipe", "external_platform"]
EntryStatus = Literal["draft", "submitted", "approved", "published", "deprecated", "archived"]
EntryVisibility = Literal["private", "department", "company"]
AdapterType = Literal["a2a", "mcp", "http", "openai_assistants"]
ArtifactType = Literal["agent", "skill", "recipe"]
ReviewStatus = Literal["pending", "approved", "rejected", "changes_requested"]
VerificationMethod = Literal["declared", "fingerprint", "execution"]


class CatalogEntryCreate(BaseModel):
    """Payload para criar entry. Cria sempre em status='draft'."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    kind: EntryKind
    artifact_type: Optional[ArtifactType] = None
    artifact_id: Optional[str] = None
    domain: Optional[str] = None
    version: str = "0.1.0"
    visibility: EntryVisibility = "private"
    visibility_scope: Optional[str] = None
    steward_team: Optional[str] = None
    adapter_type: AdapterType = "a2a"
    adapter_config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _version_semver(cls, v: str) -> str:
        import re
        if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", v):
            raise ValueError("version deve ser semver MAJOR.MINOR.PATCH")
        return v

    @field_validator("kind")
    @classmethod
    def _kind_allowed(cls, v: str) -> str:
        if v not in VALID_KINDS:
            raise ValueError(f"kind inválido. Esperado: {sorted(VALID_KINDS)}")
        return v

    def require_artifact_link(self) -> None:
        """agent/skill exigem vínculo a artefato. external_platform e recipe não.

        Chamado pelo handler do endpoint após validação base. Mantido aqui para
        co-localizar a regra com o model (não vira validator porque depende da
        regra de produto, não da forma).

        Recipe (Onda 3): não tem artefato — sua definição é a composição
        declarativa em catalog_recipes.steps.
        """
        if self.kind in ("agent", "skill"):
            if not self.artifact_type or not self.artifact_id:
                raise ValueError(
                    f"kind={self.kind} requer artifact_type + artifact_id (vínculo a artefato existente)"
                )


class CatalogEntryUpdate(BaseModel):
    """Update parcial — todos os campos opcionais. Status é alterado por endpoints
    dedicados (submit/approve/publish/deprecate), nunca por PUT direto."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    domain: Optional[str] = None
    version: Optional[str] = None
    visibility: Optional[EntryVisibility] = None
    visibility_scope: Optional[str] = None
    steward_team: Optional[str] = None
    adapter_config: Optional[dict] = None
    tags: Optional[list[str]] = None

    @field_validator("version")
    @classmethod
    def _version_semver(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import re
        if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", v):
            raise ValueError("version deve ser semver MAJOR.MINOR.PATCH")
        return v


class CatalogEntry(BaseModel):
    """Representação completa de uma entry (saída da API)."""

    id: str
    urn: str
    name: str
    description: str = ""
    kind: EntryKind
    artifact_type: Optional[ArtifactType] = None
    artifact_id: Optional[str] = None
    domain: Optional[str] = None
    version: str
    status: EntryStatus
    visibility: EntryVisibility
    visibility_scope: Optional[str] = None
    owner_user_id: str
    steward_team: Optional[str] = None
    adapter_type: AdapterType
    adapter_config: dict = Field(default_factory=dict)
    trust_reliability: float = 0.0
    trust_latency_p95_ms: float = 0.0
    trust_avg_cost_usd: float = 0.0
    trust_invocation_count: int = 0
    trust_last_invoked_at: Optional[datetime] = None
    tags: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    deprecated_at: Optional[datetime] = None

    @field_validator("urn")
    @classmethod
    def _urn_valid(cls, v: str) -> str:
        if not is_valid_urn(v):
            raise ValueError(f"URN inválido: {v}")
        return v

    @field_validator("status")
    @classmethod
    def _status_known(cls, v: str) -> str:
        if v not in ENTRY_STATES:
            raise ValueError(f"status desconhecido: {v}")
        return v


class CapabilityDisclosure(BaseModel):
    """Etiqueta nutricional R6.3 — declarada pelo publisher."""

    reads_user_kb: bool = False
    writes_user_kb: bool = False
    calls_external_apis: bool = False
    external_apis_list: list[str] = Field(default_factory=list)
    stores_input: bool = False
    storage_retention_days: Optional[int] = Field(None, ge=0)
    accesses_internet: bool = False
    processes_pii: bool = False
    processes_financial: bool = False
    processes_health: bool = False
    trains_on_input: bool = False
    output_is_deterministic: bool = False
    data_residency: Optional[str] = None
    additional_notes: str = ""
    verification_method: VerificationMethod = "declared"

    @field_validator("external_apis_list")
    @classmethod
    def _apis_consistent_with_flag(cls, v: list[str], info) -> list[str]:
        # Consistência: se declara que chama APIs externas, lista deve ter pelo menos 1.
        # Aplicado apenas quando calls_external_apis=True (validação relaxada caso contrário).
        if info.data.get("calls_external_apis") and not v:
            raise ValueError(
                "calls_external_apis=True exige external_apis_list não vazia"
            )
        return v


class SubmissionCreate(BaseModel):
    """Payload de submit. Snapshot e pré-checks são gerados pelo handler."""

    notes: str = ""


class SubmissionDecision(BaseModel):
    """Payload de aprovar/rejeitar/solicitar mudanças."""

    decision: Literal["approved", "rejected", "changes_requested"]
    notes: str = ""

    @field_validator("decision")
    @classmethod
    def _decision_known(cls, v: str) -> str:
        if v not in REVIEW_STATES or v == "pending":
            raise ValueError(f"decision inválida: {v}")
        return v


class Submission(BaseModel):
    """Representação de uma submissão (saída da API)."""

    id: str
    entry_id: str
    submitted_by: str
    submitted_at: Optional[datetime] = None
    snapshot: dict = Field(default_factory=dict)
    precheck_report: dict = Field(default_factory=dict)
    precheck_passed: bool = False
    review_status: ReviewStatus
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_notes: str = ""


# ─── External Platforms (Onda 2) ─────────────────────────────────


ContractStatus = Literal["none", "negotiating", "active", "expired", "terminated"]


class ExternalPlatformMetadata(BaseModel):
    """Metadata de plataforma externa (R10).

    Catalogada quando kind='external_platform'. Vendor é obrigatório na
    primeira escrita; demais campos são opcionais e atualizáveis individualmente.
    """

    vendor: Optional[str] = Field(None, min_length=1, max_length=200)
    vendor_url: Optional[str] = Field(None, max_length=500)
    contract_status: Optional[ContractStatus] = None
    contract_renewal_date: Optional[str] = None  # ISO date YYYY-MM-DD
    monthly_cost_usd: Optional[float] = Field(None, ge=0)
    vendor_contact: Optional[str] = Field(None, max_length=500)
    approved_use_cases: Optional[str] = None
    restrictions: Optional[str] = None
    approved_by_user_id: Optional[str] = None
    approved_at: Optional[datetime] = None

    @field_validator("contract_renewal_date")
    @classmethod
    def _date_iso(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("contract_renewal_date deve estar em ISO YYYY-MM-DD")
        return v


# ─── Stewardship reassign (Onda 2) ───────────────────────────────


class ReassignPayload(BaseModel):
    """Realoca owner e/ou steward de uma entry. Apenas Root.

    Pelo menos um dos dois campos deve vir preenchido. Para "limpar"
    o steward_team, mandar string vazia explicitamente.
    """

    new_owner_user_id: Optional[str] = None
    new_steward_team: Optional[str] = None

    def has_any_change(self) -> bool:
        return self.new_owner_user_id is not None or self.new_steward_team is not None


# ─── Bulk decide (Onda 2) ────────────────────────────────────────


class BulkDecisionPayload(BaseModel):
    """Decisão em batch sobre múltiplas submissões. Apenas Root.

    Mesma decision e notes aplicados a todas as submissions listadas.
    Falhas individuais (submission já decidida, transição inválida etc.)
    são reportadas no response — não interrompem as demais.
    """

    submission_ids: list[str] = Field(..., min_length=1, max_length=100)
    decision: Literal["approved", "rejected", "changes_requested"]
    notes: str = ""

    @field_validator("submission_ids")
    @classmethod
    def _ids_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("submission_ids contém duplicatas")
        return v


# ─── Cost & Consumption (Onda 3) ─────────────────────────────────


class InvocationCostRecord(BaseModel):
    """Registro de custo de uma invocação. Usado por integradores externos
    e (futuramente) pelo engine quando auto-wire estiver disponível.

    consumer_user_id é obrigatório — sem isso não dá pra fazer chargeback
    nem analisar consumo por área. Default = user atual no handler.
    """

    consumer_user_id: Optional[str] = None  # default: user.id
    consumer_department: Optional[str] = None
    interaction_id: Optional[str] = None
    cost_usd: float = Field(0, ge=0)
    tokens_used: int = Field(0, ge=0)
    latency_ms: float = Field(0, ge=0)


# ─── Recipes (Onda 3) ────────────────────────────────────────────


class RecipeStep(BaseModel):
    """Step individual em um recipe — referencia outra entry do catálogo."""

    order: int = Field(..., ge=1)
    target_entry_id: str = Field(..., min_length=1)
    notes: Optional[str] = None


class RecipeDefinition(BaseModel):
    """Definição completa de um recipe (lista ordenada de steps).

    Steps devem ter `order` único e contínuo a partir de 1. Validador
    normaliza/reordena para evitar gaps.
    """

    steps: list[RecipeStep] = Field(..., min_length=1, max_length=50)

    @field_validator("steps")
    @classmethod
    def _normalize_order(cls, steps: list[RecipeStep]) -> list[RecipeStep]:
        # Detecta orders duplicados
        orders = [s.order for s in steps]
        if len(set(orders)) != len(orders):
            raise ValueError("steps com 'order' duplicado")
        # Detecta target_entry_id duplicado no mesmo recipe — Onda 3
        # restringe (recipe que invoca a mesma entry 2x pode existir, mas é
        # raro; vamos permitir só para Onda 4 quando houver execução real).
        targets = [s.target_entry_id for s in steps]
        if len(set(targets)) != len(targets):
            raise ValueError("steps com target_entry_id duplicado — uma entry por step")
        # Reordena ascendente e renumera 1..N para garantir continuidade
        sorted_steps = sorted(steps, key=lambda s: s.order)
        for idx, s in enumerate(sorted_steps, start=1):
            s.order = idx
        return sorted_steps


# ─── Recipe Executions (Onda 4) ──────────────────────────────────


class RecipeExecutionRequest(BaseModel):
    """Payload para POST /entries/{id}/execute.

    `input` alimenta o step 1; output de cada step vira input do próximo
    (chain). Limite generoso (50k chars) para tolerar contexto inicial
    rico sem virar abuso de payload.
    """

    input: str = Field(..., min_length=1, max_length=50000)
