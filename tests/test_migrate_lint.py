"""Migration lint (B03e): top-level transaction control is refused, quoting never is.

Run: python tests/test_migrate_lint.py   (standard library only, no database)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eduai_migrate import sqltok  # noqa: E402

MUST_PASS = {
    "plpgsql function body": """
        CREATE OR REPLACE FUNCTION edu_ai.f() RETURNS int LANGUAGE plpgsql AS $$
        BEGIN
            IF true THEN RETURN 1; END IF;
            RETURN 2;
        END;
        $$;""",
    "DO block": "DO $$ BEGIN PERFORM 1; END $$;",
    "DO block with tag and COMMIT text": "DO $body$ BEGIN RAISE NOTICE 'COMMIT'; END $body$;",
    "CASE ... END": "SELECT CASE WHEN 1 = 1 THEN 'a' ELSE 'b' END;\nUPDATE t SET x = CASE WHEN y THEN 1 END;",
    "COMMIT in a string": "SELECT 'COMMIT';\nSELECT 'BEGIN; END;';",
    "E-string with doubled quote": "SELECT E'it''s END';",
    "E-string with backslash quote": "SELECT E'it\\'s; COMMIT;';",
    "line comment": "-- COMMIT\nSELECT 1;",
    "block comment": "/* BEGIN */ SELECT 1;",
    "nested block comment": "/* a /* COMMIT; */ END; */ SELECT 1;",
    "dollar quote tagged": "SELECT $fn$ ... COMMIT; ... $fn$;",
    "column named end": 'ALTER TABLE edu_ai.t ADD COLUMN "end" int;\nSELECT "end", "COMMIT" FROM edu_ai.t;',
    "COMMENT ON is not COMMIT": "COMMENT ON TABLE edu_ai.t IS 'x';",
    "BEGIN ATOMIC function": """
        CREATE FUNCTION edu_ai.g() RETURNS int LANGUAGE sql
        BEGIN ATOMIC
            SELECT CASE WHEN true THEN 1 ELSE 2 END;
            SELECT 3;
        END;""",
    "semicolon inside parentheses": "CREATE RULE r AS ON INSERT TO edu_ai.t DO ALSO (SELECT 1; SELECT 2);",
    "positional parameter": "PREPARE p AS SELECT $1::int;",
    "last statement without semicolon": "SELECT 1",
    "only comments": "-- nothing here\n/* at all */",
    "transaction-safe CONCURRENTLY text": "SELECT 'CREATE INDEX CONCURRENTLY';",
    "words that merely start like keywords": "SELECT 1 AS commit_count, 2 AS beginning;",
}

# (name, sql, expected rule, expected line)
MUST_FAIL = [
    ("BEGIN", "BEGIN;", "tx-control", 1),
    ("begin transaction", "begin transaction;", "tx-control", 1),
    ("START TRANSACTION", "START TRANSACTION;", "tx-control", 1),
    ("COMMIT", "COMMIT;", "tx-control", 1),
    ("END", "END;", "tx-control", 1),
    ("ROLLBACK", "ROLLBACK;", "tx-control", 1),
    ("ABORT", "ABORT;", "tx-control", 1),
    ("COMMIT after a comment line", "-- migration 217\nSELECT 1;\n-- done\nCOMMIT", "tx-control", 4),
    ("COMMIT AND CHAIN", "SELECT 1; COMMIT AND CHAIN;", "tx-control", 1),
    ("END WORK", "END WORK;", "tx-control", 1),
    ("ROLLBACK TO SAVEPOINT", "SAVEPOINT s;\nROLLBACK TO SAVEPOINT s;", "tx-control", 2),
    ("PREPARE TRANSACTION", "PREPARE TRANSACTION 'x';", "tx-control", 1),
    ("COMMIT PREPARED", "COMMIT PREPARED 'x';", "tx-control", 1),
    ("ROLLBACK PREPARED", "ROLLBACK PREPARED 'x';", "tx-control", 1),
    ("COMMIT after a valid statement on the same line", "SELECT 1; COMMIT;", "tx-control", 1),
    ("CREATE INDEX CONCURRENTLY", "CREATE INDEX CONCURRENTLY i ON edu_ai.t (a);", "non-transactional", 1),
    ("CREATE UNIQUE INDEX CONCURRENTLY", "CREATE UNIQUE INDEX CONCURRENTLY i ON edu_ai.t (a);", "non-transactional", 1),
    ("DROP INDEX CONCURRENTLY", "DROP INDEX CONCURRENTLY edu_ai.i;", "non-transactional", 1),
    ("REINDEX CONCURRENTLY", "REINDEX TABLE CONCURRENTLY edu_ai.t;", "non-transactional", 1),
    ("DETACH PARTITION CONCURRENTLY", "ALTER TABLE edu_ai.t DETACH PARTITION p CONCURRENTLY;", "non-transactional", 1),
    ("VACUUM", "VACUUM edu_ai.t;", "non-transactional", 1),
    ("CREATE DATABASE", "CREATE DATABASE x;", "non-transactional", 1),
    ("DROP DATABASE", "DROP DATABASE x;", "non-transactional", 1),
    ("CREATE TABLESPACE", "CREATE TABLESPACE s LOCATION '/x';", "non-transactional", 1),
    ("ALTER SYSTEM", "ALTER SYSTEM SET work_mem = '1MB';", "non-transactional", 1),
    ("psql meta-command", "\\set ON_ERROR_STOP off", "psql-meta", 1),
    ("psql meta-command after SQL", "SELECT 1;\n\\i other.sql", "psql-meta", 2),
    ("unterminated dollar quote", "SELECT 1;\nDO $$ BEGIN PERFORM 1; END;", "lex", 2),
    ("unterminated string", "SELECT 'abc;", "lex", 1),
    ("unterminated block comment", "SELECT 1; /* open", "lex", 1),
    ("unterminated identifier", 'SELECT "abc;', "lex", 1),
]


class LintTest(unittest.TestCase):
    def test_must_pass(self):
        for name, sql in MUST_PASS.items():
            with self.subTest(name):
                self.assertEqual([], sqltok.lint_migration(sql))

    def test_must_fail_with_rule_and_line(self):
        for name, sql, rule, line in MUST_FAIL:
            with self.subTest(name):
                found = sqltok.lint_migration(sql)
                self.assertTrue(found, f"{name}: not rejected")
                self.assertIn((rule, line), [(f.rule, f.line) for f in found])

    def test_message_names_file_and_line(self):
        f = sqltok.lint_migration("SELECT 1;\nCOMMIT;")[0]
        self.assertTrue(f.format("217_x.sql").startswith("217_x.sql:2: "))

    def test_begin_atomic_does_not_hide_a_later_commit(self):
        sql = "CREATE FUNCTION f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END;\nCOMMIT;"
        self.assertEqual([("tx-control", 2)], [(f.rule, f.line) for f in sqltok.lint_migration(sql)])

    def test_statement_split_matches_psql_rules(self):
        text = "SELECT 'a;b'; DO $$ BEGIN PERFORM 1; END $$; SELECT (1; 2);"
        self.assertEqual(3, len(sqltok.split_statements(text)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
