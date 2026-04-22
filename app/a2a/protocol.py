"""Protocolo Agent2Agent (A2A) — §7.

Envelope tipado, IntentDescriptor, DelegationEnvelope, ContextDelta.
"""

import uuid
import time
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional
from app.core.database import envelopes_repo


@dataclass
class IntentDescriptor:
    domain: str = ""
    process_candidate: str = ""
    entities: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)
    urgency: str = "normal"
    actor: str = ""


@dataclass
class Budget:
    tokens: int = 50000
    wall_ms: int = 120000
    usd: float = 1.0


@dataclass
class Envelope:
    envelope_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_span_id: str = ""
    origin_agent_id: str = ""
    origin_skill_urn: str = ""
    target_agent_id: str = ""
    target_skill_urn: str = ""
    intent: Optional[IntentDescriptor] = None
    skill_ref: str = ""
    context: dict = field(default_factory=dict)
    state_pointer: str = ""
    budget_remaining: Budget = field(default_factory=Budget)
    deadline: str = ""
    status: str = "pending"
    signature: str = ""
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def sign(self):
        payload = json.dumps({"id": self.envelope_id, "target": self.target_agent_id, "skill": self.skill_ref}, sort_keys=True)
        self.signature = hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self):
        d = asdict(self)
        d["intent"] = json.dumps(asdict(self.intent)) if self.intent else "{}"
        d["context"] = json.dumps(self.context)
        d["budget_remaining"] = json.dumps(asdict(self.budget_remaining))
        return d


@dataclass
class ContextDelta:
    """Mudanças explícitas emitidas por subagente ao terminar."""
    agent_id: str = ""
    skill_ref: str = ""
    additions: dict = field(default_factory=dict)
    span_id: str = ""


def create_delegation_envelope(
    origin_agent_id: str,
    target_agent_id: str,
    skill_ref: str,
    intent: IntentDescriptor,
    context: dict = None,
    budget: Budget = None,
    parent_span_id: str = "",
) -> Envelope:
    """Cria DelegationEnvelope assinado do AOBD para AR ou do AR para SA."""
    env = Envelope(
        origin_agent_id=origin_agent_id,
        target_agent_id=target_agent_id,
        target_skill_urn=skill_ref,
        skill_ref=skill_ref,
        intent=intent,
        context=context or {},
        budget_remaining=budget or Budget(),
        parent_span_id=parent_span_id,
    )
    env.sign()
    return env


async def persist_envelope(env: Envelope) -> dict:
    """Persiste envelope no banco."""
    data = {
        "id": env.envelope_id,
        "trace_id": env.trace_id,
        "span_id": env.span_id,
        "parent_span_id": env.parent_span_id,
        "origin_agent_id": env.origin_agent_id,
        "origin_skill_urn": env.origin_skill_urn,
        "target_agent_id": env.target_agent_id,
        "target_skill_urn": env.target_skill_urn,
        "intent": json.dumps(asdict(env.intent)) if env.intent else "{}",
        "skill_ref": env.skill_ref,
        "context": json.dumps(env.context),
        "state_pointer": env.state_pointer,
        "budget_remaining": json.dumps(asdict(env.budget_remaining)),
        "deadline": env.deadline,
        "status": env.status,
        "signature": env.signature,
    }
    return await envelopes_repo.create(data)


def apply_context_delta(current_context: dict, delta: ContextDelta) -> dict:
    """Aplica ContextDelta append-only ao contexto corrente."""
    merged = {**current_context}
    for k, v in delta.additions.items():
        if k in merged and isinstance(merged[k], list) and isinstance(v, list):
            merged[k] = merged[k] + v
        else:
            merged[k] = v
    merged.setdefault("_deltas", [])
    merged["_deltas"].append({"agent": delta.agent_id, "skill": delta.skill_ref, "span": delta.span_id})
    return merged
