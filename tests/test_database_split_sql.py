"""Regressão do splitter de SQL (`app.core.database._split_sql`).

Bug histórico (2026-05): comentários `-- texto` com `;` interno cortavam
CREATE TABLE ao meio. Em produção (Hostinger VPS), asyncpg recusava com
'syntax error at end of input' porque o splitter entregava fragmentos
inválidos:

```sql
-- Onda 3 entrega apenas o manifest declarativo;   <-- ; corta aqui
CREATE TABLE catalog_recipes (...)               <-- statement quebrado
```

Estes testes garantem que o splitter:
1. SCHEMA real do projeto divide em N statements que começam com SQL valido
2. Comentário com `;` não corta o statement
3. Comentário com `'foo'` não entra em modo string
4. Aspas em strings reais continuam respeitadas
5. Dollar-quoting ($tag$ ... $tag$) continua funcionando
"""

from __future__ import annotations

from app.core.database import SCHEMA, _split_sql


class TestSchemaReal:
    def test_schema_inteiro_divide_em_statements_validos(self):
        """Cada statement do SCHEMA começa com SQL keyword. Sem fragmentos."""
        stmts = _split_sql(SCHEMA)
        valid_starts = {"CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "SELECT", "--"}
        for i, s in enumerate(stmts):
            stripped = s.strip()
            assert stripped, f"statement [{i}] está vazio"
            first_word = stripped.split()[0].upper()
            assert first_word in valid_starts, (
                f"statement [{i}] começa com {first_word!r}, esperado um de {valid_starts}. "
                f"Fragmento: {stripped[:120]!r}"
            )

    def test_schema_contem_tabelas_chave(self):
        """Sanity: tabelas-chave do projeto saem como statements únicos."""
        stmts = _split_sql(SCHEMA)
        tables_to_find = [
            "catalog_entries", "catalog_submissions", "catalog_capability_disclosure",
            "catalog_costs", "catalog_external_metadata", "catalog_recipes",
            "catalog_recipe_executions",
        ]
        for table in tables_to_find:
            matching = [s for s in stmts if f"CREATE TABLE IF NOT EXISTS {table}" in s]
            assert len(matching) == 1, (
                f"esperado 1 CREATE TABLE para {table}, encontrei {len(matching)}"
            )


class TestCommentSemicolon:
    def test_comentario_com_ponto_e_virgula_nao_corta(self):
        sql = """
        -- Este comentário tem ; no meio; deve ser ignorado.
        CREATE TABLE foo (id INT);
        """
        stmts = _split_sql(sql)
        assert len(stmts) == 1
        assert "CREATE TABLE foo" in stmts[0]

    def test_multiplos_comentarios_com_pontoevirgula(self):
        sql = """
        -- comentário 1; com ;
        -- comentário 2; mais ;;;
        CREATE TABLE bar (id INT);
        -- depois também; tem ;
        CREATE INDEX idx ON bar(id);
        """
        stmts = _split_sql(sql)
        assert len(stmts) == 2
        assert "CREATE TABLE bar" in stmts[0]
        assert "CREATE INDEX idx" in stmts[1]


class TestCommentApostrophe:
    def test_comentario_com_apostrofo_nao_entra_modo_string(self):
        """Antes do fix: `'foo'` em comentário entrava em modo string e
        o splitter consumia tudo depois como string até achar outro `'`,
        quebrando vários statements."""
        sql = """
        -- kind='recipe' está em comentário
        CREATE TABLE x (id INT);
        CREATE TABLE y (id INT);
        """
        stmts = _split_sql(sql)
        assert len(stmts) == 2
        assert "CREATE TABLE x" in stmts[0]
        assert "CREATE TABLE y" in stmts[1]

    def test_comentario_com_aspas_e_pontoevirgula(self):
        """Combo do bug original: comentário com `'string'; depois`."""
        sql = """
        -- Onda 3 entrega apenas o manifest declarativo;
        CREATE TABLE catalog_recipes (
            entry_id TEXT PRIMARY KEY,
            steps JSONB NOT NULL DEFAULT '[]'
        );
        """
        stmts = _split_sql(sql)
        assert len(stmts) == 1, f"esperado 1 statement, vieram {len(stmts)}: {[s[:50] for s in stmts]}"
        assert "CREATE TABLE catalog_recipes" in stmts[0]
        assert "DEFAULT '[]'" in stmts[0]


class TestStringRespected:
    def test_string_com_pontoevirgula_nao_corta(self):
        sql = """
        INSERT INTO foo VALUES ('a; b; c');
        INSERT INTO foo VALUES ('d');
        """
        stmts = _split_sql(sql)
        assert len(stmts) == 2
        assert "'a; b; c'" in stmts[0]
        assert "'d'" in stmts[1]

    def test_string_dupla_quote(self):
        sql = '''
        SELECT "col; name" FROM foo;
        SELECT 1;
        '''
        stmts = _split_sql(sql)
        assert len(stmts) == 2
        assert '"col; name"' in stmts[0]

    def test_string_aspa_dupla_escape(self):
        """`''` dentro de string é escape para um único `'`."""
        sql = "INSERT INTO t VALUES ('don''t; stop'); SELECT 1;"
        stmts = _split_sql(sql)
        assert len(stmts) == 2
        assert "'don''t; stop'" in stmts[0]


class TestDollarQuoting:
    def test_dollar_quote_simples(self):
        sql = """
        CREATE FUNCTION f() RETURNS void AS $$
        BEGIN
            RAISE NOTICE 'with; semicolon';
        END;
        $$ LANGUAGE plpgsql;
        SELECT 1;
        """
        stmts = _split_sql(sql)
        # função tem ; dentro mas $$..$$ protege; depois SELECT 1
        assert len(stmts) == 2
        assert "RAISE NOTICE" in stmts[0]
        assert "SELECT 1" in stmts[1]


class TestEdgeCases:
    def test_script_vazio(self):
        assert _split_sql("") == []
        assert _split_sql("   \n\t\n") == []

    def test_apenas_comentarios(self):
        sql = """
        -- só comentário 1
        -- só comentário 2; com ;
        """
        # Comentário sem statement ainda gera 1 "statement" não vazio (comment puro).
        # PG aceita um statement só-comentário (no-op), então não é problema.
        stmts = _split_sql(sql)
        # O importante é não quebrar nem produzir lixo
        for s in stmts:
            assert s.strip().startswith("--")

    def test_statement_sem_ponto_e_virgula_final(self):
        sql = "CREATE TABLE foo (id INT)"
        stmts = _split_sql(sql)
        assert len(stmts) == 1
        assert "CREATE TABLE foo" in stmts[0]
