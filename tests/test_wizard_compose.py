"""Testes do "IA, me ajude!" do Composer de Missão (/api/v1/wizard/compose).

Slice "IA, me ajude!": dentro do modal Composer (orquestrador/roteador), um
botão gera um RASCUNHO estruturado dos campos (missão, regras quando→destino,
fallback, regra de ouro) a partir de uma intenção em linguagem natural —
ANCORADO no catálogo real de skills/agentes do usuário (mitiga alucinação de
destinos inexistentes). O frontend NÃO auto-aplica: preenche para revisão.

A persona muda por camada (mesmo espírito do _mentor_persona/_refine_persona):
- aobd (Orquestrador) → regras = critérios de delegação; goldenRule=true
- router (Roteador/AR) → regras = categorias/destinos; goldenRule=false

Testes puros (sem DB nem rede): `_compose_persona`, `_compose_catalog_names`,
`_build_compose_catalog`, `_parse_compose_json` e `_ground_compose_targets` são
funções puras; na rota, `_resolve_wizard_llm` e `get_provider` são mockados —
capturamos as mensagens para provar persona + catálogo + intenção, e o conteúdo
devolvido para provar parse + grounding + graceful.
"""
from __future__ import annotations

import json

import pytest

from app.routes import wizard


# ═════════════════════════════════════════════════════════════════
# _compose_persona — função pura de seleção de persona por camada
# ═════════════════════════════════════════════════════════════════
class TestComposePersona:
    def test_aobd_foca_em_delegacao(self):
        p = wizard._compose_persona("aobd")
        assert wizard._COMPOSE_PERSONA_AOBD in p
        assert "Orquestrador" in p
        assert "DELEGA" in p
        assert "goldenRule=true" in p

    def test_router_foca_em_classificacao(self):
        p = wizard._compose_persona("router")
        assert wizard._COMPOSE_PERSONA_AR in p
        assert "Roteador" in p
        assert "CLASSIFICA" in p
        assert "goldenRule=false" in p

    def test_vazio_cai_no_aobd(self):
        # O Composer só existe para aobd/router; default sensato = orquestrador.
        assert wizard._COMPOSE_PERSONA_AOBD in wizard._compose_persona("")

    def test_none_cai_no_aobd(self):
        assert wizard._COMPOSE_PERSONA_AOBD in wizard._compose_persona(None)  # type: ignore[arg-type]

    def test_desconhecido_cai_no_aobd(self):
        assert wizard._COMPOSE_PERSONA_AOBD in wizard._compose_persona("xpto")

    def test_case_e_whitespace_normalizados(self):
        assert wizard._COMPOSE_PERSONA_AR in wizard._compose_persona("  Router  ")
        assert wizard._COMPOSE_PERSONA_AOBD in wizard._compose_persona("AOBD")

    def test_regras_de_saida_sempre_presentes(self):
        """Independente da camada, as regras de saída JSON entram."""
        for kind in ("aobd", "router", "", "xpto"):
            p = wizard._compose_persona(kind)
            assert wizard._COMPOSE_RULES in p
            assert "JSON válido" in p

    def test_personas_distintas_por_camada(self):
        assert wizard._COMPOSE_PERSONA_AOBD != wizard._COMPOSE_PERSONA_AR

    def test_regras_pedem_ancoragem_no_catalogo(self):
        """A persona instrui o LLM a preferir nomes EXATOS do catálogo."""
        assert "catálogo" in wizard._COMPOSE_RULES
        assert "EXATOS" in wizard._COMPOSE_RULES


# ═════════════════════════════════════════════════════════════════
# _compose_catalog_names — normaliza strings/dicts em nomes únicos
# ═════════════════════════════════════════════════════════════════
class TestComposeCatalogNames:
    def test_strings_simples(self):
        assert wizard._compose_catalog_names(["A", "B"]) == ["A", "B"]

    def test_dicts_com_name(self):
        assert wizard._compose_catalog_names([{"name": "X"}, {"name": "Y"}]) == ["X", "Y"]

    def test_dedup_preserva_ordem(self):
        assert wizard._compose_catalog_names(["A", "A", "B", "A"]) == ["A", "B"]

    def test_descarta_vazios_e_whitespace(self):
        assert wizard._compose_catalog_names(["A", "", "  ", {"name": "  "}]) == ["A"]

    def test_tolera_tipos_invalidos(self):
        # Cliente malformado: números, None, dict sem name — não explode.
        assert wizard._compose_catalog_names(["A", 123, None, {"foo": 1}]) == ["A"]

    def test_none_e_vazio(self):
        assert wizard._compose_catalog_names(None) == []
        assert wizard._compose_catalog_names([]) == []

    def test_trim_dos_nomes(self):
        assert wizard._compose_catalog_names(["  A  ", {"name": " B "}]) == ["A", "B"]


# ═════════════════════════════════════════════════════════════════
# _build_compose_catalog — bloco de contexto com o catálogo real
# ═════════════════════════════════════════════════════════════════
class TestBuildComposeCatalog:
    def test_inclui_cabecalho(self):
        cat = wizard._build_compose_catalog(["S"], ["A"])
        assert "[CATÁLOGO DE DESTINOS DISPONÍVEIS]" in cat

    def test_lista_skills_e_agentes(self):
        cat = wizard._build_compose_catalog(["Cobrança", "Suporte"], ["Vendas Bot"])
        assert "Cobrança" in cat and "Suporte" in cat
        assert "Vendas Bot" in cat

    def test_aceita_dicts(self):
        cat = wizard._build_compose_catalog([{"name": "SkX"}], [{"name": "AgY"}])
        assert "SkX" in cat and "AgY" in cat

    def test_skills_vazias_avisa(self):
        cat = wizard._build_compose_catalog([], ["A"])
        assert "nenhuma cadastrada" in cat

    def test_agentes_vazios_avisa(self):
        cat = wizard._build_compose_catalog(["S"], [])
        assert "nenhum cadastrado" in cat

    def test_instrui_a_preferir_o_catalogo(self):
        cat = wizard._build_compose_catalog(["S"], ["A"])
        assert "PREFERENCIALMENTE" in cat


# ═════════════════════════════════════════════════════════════════
# _parse_compose_json — extrai o rascunho (tolera fences e quebras)
# ═════════════════════════════════════════════════════════════════
class TestParseComposeJson:
    def _good(self):
        return json.dumps({
            "statement": "Coordenar atendimento",
            "rules": [
                {"when": "cobrança", "target": "Faturador"},
                {"when": "suporte", "target": "Suporte Bot"},
            ],
            "fallback": "pedir esclarecimento",
            "goldenRule": True,
        })

    def test_json_limpo(self):
        d = wizard._parse_compose_json(self._good(), "aobd")
        assert d["parsed"] is True
        assert d["statement"] == "Coordenar atendimento"
        assert len(d["rules"]) == 2
        assert d["rules"][0] == {"when": "cobrança", "target": "Faturador"}
        assert d["fallback"] == "pedir esclarecimento"
        assert d["goldenRule"] is True

    def test_json_com_fence(self):
        fenced = "```json\n" + self._good() + "\n```"
        d = wizard._parse_compose_json(fenced, "aobd")
        assert d["parsed"] is True
        assert d["statement"] == "Coordenar atendimento"

    def test_json_com_fence_sem_lang(self):
        fenced = "```\n" + self._good() + "\n```"
        d = wizard._parse_compose_json(fenced, "aobd")
        assert d["parsed"] is True

    def test_texto_livre_vira_rascunho_graceful(self):
        d = wizard._parse_compose_json("Desculpe, não consegui montar.", "aobd")
        assert d["parsed"] is False
        assert d["statement"].startswith("Desculpe")
        assert d["rules"] == []
        assert d["fallback"] == ""

    def test_graceful_goldenrule_por_camada(self):
        # Sem JSON: goldenRule default por camada (aobd True, router False).
        assert wizard._parse_compose_json("oops", "aobd")["goldenRule"] is True
        assert wizard._parse_compose_json("oops", "router")["goldenRule"] is False

    def test_graceful_trunca_texto_longo(self):
        d = wizard._parse_compose_json("Z" * 5000, "aobd")
        assert len(d["statement"]) == 1000

    def test_json_nao_objeto_vira_graceful(self):
        # Uma lista é JSON válido mas não é o objeto esperado → graceful.
        d = wizard._parse_compose_json("[1, 2, 3]", "aobd")
        assert d["parsed"] is False

    def test_goldenrule_explicito_respeitado(self):
        raw = json.dumps({"statement": "x", "rules": [], "fallback": "", "goldenRule": False})
        assert wizard._parse_compose_json(raw, "aobd")["goldenRule"] is False

    def test_goldenrule_ausente_default_por_camada(self):
        raw = json.dumps({"statement": "x", "rules": [], "fallback": ""})
        assert wizard._parse_compose_json(raw, "aobd")["goldenRule"] is True
        assert wizard._parse_compose_json(raw, "router")["goldenRule"] is False

    def test_regras_sanitizadas(self):
        raw = json.dumps({
            "statement": "x",
            "rules": [
                {"when": "a", "target": "T1"},
                "lixo",                          # não-dict → descartado
                {"when": "", "target": ""},      # vazia → descartada
                {"when": "  c  ", "target": "  T2  "},  # trim
            ],
            "fallback": "",
        })
        d = wizard._parse_compose_json(raw, "aobd")
        assert d["rules"] == [
            {"when": "a", "target": "T1"},
            {"when": "c", "target": "T2"},
        ]

    def test_sempre_retorna_as_chaves(self):
        for content in ("", "lixo", self._good()):
            d = wizard._parse_compose_json(content, "aobd")
            assert set(d) >= {"statement", "rules", "fallback", "goldenRule", "parsed"}


# ═════════════════════════════════════════════════════════════════
# _ground_compose_targets — canoniza targets para nomes do catálogo
# ═════════════════════════════════════════════════════════════════
class TestGroundComposeTargets:
    def _draft(self, *targets):
        return {"rules": [{"when": "w", "target": t} for t in targets]}

    def test_canoniza_caixa_de_agente(self):
        d = wizard._ground_compose_targets(
            self._draft("faturador x"), skills=[], agents=["Faturador X"]
        )
        assert d["rules"][0]["target"] == "Faturador X"

    def test_canoniza_caixa_de_skill(self):
        d = wizard._ground_compose_targets(
            self._draft("RESUMIR boleto"), skills=["Resumir Boleto"], agents=[]
        )
        assert d["rules"][0]["target"] == "Resumir Boleto"

    def test_sem_match_mantem_texto_livre(self):
        d = wizard._ground_compose_targets(
            self._draft("Algo Inexistente"), skills=["S"], agents=["A"]
        )
        assert d["rules"][0]["target"] == "Algo Inexistente"

    def test_agente_tem_precedencia_em_colisao(self):
        # Mesmo nome em skill e agente: o agente (nó de mesh) é o canônico.
        d = wizard._ground_compose_targets(
            self._draft("duplicado"),
            skills=[{"name": "Duplicado"}],
            agents=[{"name": "DUPLICADO"}],
        )
        assert d["rules"][0]["target"] == "DUPLICADO"

    def test_tolera_draft_sem_rules(self):
        d = wizard._ground_compose_targets({}, skills=["S"], agents=["A"])
        assert d == {}

    def test_aceita_dicts_no_catalogo(self):
        d = wizard._ground_compose_targets(
            self._draft("vendas bot"), skills=[], agents=[{"name": "Vendas Bot"}]
        )
        assert d["rules"][0]["target"] == "Vendas Bot"


# ═════════════════════════════════════════════════════════════════
# WizardComposeRequest — modelo (defaults retrocompat)
# ═════════════════════════════════════════════════════════════════
class TestWizardComposeRequestModel:
    def test_defaults(self):
        m = wizard.WizardComposeRequest(intent="coordenar atendimento")
        assert m.kind == "aobd"
        assert m.skills == []
        assert m.agents == []
        assert m.task_type == ""
        assert m.provider == "openai"

    def test_aceita_overrides(self):
        m = wizard.WizardComposeRequest(
            intent="triar", kind="router",
            skills=["A"], agents=[{"name": "B"}], task_type="reasoning",
        )
        assert m.kind == "router"
        assert m.skills == ["A"]
        assert m.agents == [{"name": "B"}]


# ═════════════════════════════════════════════════════════════════
# Rota /compose — persona + catálogo + parse + grounding (mocks)
# ═════════════════════════════════════════════════════════════════
def _patch_llm(monkeypatch, content: str = ""):
    """Mocka _resolve_wizard_llm + get_provider; captura mensagens.

    `content` é o que o "LLM" devolve (JSON string, fenced ou lixo). Default =
    um JSON válido genérico.
    """
    if not content:
        content = json.dumps({
            "statement": "Coordenar atendimento ao cliente",
            "rules": [{"when": "cobrança", "target": "Faturador"}],
            "fallback": "pedir esclarecimento",
            "goldenRule": True,
        })
    captured: dict = {}

    async def _fake_resolve(data, route):
        captured["route"] = route
        return ("openai", "gpt-4o-mini", "reasoning")

    class _FakeProvider:
        async def generate(self, messages, **kwargs):
            captured["messages"] = messages
            captured["system"] = next(
                (m["content"] for m in messages if m["role"] == "system"), None
            )
            captured["user"] = next(
                (m["content"] for m in messages if m["role"] == "user"), None
            )
            return {"content": content}

    def _fake_get_provider(provider_name, **kwargs):
        captured["provider_name"] = provider_name
        return _FakeProvider()

    monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)
    monkeypatch.setattr(wizard, "get_provider", _fake_get_provider)
    return captured


class TestWizardComposeRoute:
    @pytest.mark.asyncio
    async def test_resposta_no_formato_esperado(self, monkeypatch):
        _patch_llm(monkeypatch)
        out = await wizard.wizard_compose(
            wizard.WizardComposeRequest(intent="coordenar atendimento", kind="aobd")
        )
        assert out["status"] == "ok"
        assert "draft" in out
        assert out["draft"]["statement"] == "Coordenar atendimento ao cliente"
        assert out["draft"]["parsed"] is True

    @pytest.mark.asyncio
    async def test_aobd_usa_persona_de_delegacao(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_compose(
            wizard.WizardComposeRequest(intent="x", kind="aobd")
        )
        assert wizard._COMPOSE_PERSONA_AOBD in captured["system"]

    @pytest.mark.asyncio
    async def test_router_usa_persona_de_classificacao(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_compose(
            wizard.WizardComposeRequest(intent="x", kind="router")
        )
        assert wizard._COMPOSE_PERSONA_AR in captured["system"]

    @pytest.mark.asyncio
    async def test_catalogo_real_vai_no_system(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_compose(
            wizard.WizardComposeRequest(
                intent="x", kind="aobd",
                skills=["Resumir Boleto"], agents=["Faturador X"],
            )
        )
        assert "Resumir Boleto" in captured["system"]
        assert "Faturador X" in captured["system"]

    @pytest.mark.asyncio
    async def test_intencao_vai_como_mensagem_user(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_compose(
            wizard.WizardComposeRequest(intent="MINHA_INTENCAO", kind="aobd")
        )
        assert captured["user"] == "MINHA_INTENCAO"

    @pytest.mark.asyncio
    async def test_resolve_llm_pela_rota_compose(self, monkeypatch):
        captured = _patch_llm(monkeypatch)
        await wizard.wizard_compose(
            wizard.WizardComposeRequest(intent="x", kind="aobd")
        )
        assert captured["route"] == "compose"

    @pytest.mark.asyncio
    async def test_grounding_canoniza_targets_no_draft(self, monkeypatch):
        # LLM devolve target em caixa errada; o catálogo canoniza no draft.
        content = json.dumps({
            "statement": "x",
            "rules": [{"when": "cobrança", "target": "faturador x"}],
            "fallback": "",
            "goldenRule": True,
        })
        _patch_llm(monkeypatch, content=content)
        out = await wizard.wizard_compose(
            wizard.WizardComposeRequest(
                intent="x", kind="aobd", agents=["Faturador X"]
            )
        )
        assert out["draft"]["rules"][0]["target"] == "Faturador X"

    @pytest.mark.asyncio
    async def test_texto_livre_devolve_parsed_false(self, monkeypatch):
        _patch_llm(monkeypatch, content="desculpe, não consegui")
        out = await wizard.wizard_compose(
            wizard.WizardComposeRequest(intent="x", kind="aobd")
        )
        assert out["draft"]["parsed"] is False
        assert out["draft"]["statement"].startswith("desculpe")

    @pytest.mark.asyncio
    async def test_intencao_vazia_vira_http_400(self, monkeypatch):
        _patch_llm(monkeypatch)
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_compose(
                wizard.WizardComposeRequest(intent="   ", kind="aobd")
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_http_400_nao_eh_engolido_por_500(self, monkeypatch):
        _patch_llm(monkeypatch)
        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_compose(wizard.WizardComposeRequest(intent=""))
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_provider_error_vira_http_500(self, monkeypatch):
        async def _fake_resolve(data, route):
            return ("openai", "gpt-4o-mini", "reasoning")

        def _boom(*a, **k):
            raise RuntimeError("sem credencial")

        monkeypatch.setattr(wizard, "_resolve_wizard_llm", _fake_resolve)
        monkeypatch.setattr(wizard, "get_provider", _boom)

        with pytest.raises(wizard.HTTPException) as ei:
            await wizard.wizard_compose(
                wizard.WizardComposeRequest(intent="x", kind="aobd")
            )
        assert ei.value.status_code == 500

    @pytest.mark.asyncio
    async def test_compose_no_default_task_type(self):
        # Garante que a rota /compose tem default de roteamento (reasoning).
        assert wizard._DEFAULT_TASK_TYPE.get("compose") == "reasoning"
