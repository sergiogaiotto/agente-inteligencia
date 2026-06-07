"""Fase B — Slice 2b (2026-06-07): o catálogo do /compose passa a incluir os
parâmetros declarados (## Inputs) de CADA destino, para a IA sugerir, por
destino, quais valores o roteador deve extrair (auto-sugestão de params).
"""
from __future__ import annotations


class TestSchemaParamNames:
    def test_properties_keys(self):
        from app.routes.wizard import _schema_param_names
        assert _schema_param_names(
            {"type": "object", "properties": {"cep": {"type": "string"}}}
        ) == ["cep"]

    def test_top_level_fallback(self):
        from app.routes.wizard import _schema_param_names
        assert set(_schema_param_names({"cep": {}, "uf": {}})) == {"cep", "uf"}

    def test_non_dict_returns_empty(self):
        from app.routes.wizard import _schema_param_names
        assert _schema_param_names(None) == []
        assert _schema_param_names("x") == []


class TestComposeCatalogParams:
    def test_includes_params_per_destination(self):
        from app.routes.wizard import _build_compose_catalog
        out = _build_compose_catalog(
            [], ["Busca endereço", "Tavily"], {"Busca endereço": ["cep"]}
        )
        assert "[PARÂMETROS POR DESTINO]" in out
        assert "Busca endereço requer: cep" in out
        # destino sem params declarados não entra no bloco
        assert "Tavily requer" not in out
        # instrução p/ a IA: extrair da mensagem
        assert "EXTRAÍDOS da mensagem" in out

    def test_backcompat_without_inputs(self):
        from app.routes.wizard import _build_compose_catalog
        out = _build_compose_catalog([], ["X"])  # sem agent_inputs
        assert "[PARÂMETROS POR DESTINO]" not in out
        assert "[CATÁLOGO DE DESTINOS DISPONÍVEIS]" in out
