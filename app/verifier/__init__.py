"""Verifier — camada de avaliação independente (judge).

Promove o que era `EvidenceChecker` em app/evidence/runtime.py para 1ª classe,
deixando claro que VERIFICAÇÃO é diferente de RAG (retrieval).

Uso:
    from app.verifier import Verifier, VerificationResult, verifier

    result = await verifier.verify(
        draft="...",
        evidences=[...],
        output_contract="...",
        guardrails="...",
        user_question="...",
        profile="rigorous",
        turn_id="...",
        interaction_id="...",
    )

Back-compat:
    `EvidenceChecker` é alias de `Verifier` (legacy import).
    Importar `evidence_checker` ainda funciona — reaponta para o singleton novo.
"""

from app.verifier.runtime import (
    Verifier,
    VerificationResult,
    verifier,
)

# Aliases para back-compat com código existente
EvidenceChecker = Verifier
evidence_checker = verifier

__all__ = [
    "Verifier",
    "VerificationResult",
    "verifier",
    "EvidenceChecker",   # alias legacy
    "evidence_checker",  # alias legacy
]
