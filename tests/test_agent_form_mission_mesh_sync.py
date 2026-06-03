"""Smoke do template agent_form.html — dry-run de roteamento + Sincronizar com
AI Mesh (PR4, sobre o Composer de Missão da PR3).

Dentro do modal "Compor missão", cada regra "quando … → delegar a …" é
classificada em tempo real:
- agente real  → vira aresta no AI Mesh (orquestrador → agente)
- skill        → referência válida na prosa, mas NÃO é nó de mesh
- texto livre  → sem correspondência (ok, só não sincroniza)

O botão "Aplicar e sincronizar com AI Mesh" cria as conexões para os alvos que
são agentes reais. Em EDIÇÃO, cria já (dedup via topology). Em agente NOVO, os
alvos ficam "staged" e as conexões são criadas logo após o save() — quando o
orquestrador finalmente ganha um id. Falha parcial é tolerada e reportada.

100% frontend: resolve alvos client-side (availableAgents/availableSkills já
carregados) e usa endpoints existentes (POST /mesh/connections, GET
/mesh/topology, POST /agents). Como Alpine só roda no browser, travamos
contratos ESTRUTURAIS no HTML.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "app" / "templates" / "pages" / "agent_form.html"
)


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


class TestMeshSyncState:
    @pytest.mark.parametrize("marker", ["meshSyncing: false,", "pendingMeshTargets: [],"])
    def test_data_prop_defined(self, html, marker):
        assert marker in html, f"estado {marker!r} ausente no x-data()"


class TestMeshSyncMethods:
    @pytest.mark.parametrize(
        "method",
        [
            "_normName(s) {",
            "resolveAgentByName(name) {",
            "classifyTarget(name) {",
            "get missionRuleChecks() {",
            "get missionAgentTargets() {",
            "async _existingDownstreamIds(sourceId) {",
            "async _createMeshConnections(sourceId, targets) {",
            "_syncSummary(r, dup) {",
            "async syncMissionToMesh() {",
        ],
    )
    def test_method_defined_in_alpine_data(self, html, method):
        assert method in html, f"método {method!r} ausente no x-data()"


class TestTargetClassification:
    def test_classify_branches(self, html):
        assert "return 'agent';" in html
        assert "return 'skill';" in html
        assert "return 'free';" in html
        assert "return 'empty';" in html

    def test_resolution_is_normalized_against_real_agents(self, html):
        assert "this.availableAgents.find(a => this._normName(a.name) === n)" in html

    def test_skill_match_against_available_skills(self, html):
        assert "this.availableSkills.some(s => this._normName(s.name) === n)" in html

    def test_agent_targets_are_unique(self, html):
        """missionAgentTargets dedupa por id (não cria conexão duplicada)."""
        assert "const seen = new Set();" in html
        assert "if (a && !seen.has(a.id))" in html


class TestMeshApiUsage:
    def test_creates_connection_via_mesh_endpoint(self, html):
        assert "api.post('/api/v1/mesh/connections', {" in html

    def test_connection_payload_shape(self, html):
        assert "source_agent_id: sourceId, target_agent_id: t.id," in html
        assert "connection_type: 'sequential', config: '{}'," in html

    def test_dedup_reads_topology(self, html):
        assert "api.get('/api/v1/mesh/topology')" in html
        assert ".filter(e => e.source === sourceId)" in html


class TestSyncBehavior:
    def test_applies_prompt_before_syncing(self, html):
        """Sync sempre aplica o prompt — evita mesh sem missão / missão sem mesh."""
        # composeMissionPrompt aparece em applyMissionComposer E em syncMissionToMesh
        assert html.count("this.form.system_prompt = this.composeMissionPrompt();") >= 2

    def test_edit_mode_creates_immediately_with_dedup(self, html):
        assert "if (this.isEdit) {" in html
        assert "await this._existingDownstreamIds(editId)" in html
        assert "this._createMeshConnections(editId, toCreate)" in html

    def test_new_agent_stages_targets(self, html):
        assert "this.pendingMeshTargets = targets.slice();" in html

    def test_guards_mission_and_targets(self, html):
        assert "Informe a missão antes de sincronizar" in html
        assert "Nenhuma regra aponta para um agente real" in html


class TestSaveIntegration:
    def test_capture_new_agent_id(self, html):
        assert "const created = await api.post('/api/v1/agents', payload);" in html

    def test_creates_staged_connections_after_save(self, html):
        assert "this.pendingMeshTargets.length && created?.id" in html
        assert "this._createMeshConnections(created.id, this.pendingMeshTargets)" in html

    def test_clears_staged_after_creation(self, html):
        assert "this.pendingMeshTargets = [];" in html


class TestPartialFailureHandling:
    def test_collects_failures(self, html):
        assert "const failed = [];" in html
        assert "failed.push(t.name);" in html

    def test_summary_reports_failures(self, html):
        assert "if (r.failed.length)" in html

    def test_error_toast_when_any_failure(self, html):
        assert "r.failed.length ? 'error' : 'success'" in html


class TestDryRunUI:
    def test_section_shown_when_rules_have_targets(self, html):
        assert 'x-show="missionRuleChecks.length"' in html

    def test_renders_per_target_classification(self, html):
        assert 'x-for="(chk, i) in missionRuleChecks"' in html

    def test_badge_classes_per_kind(self, html):
        for cls in ("bg-emerald-100 text-emerald-700", "bg-indigo-100 text-indigo-700", "bg-amber-100 text-amber-700"):
            assert cls in html, f"badge sem classe {cls!r}"

    def test_badge_labels(self, html):
        assert "✓ agente" in html
        assert "🔗 skill" in html
        assert "⚠ texto livre" in html

    def test_sync_button_wired_and_guarded(self, html):
        assert '@click="syncMissionToMesh()"' in html
        assert ':disabled="meshSyncing || !missionAgentTargets.length"' in html

    def test_syncable_count_displayed(self, html):
        assert "missionAgentTargets.length + ' agente(s) sincronizável(is)'" in html

    def test_new_agent_hint(self, html):
        assert 'x-show="!isEdit && missionAgentTargets.length"' in html
        assert "as conexões serão criadas no AI Mesh ao salvar" in html
