"""Workspace package — helpers compartilhados entre rotas e UI.

Pacote criado na Onda A.1 da feature de slash command universal pra
invocação direta de bindings (MCP/API/RAG/Tabular) sem passar pelo LLM.

Módulos:
- binding_schema: normalizador que produz CanonicalFormSchema a partir
  do schema nativo de cada tipo de binding. Hoje só MCP; A.2+ adiciona
  API/RAG/Tabular.
"""
