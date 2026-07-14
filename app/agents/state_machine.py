"""Máquina de Estados da Interação — §15.

Estados: Intake → PolicyCheck → RetrieveEvidence → DraftAnswer →
         VerifyEvidence → Recommend|Refuse|Escalate → LogAndClose

Invariantes:
- Todo caminho termina em LogAndClose
- VerifyEvidence é obrigatório
- Transições são atômicas e auditadas
"""

import time
import uuid
import json
import logging
from dataclasses import dataclass, field

from app.core.datetime_utils import naive_utc_now
from enum import Enum
from app.core.database import interactions_repo, turns_repo, audit_repo
from app.core.config import get_settings
from app.core.dlp import redact_for_persist, count_pii
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)


def _maybe_redact(text: str) -> str:
    """Redacta PII se DLP estiver habilitado nas settings."""
    if not text:
        return text
    if get_settings().dlp_enabled:
        return redact_for_persist(text)
    return text


class State(str, Enum):
    INTAKE = "Intake"
    POLICY_CHECK = "PolicyCheck"
    RETRIEVE_EVIDENCE = "RetrieveEvidence"
    DRAFT_ANSWER = "DraftAnswer"
    VERIFY_EVIDENCE = "VerifyEvidence"
    RECOMMEND = "Recommend"
    REFUSE = "Refuse"
    ESCALATE = "Escalate"
    LOG_AND_CLOSE = "LogAndClose"


# Transições válidas
TRANSITIONS = {
    State.INTAKE: [State.POLICY_CHECK],
    State.POLICY_CHECK: [State.RETRIEVE_EVIDENCE, State.REFUSE],
    State.RETRIEVE_EVIDENCE: [State.DRAFT_ANSWER],
    State.DRAFT_ANSWER: [State.VERIFY_EVIDENCE],
    State.VERIFY_EVIDENCE: [State.RECOMMEND, State.REFUSE, State.ESCALATE],
    State.RECOMMEND: [State.LOG_AND_CLOSE],
    State.REFUSE: [State.LOG_AND_CLOSE],
    State.ESCALATE: [State.LOG_AND_CLOSE],
    State.LOG_AND_CLOSE: [],
}

TERMINAL_STATES = {State.LOG_AND_CLOSE}


@dataclass
class TransitionCondition:
    name: str = ""
    evidence_ok: bool = False
    policy_ok: bool = True
    risk_high: bool = False
    fraud_suspected: bool = False


@dataclass
class InteractionContext:
    interaction_id: str = ""
    current_state: State = State.INTAKE
    agent_id: str = ""
    journey: str = ""
    channel: str = "api"
    actor: str = ""
    permissions: dict = field(default_factory=dict)
    evidences: list = field(default_factory=list)
    draft: str = ""
    final_output: str = ""
    evidence_score: float = 0.0
    transition_log: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # turn_number da request que iniciou esta execução. Em sessões novas
    # começa em 1; quando run_intake reusa session_id existente, fica em
    # max(turns_anteriores)+1. run_log_and_close usa next_user_turn+1
    # para gravar o output turn no DB sem sobrescrever turns antigos.
    next_user_turn: int = 1


class InteractionStateMachine:
    """FSM para processamento de interações conforme §15."""

    def __init__(self, ctx: InteractionContext):
        self.ctx = ctx

    def can_transition(self, target: State) -> bool:
        return target in TRANSITIONS.get(self.ctx.current_state, [])

    async def transition(self, target: State, condition: TransitionCondition = None):
        if not self.can_transition(target):
            raise ValueError(
                f"Transição inválida: {self.ctx.current_state.value} → {target.value}. "
                f"Permitidas: {[s.value for s in TRANSITIONS[self.ctx.current_state]]}"
            )
        from_state = self.ctx.current_state
        # Span da transição. Quando OTEL_ENABLED=false, é no-op (zero overhead).
        # Quando ligado, vira filho do span raiz da request HTTP (auto-instrumented),
        # então cada interação aparece no Tempo como árvore: POST → fsm.transition × N.
        with _tracer.start_as_current_span(f"fsm.transition:{from_state.value}->{target.value}") as span:
            span.set_attribute("fsm.from", from_state.value)
            span.set_attribute("fsm.to", target.value)
            span.set_attribute("fsm.condition", condition.name if condition else "")
            span.set_attribute("interaction.id", self.ctx.interaction_id or "")
            self.ctx.current_state = target
            entry = {
                "from": from_state.value,
                "to": target.value,
                "condition": condition.name if condition else "",
                # UTC-naive (não hora local): alinha com o ended_at/started_at das
                # tabelas (naive_utc_now) — antes `time.strftime` sem gmtime gravava
                # BRT e o dashboard exibia "terminou antes de começar".
                "timestamp": naive_utc_now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.ctx.transition_log.append(entry)

            # Persiste transição
            if self.ctx.interaction_id:
                await interactions_repo.update(self.ctx.interaction_id, {"state": target.value})
                await audit_repo.create({
                    "entity_type": "interaction",
                    "entity_id": self.ctx.interaction_id,
                    "action": f"state_transition:{from_state.value}→{target.value}",
                    "details": json.dumps(entry),
                })

            logger.info(f"Interaction {self.ctx.interaction_id}: {from_state.value} → {target.value}")

    async def run_intake(
        self,
        user_input: str,
        agent_id: str,
        journey: str = "",
        channel: str = "api",
        session_id: str | None = None,
    ):
        """Estado Intake: recebe solicitação, normaliza, constrói contexto.

        Reuso de sessão (2026-06-01): quando `session_id` é informado e já
        existe no DB, a interaction é REUTILIZADA — turn_number da request
        fica como max(turns existentes) + 1, sem `interactions_repo.create`.
        Sem isso, mensagens sucessivas com o mesmo `session_id` criavam
        interactions distintas no DB, e a sidebar do workspace
        fragmentava a conversa em entradas separadas. O `/chat` declarativo
        e `/invoke-binding-direct` já implementam essa lógica diretamente
        no handler; aqui replicamos no FSM para o branch standard.
        """
        requested = (session_id or "").strip() or None
        existing = None
        if requested:
            try:
                existing = await interactions_repo.find_by_id(requested)
            except Exception as e:  # find_by_id pode falhar (DB down etc)
                logger.warning(
                    "state_machine.run_intake.find_by_id_failed",
                    extra={
                        "event": "state_machine.session_lookup",
                        "session_id": requested,
                        "error_type": type(e).__name__,
                        "error_msg": str(e)[:200],
                    },
                )
                existing = None

        if existing:
            self.ctx.interaction_id = requested
            old_turns = await turns_repo.find_all(interaction_id=requested, limit=500)
            self.ctx.next_user_turn = max(
                (int(t.get("turn_number") or 0) for t in old_turns), default=0
            ) + 1
        else:
            self.ctx.interaction_id = requested or str(uuid.uuid4())
            self.ctx.next_user_turn = 1

        self.ctx.agent_id = agent_id
        self.ctx.journey = journey
        self.ctx.channel = channel
        # FIN-3 (35.12.0): âncora p/ latency_ms do turno de saída (monotonic
        # não retrocede; mede intake→close = a interação inteira).
        self.ctx.metadata["_t0_monotonic"] = time.monotonic()

        # Conta PII para audit antes de redactar (sinaliza que existia)
        pii = count_pii(user_input)
        if pii.total:
            self.ctx.metadata["pii_in_input"] = {
                "cpf": pii.cpf, "cnpj": pii.cnpj, "email": pii.email,
                "phone": pii.phone, "card": pii.card, "cep": pii.cep,
            }
            logger.info(f"PII detectada no input: total={pii.total} types={self.ctx.metadata['pii_in_input']}")

        if existing:
            # Reativa a sessão — não recria. ended_at é atualizado em LogAndClose.
            await interactions_repo.update(self.ctx.interaction_id, {
                "state": State.INTAKE.value,
            })
        else:
            # Dono na CRIAÇÃO (35.4.0): nasce carimbado quando o caller setou o
            # contexto — um aborto (timeout do invoke-job, crash) não deixa mais
            # interaction órfã sem dono (IDOR). Ver interaction_access.
            from app.core.interaction_access import (
                interaction_owner_for_creation, interaction_customer_hash_for_creation)
            _owner = interaction_owner_for_creation()
            _chash = interaction_customer_hash_for_creation()  # LGPD-2: pivô do esquecimento
            await interactions_repo.create({
                "id": self.ctx.interaction_id,
                "title": _maybe_redact(user_input)[:80].strip(),
                "agent_id": agent_id,
                "channel": channel,
                "journey_id": journey,
                "state": State.INTAKE.value,
                **({"owner_user_id": _owner} if _owner else {}),
                **({"customer_hash": _chash} if _chash else {}),
            })
        await turns_repo.create({
            "id": str(uuid.uuid4()),
            "turn_number": self.ctx.next_user_turn,
            "user_text_redacted": _maybe_redact(user_input),
            "interaction_id": self.ctx.interaction_id,
        })
        await self.transition(State.POLICY_CHECK)

    async def run_policy_check(self, policy_result: dict = None):
        """Estado PolicyCheck: avalia permissões via Policy Engine (OPA)."""
        result = policy_result or {"allowed": True, "tools": [], "budget": {}}
        self.ctx.permissions = result

        if not result.get("allowed", True):
            await self.transition(
                State.REFUSE,
                TransitionCondition(name="policy_denied", policy_ok=False),
            )
            return False
        await self.transition(State.RETRIEVE_EVIDENCE)
        return True

    async def run_retrieve_evidence(self, evidences: list):
        """Estado RetrieveEvidence: recebe evidências do Retriever+Reranker."""
        self.ctx.evidences = evidences
        await self.transition(State.DRAFT_ANSWER)

    async def run_draft_answer(self, draft: str):
        """Estado DraftAnswer: recebe rascunho do LLM."""
        self.ctx.draft = draft
        await self.transition(State.VERIFY_EVIDENCE)

    async def run_verify_evidence(self, verification_result: dict):
        """Estado VerifyEvidence: avalia consistência e cobertura."""
        is_ok = verification_result.get("ok", False)
        score = verification_result.get("confidence", 0.0)
        risk = verification_result.get("risk_high", False)
        fraud = verification_result.get("fraud_suspected", False)
        self.ctx.evidence_score = score

        if fraud or risk:
            await self.transition(
                State.ESCALATE,
                TransitionCondition(name="risk_or_fraud", risk_high=risk, fraud_suspected=fraud),
            )
        elif is_ok:
            await self.transition(
                State.RECOMMEND,
                TransitionCondition(name="evidence_ok", evidence_ok=True, policy_ok=True),
            )
        else:
            await self.transition(
                State.REFUSE,
                TransitionCondition(name="evidence_insufficient", evidence_ok=False),
            )

    async def run_recommend(self, final_output: str):
        """Estado Recommend: formata recomendação final com citações."""
        self.ctx.final_output = final_output
        await self.transition(State.LOG_AND_CLOSE)

    async def run_refuse(self, reason: str, next_step: str = ""):
        """Estado Refuse: emite recusa controlada com próximo passo."""
        ns = next_step or "Escalar para supervisor ou solicitar dado adicional."
        self.ctx.final_output = f"⚠ Recusa controlada: {reason}\n\nPróximo passo: {ns}"
        await self.transition(State.LOG_AND_CLOSE)

    async def run_escalate(self, reason: str):
        """Estado Escalate: delegação a supervisor humano."""
        self.ctx.final_output = f"🔺 Escalação: {reason}\n\nContexto: rascunho gerado com {len(self.ctx.evidences)} evidência(s). Requer revisão humana."
        await self.transition(State.LOG_AND_CLOSE)

    async def run_log_and_close(self):
        """Estado terminal: registra decisão, fecha ProcessContext."""
        if self.ctx.interaction_id:
            await interactions_repo.update(self.ctx.interaction_id, {
                "state": State.LOG_AND_CLOSE.value,
                "ended_at": naive_utc_now(),
            })
            # Registra turno de saída — output também é redactado.
            # Saída do LLM pode regurgitar PII vinda das evidências.
            # Em sessão nova: next_user_turn=1 → output_turn=2 (comportamento
            # legado preservado). Em sessão reutilizada: next_user_turn=N → output
            # turn=N+1, evitando sobrescrever turns anteriores.
            output_turn = (self.ctx.next_user_turn or 1) + 1
            # FIN-3 (35.12.0): grão por turno — tokens da geração (billed) e
            # latência intake→close no turno do ASSISTANT (o de input fica 0:
            # a geração pertence à resposta). Colunas existiam mortas no DDL.
            _tok = 0
            try:
                _tok = int((self.ctx.metadata.get("tokens") or {}).get("total_billed") or 0)
            except Exception:
                pass
            _lat = 0.0
            try:
                _t0 = self.ctx.metadata.get("_t0_monotonic")
                if _t0:
                    _lat = round((time.monotonic() - _t0) * 1000, 2)
            except Exception:
                pass
            await turns_repo.create({
                "id": str(uuid.uuid4()),
                "turn_number": output_turn,
                "output_text_redacted": _maybe_redact(self.ctx.final_output),
                "interaction_id": self.ctx.interaction_id,
                "tokens_used": _tok,
                "latency_ms": _lat,
            })
        return self.ctx