"""Versão do produto (PR-driven) exibida no rodapé da UI.

ESQUEMA (definido com o usuário em 2026-06-06): ``MAJOR.MEDIUM.MINOR``,
incrementado A CADA PR conforme o tipo da mudança, no estilo SemVer — com
RESET dos níveis inferiores quando um nível mais alto sobe:

    - NOVA FUNCIONALIDADE   → incrementa MAJOR  (zera MEDIUM e MINOR)
                              ex.: 1.4.2 → 2.0.0
    - MELHORIA EM EXISTENTE → incrementa MEDIUM (zera MINOR)
                              ex.: 2.0.0 → 2.1.0
    - CORREÇÃO / BUGFIX     → incrementa MINOR
                              ex.: 2.1.0 → 2.1.1

Fonte ÚNICA da verdade: este arquivo. O ``main.py`` injeta ``app_version`` nos
globals do Jinja, então todos os templates leem ``{{ app_version }}`` (ver o
rodapé em ``templates/layouts/base.html``).

⚠️ AO ABRIR UM PR: atualize ``APP_VERSION`` abaixo conforme a regra (bump manual
por PR). NÃO confundir com a versão de spec/API ("2.0.0" em ``main.py``), que é
outra coisa.
"""

APP_VERSION = "36.0.0"
