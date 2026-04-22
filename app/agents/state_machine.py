"""Máquina de Estados da Interação — §15.

Estados: Intake → PolicyCheck → RetrieveEvidence → DraftAnswer →
         VerifyEvidence → Recommend|Refuse|Escalate → LogAndClose

Invariantes:
- Todo caminho termina em LogAndClose
- VerifyEvidence é obrigatório
- Transições são atômicas e auditadas
"""

import uuid
import time
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from app.core.database import interactions_repo, turns_repo, audit_repo

logger = logging.getLogger(__name__)


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
        self.ctx.current_state = target
        entry = {
            "from": from_state.value,
            "to": target.value,
            "condition": condition.name if condition else "",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
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

    async def run_intake(self, user_input: str, agent_id: str, journey: str = "", channel: str = "api"):
        """Estado Intake: recebe solicitação, normaliza, constrói contexto."""
        self.ctx.interaction_id = str(uuid.uuid4())
        self.ctx.agent_id = agent_id
        self.ctx.journey = journey
        self.ctx.channel = channel

        await interactions_repo.create({
            "id": self.ctx.interaction_id,
            "title": user_input[:80].strip(),
            "agent_id": agent_id,
            "channel": channel,
            "journey_id": journey,
            "state": State.INTAKE.value,
        })
        await turns_repo.create({
            "id": str(uuid.uuid4()),
            "turn_number": 1,
            "user_text_redacted": user_input,
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
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            # Registra turno de saída
            await turns_repo.create({
                "id": str(uuid.uuid4()),
                "turn_number": 2,
                "output_text_redacted": self.ctx.final_output,
                "interaction_id": self.ctx.interaction_id,
            })
        return self.ctx