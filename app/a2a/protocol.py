"""Protocolo Agent2Agent (A2A) — §7.

Envelope tipado, IntentDescriptor, DelegationEnvelope, ContextDelta.
"""

import uuid
import time
import json
import hashlib
import hmac
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
    origin_workspace: str = ""  # PR8a: namespace da instância de origem (federação)
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

    # ── Assinatura HMAC para federação cross-instância (PR8a) ────────────
    # `sign()` acima é um DIGEST sem segredo (sha256 de {id,target,skill}) — ok
    # para correlação intra-mesh, mas FORJÁVEL: qualquer um recomputa. Para
    # confiança entre instâncias, HMAC-SHA256 com um segredo compartilhado por
    # peer. O payload assinado é EXPLICITAMENTE enumerado (não `asdict`) para que
    # adicionar campos ao dataclass NUNCA mude silenciosamente a superfície
    # assinada. PR8b2 liga adicionalmente método+caminho+sha256(body) HTTP.

    _SIG_ALG = "hmac-sha256"

    def _canonical_signing_payload(self) -> str:
        """JSON determinístico dos campos que AUTORIZAM a delegação (chaves
        ordenadas, sem espaços). `context` e `intent` entram como hash para não
        inflar a assinatura.

        ASSINADOS: alvo (target_agent_id, target_skill_urn, skill_ref), ação
        (intent), dados (context), orçamento (budget), prazo (deadline), origem
        (origin_workspace), id/nonce (envelope_id), created_at, alg.
        NÃO assinados de propósito: campos de TRANSPORTE/correlação que não
        autorizam nada e mudam por hop — trace_id, span_id, parent_span_id,
        state_pointer, status. PR8b2 liga adicionalmente método+caminho+
        sha256(body) HTTP (cobre o payload da requisição inteira)."""
        ctx = json.dumps(self.context or {}, sort_keys=True, separators=(",", ":"))
        ctx_hash = hashlib.sha256(ctx.encode("utf-8")).hexdigest()
        intent_obj = asdict(self.intent) if self.intent else {}
        intent_canon = json.dumps(intent_obj, sort_keys=True, separators=(",", ":"))
        intent_hash = hashlib.sha256(intent_canon.encode("utf-8")).hexdigest()
        b = self.budget_remaining or Budget()
        payload = {
            "alg": self._SIG_ALG,
            "envelope_id": self.envelope_id,  # também serve de nonce (único por envelope)
            "origin_workspace": self.origin_workspace,
            "target_agent_id": self.target_agent_id,
            "target_skill_urn": self.target_skill_urn,
            "skill_ref": self.skill_ref,
            "intent_sha256": intent_hash,
            "context_sha256": ctx_hash,
            "budget": {"tokens": b.tokens, "wall_ms": b.wall_ms, "usd": b.usd},
            "deadline": self.deadline,
            "created_at": self.created_at,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def sign_hmac(self, secret: str) -> str:
        """Assina com HMAC-SHA256(segredo) sobre o payload canônico. Grava e
        devolve a assinatura (hex)."""
        if not secret:
            raise ValueError("sign_hmac exige um segredo não vazio")
        mac = hmac.new(
            secret.encode("utf-8"),
            self._canonical_signing_payload().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.signature = mac
        return mac

    def verify_hmac(self, secret: str, signature: Optional[str] = None) -> bool:
        """Verifica a assinatura HMAC em tempo constante. Usa `self.signature`
        se `signature` não for passado. False se o segredo, a assinatura ou
        qualquer campo canônico divergir."""
        if not secret:
            return False
        candidate = signature if signature is not None else self.signature
        if not candidate:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            self._canonical_signing_payload().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, candidate)

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
