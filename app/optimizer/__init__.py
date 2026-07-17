"""Módulo de Otimização de Prompt/Skill — arco DSPy-inspired (43.x–45.x).

PR2 (43.0.0): harness assíncrono + custo no ledger + teto por run.
PR3a (44.0.0): seam `config_overrides` no engine + McNemar pareado (medição).
PR3b (45.0.0): propositor GROUNDED de variantes (este pacote) — geração.

Princípios do arco (plano v2.1, decisões com o dono):
- Report-only: o módulo LÊ-avalia-PROPÕE; nunca aplica — promoção é humana.
- Seções seladas (## Decisions/## Inputs/contratos) jamais são otimizáveis.
- Segregação total: experimentos não contaminam baseline/drift/visões.
- Sem falsa confiança: vereditos pareados (McNemar), avisos explícitos.
"""
