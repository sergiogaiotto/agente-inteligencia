"""Conftest raiz — fixtures compartilhadas.

A Onda 1 cobre apenas testes de lógica pura (Pydantic, parsers, state machines).
Integração com PostgreSQL fica para uma onda futura quando houver investimento
em test DB / fixture container (asyncpg + transação rollbackable).
"""
