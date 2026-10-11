#!/usr/bin/env python3
"""DB role contract (C2): SQL generator, guarded adoption of an existing synthetic installation, catalog verification.

    db_roles.py gen-sql [--out DIR]                     write the four psql files from postgres/role_contract/matrix.json
    db_roles.py prepare-env --env FILE                  an existing .env: generate the missing role secrets, set POSTGRES_DB_USER (no database access)
    db_roles.py check   --container C [--old-role R]    read-only: does the cluster match the known baseline?
    db_roles.py adopt   --container C --env FILE --confirm-isolated [--old-role R]
                                                         roles with passwords from the external .env, ownership, grants (guarded)
    db_roles.py reapply --container C --confirm-isolated  after a restore or a migration: roles, ownership and grants again
    db_roles.py verify  --container C [--json]          live catalog vs the matrix; exit 0 match, 1 deviation, 3 cannot verify
    db_roles.py demote-old --container C --old-role R --confirm-isolated
                                                         the old bootstrap role becomes NOLOGIN without a password
    db_roles.py globals --container C --out FILE        pg_dumpall --globals-only (0600; holds password hashes: private)

All access goes through `docker exec -i <container> psql` on the container's local socket (no password, no host port).
Passwords travel on stdin only. Output has role, object and table names and counts, never a password or row content.
Standard library only. Not for production or school installations: the contract and its baseline are for isolated synthetic ones.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
MATRIX_PATH = HERE.parent / "postgres" / "role_contract" / "matrix.json"
INIT_DIR = HERE.parent / "postgres" / "init_clean"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PASSWORD = re.compile(r"[A-Za-z0-9_-]{16,128}")
FILES = ("_role_contract_roles.psql", "_role_contract_cluster.psql", "_role_contract_edu_ai.psql", "_role_contract_langflow.psql")
PRELUDE = ["\\set ON_ERROR_STOP on", "SET search_path = pg_catalog, pg_temp;"]
NS_SQL = "SELECT nspname::text FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
EXT_SQL = "SELECT extname::text || chr(58) || extversion FROM pg_extension"
FN_SQL = "SELECT p.proname::text || chr(58) || p.prokind::text FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace WHERE s.nspname = 'edu_ai'"
ENUM_SQL = "SELECT t.typname::text FROM pg_type t JOIN pg_namespace s ON s.oid = t.typnamespace WHERE s.nspname = 'public' AND t.typtype = 'e'"
Q_RP, Q_V, Q_S = "'r','p'", "'v'", "'S'"
NOT_EXT = "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{cls}'::regclass AND d.objid = {alias}.oid AND d.deptype = 'e')"


class ContractError(Exception):
    pass


def _idents(node, out: set) -> None:
    """Every key and every string leaf that ends up in SQL must be a plain identifier."""
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            _idents(v, out)
    elif isinstance(node, list):
        for v in node:
            _idents(v, out)
    elif isinstance(node, str):
        out.add(node)


def load_matrix(path: pathlib.Path = MATRIX_PATH) -> dict:
    m = json.loads(path.read_text(encoding="utf-8"))
    names: set = set()
    for part in ("roles", "databases", "memberships", "legacy_roles", "runtime_roles", "public_revoked_databases", "langflow_db", "baseline"):
        _idents(m[part], names)
    e = dict(m["edu_ai_db"])
    e.pop("extension_owner_note", None)
    e["table_dml"] = {tbl: list(per) for tbl, per in e["table_dml"].items()}     # the I/U/D letters are validated below
    _idents({k: v for k, v in e.items()}, names)
    for key in ("note", "format", "contract"):
        names.discard(key)
    skip = {a["note"] for a in m["roles"].values()}
    for n in names - skip:
        if not isinstance(n, str) or not (IDENT.match(n) or re.fullmatch(r"[0-9.]+", n)):
            raise ContractError(f"unsafe identifier in the matrix: {n!r}")
    if not re.fullmatch(r"role-contract:[0-9]+", m["marker"]):
        raise ContractError("unsafe marker")
    for per_table in m["edu_ai_db"]["table_dml"].values():
        for role, privs in per_table.items():
            if role not in m["roles"] or not re.fullmatch(r"[IUD]{1,3}", privs):
                raise ContractError("invalid privilege entry in the matrix")
    return m


def lit(items) -> str:
    return "ARRAY[" + ", ".join("'" + str(i).replace("'", "''") + "'" for i in sorted(items)) + "]::text[]"


def contract_roles(m: dict) -> list[str]:
    return sorted(m["roles"])


# ── SQL generation ─────────────────────────────────────────────────────────────
def role_attrs(a: dict) -> str:
    return " ".join([("LOGIN" if a["login"] else "NOLOGIN"), ("SUPERUSER" if a["superuser"] else "NOSUPERUSER"),
                     ("CREATEDB" if a["createdb"] else "NOCREATEDB"), ("CREATEROLE" if a["createrole"] else "NOCREATEROLE"),
                     ("REPLICATION" if a["replication"] else "NOREPLICATION"), ("BYPASSRLS" if a["bypassrls"] else "NOBYPASSRLS"),
                     ("INHERIT" if a["inherit"] else "NOINHERIT")])


def gen_roles(m: dict) -> str:
    out = ["-- GENERATED from postgres/role_contract/matrix.json by scripts/db_roles.py gen-sql; do not edit by hand.",
           "-- Roles (no passwords here: init sets them from the environment, adoption from the external .env). Idempotent.", *PRELUDE]
    for role, a in m["roles"].items():
        attrs = role_attrs(a)
        out.append(f"SELECT 'CREATE ROLE {role} {attrs}' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') \\gexec")
        if role != "edu_ai_admin":     # the maintenance role may be the bootstrap role, whose attributes must not be touched
            out.append(f"ALTER ROLE {role} {attrs};")
        # The application roles resolve pg_trgm functions and operators (word_similarity, <->) unqualified, as the old single role
        # did through its own-name schema; every other role keeps the default path and so cannot be captured by objects in edu_ai.
        if a.get("search_path"):
            out.append(f"ALTER ROLE {role} SET search_path = {', '.join(a['search_path'])};")
        else:
            out.append(f"ALTER ROLE {role} RESET search_path;")
    pairs = ", ".join(f"('{x['role']}', '{x['member']}')" for x in m["memberships"])
    out.append("-- any membership of or in a contract role that the matrix does not list is removed (pg_* roles included)")
    out.append("DO $m$ DECLARE r record; BEGIN FOR r IN SELECT ro.rolname AS rn, u.rolname AS mn FROM pg_auth_members a "
               "JOIN pg_roles ro ON ro.oid = a.roleid JOIN pg_roles u ON u.oid = a.member "
               f"WHERE (u.rolname = ANY ({lit(contract_roles(m))}) OR ro.rolname = ANY ({lit(contract_roles(m))})) "
               f"AND (ro.rolname, u.rolname) NOT IN (VALUES {pairs}) LOOP EXECUTE format('REVOKE %I FROM %I', r.rn, r.mn); END LOOP; END $m$;")
    for x in m["memberships"]:
        out.append(f"GRANT {x['role']} TO {x['member']} WITH INHERIT {'TRUE' if x['inherit'] else 'FALSE'}, SET {'TRUE' if x['set'] else 'FALSE'};")
    return "\n".join(out) + "\n"


def gen_cluster(m: dict) -> str:
    out = ["-- GENERATED from postgres/role_contract/matrix.json; do not edit by hand.",
           "-- Database level: owners, PUBLIC CONNECT/TEMP removed, explicit CONNECT. Idempotent.", *PRELUDE]
    for db, d in m["databases"].items():
        out.append(f"ALTER DATABASE {db} OWNER TO {d['owner']};")
        out.append(f"REVOKE ALL ON DATABASE {db} FROM PUBLIC;")
        for r in sorted(m["roles"]):
            if r != d["owner"] and not m["roles"][r]["superuser"]:
                out.append(f"REVOKE ALL ON DATABASE {db} FROM {r};")
        for r in d["connect"]:
            if r != d["owner"]:
                out.append(f"GRANT CONNECT ON DATABASE {db} TO {r};")
    for db in m["public_revoked_databases"]:
        out.append(f"REVOKE ALL ON DATABASE {db} FROM PUBLIC;")
    return "\n".join(out) + "\n"


def same(subquery: str, expected) -> str:
    """SQL boolean: the set produced by `subquery` (text) equals the expected names."""
    e = lit(expected)
    return f"(ARRAY({subquery}) @> {e} AND {e} @> ARRAY({subquery}))"


def guard_block(kind: str, m: dict) -> list[str]:
    """DO block that refuses (before any write) unless the cluster is the known baseline or already the contract."""
    b = m["baseline"]
    roles = sorted(set(m["roles"]) | set(m.get("legacy_roles", [])))
    pairs = ", ".join(f"('{x['role']}', '{x['member']}')" for x in m["memberships"])
    parts = [
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> src AND rolname <> ALL ({lit(roles)})) THEN bad := bad || ' unknown_role'; END IF;",
        f"IF NOT {same('SELECT datname::text FROM pg_database', b['cluster_databases'])} THEN bad := bad || ' databases'; END IF;",
        "IF EXISTS (SELECT 1 FROM pg_auth_members a JOIN pg_roles ro ON ro.oid = a.roleid JOIN pg_roles u ON u.oid = a.member "
        f"WHERE ro.rolname !~ '^pg_' AND u.rolname !~ '^pg_' AND (ro.rolname, u.rolname) NOT IN (VALUES {pairs})) THEN bad := bad || ' memberships'; END IF;",
    ]
    rel_in = "FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = '{schema}' AND c.relkind IN ({kinds}) AND " + NOT_EXT.format(cls="pg_class", alias="c")
    if kind == "edu_ai":
        e, d = b["edu_ai"], m["edu_ai_db"]
        ext = [f"{k}:{v}" for k, v in e["extensions"].items()]
        parts += [
            f"IF NOT {same(NS_SQL, e['schemas'])} THEN bad := bad || ' schemas'; END IF;",
            f"IF NOT {same(EXT_SQL, ext)} THEN bad := bad || ' extensions'; END IF;",
            f"IF NOT {same('SELECT c.relname::text ' + rel_in.format(schema='edu_ai', kinds=Q_RP), d['table_dml'])} THEN bad := bad || ' tables'; END IF;",
            f"IF NOT {same('SELECT c.relname::text ' + rel_in.format(schema='edu_ai', kinds=Q_V), d['views'])} THEN bad := bad || ' views'; END IF;",
            f"IF NOT {same('SELECT c.relname::text ' + rel_in.format(schema='edu_ai', kinds=Q_S), e['sequences'])} THEN bad := bad || ' sequences'; END IF;",
            "IF EXISTS (SELECT 1 " + rel_in.format(schema="edu_ai", kinds="'m','f'") + ") THEN bad := bad || ' other_relations'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname IN ('public','paradedb','pdb') AND c.relkind IN ('r','p','v','m','f','S') AND "
            + NOT_EXT.format(cls="pg_class", alias="c") + ") THEN bad := bad || ' foreign_relations'; END IF;",
            f"IF NOT {same(FN_SQL + ' AND ' + NOT_EXT.format(cls='pg_proc', alias='p'), [x + ':f' for x in e['functions']])} THEN bad := bad || ' functions'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace WHERE s.nspname IN ('public','paradedb','pdb') AND " + NOT_EXT.format(cls="pg_proc", alias="p") + ") THEN bad := bad || ' foreign_functions'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace s ON s.oid = t.typnamespace WHERE s.nspname IN ('edu_ai','public','paradedb','pdb') AND t.typtype IN ('e','d','r','m') AND "
            + NOT_EXT.format(cls="pg_type", alias="t") + ") THEN bad := bad || ' types'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = 'edu_ai' AND c.relkind IN ('r','p','v','S') AND "
            + NOT_EXT.format(cls="pg_class", alias="c") + f" AND pg_get_userbyid(c.relowner) NOT IN (src, '{d['object_owner']}')) THEN bad := bad || ' foreign_owner'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace WHERE s.nspname = 'edu_ai' AND " + NOT_EXT.format(cls="pg_proc", alias="p")
            + f" AND pg_get_userbyid(p.proowner) NOT IN (src, '{d['object_owner']}')) THEN bad := bad || ' foreign_function_owner'; END IF;",
        ]
    else:
        f = b["langflow"]
        ext = [f"{k}:{v}" for k, v in f["extensions"].items()]
        tabs = same("SELECT c.relname::text " + rel_in.format(schema="public", kinds=Q_RP), f["tables"])
        seqs = same("SELECT c.relname::text " + rel_in.format(schema="public", kinds=Q_S), f["sequences"])
        enums = same(ENUM_SQL, f["enum_types"])
        none_t = same("SELECT c.relname::text " + rel_in.format(schema="public", kinds=Q_RP), [])
        none_s = same("SELECT c.relname::text " + rel_in.format(schema="public", kinds=Q_S), [])
        none_e = same(ENUM_SQL, [])
        parts += [
            f"IF NOT {same(NS_SQL, f['schemas'])} THEN bad := bad || ' schemas'; END IF;",
            f"IF NOT {same(EXT_SQL, ext)} THEN bad := bad || ' extensions'; END IF;",
            f"IF NOT (({tabs} AND {seqs} AND {enums}) OR ({none_t} AND {none_s} AND {none_e})) THEN bad := bad || ' vendor_objects'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = 'public' AND c.relkind IN ('v','m','f')) THEN bad := bad || ' other_relations'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace WHERE s.nspname = 'public') THEN bad := bad || ' functions'; END IF;",
            "IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = 'public' AND c.relkind IN ('r','p','v','S') "
            f"AND pg_get_userbyid(c.relowner) NOT IN (src, '{m['langflow_db']['owner']}')) THEN bad := bad || ' foreign_owner'; END IF;",
        ]
    block = ("DO $g$ DECLARE src text := current_setting('edu_ai.src'); bad text := ''; BEGIN " + " ".join(parts)
             + " IF bad <> '' THEN RAISE EXCEPTION 'baseline mismatch, nothing was written:%', bad; END IF; END $g$;")
    return ["SELECT set_config('edu_ai.src', :'src', false);", block]


def ownership_loop(schema: str, owner: str, with_functions: bool, types: bool) -> str:
    rel = ("FOR r IN SELECT format('%I.%I', s.nspname, c.relname) AS rel, c.relkind FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
           f"WHERE s.nspname = '{schema}' AND c.relkind IN ('r','p','v','S') AND pg_get_userbyid(c.relowner) = current_setting('edu_ai.src') "
           "AND " + NOT_EXT.format(cls="pg_class", alias="c") + " "
           "AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend o WHERE o.classid = 'pg_class'::regclass AND o.objid = c.oid AND o.refclassid = 'pg_class'::regclass AND o.deptype IN ('a','i'))) "
           "ORDER BY c.relname LOOP "
           f"IF r.relkind = 'S' THEN EXECUTE format('ALTER SEQUENCE %s OWNER TO {owner}', r.rel); ELSE EXECUTE format('ALTER TABLE %s OWNER TO {owner}', r.rel); END IF; END LOOP; ")
    fn = ("FOR r IN SELECT format('%I.%I(%s)', s.nspname, p.proname, pg_get_function_identity_arguments(p.oid)) AS fn FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace "
          f"WHERE s.nspname = '{schema}' AND pg_get_userbyid(p.proowner) = current_setting('edu_ai.src') AND " + NOT_EXT.format(cls="pg_proc", alias="p") + " LOOP "
          f"EXECUTE format('ALTER FUNCTION %s OWNER TO {owner}', r.fn); END LOOP; ") if with_functions else ""
    ty = ("FOR r IN SELECT format('%I.%I', s.nspname, t.typname) AS ty FROM pg_type t JOIN pg_namespace s ON s.oid = t.typnamespace "
          f"WHERE s.nspname = '{schema}' AND t.typtype = 'e' AND pg_get_userbyid(t.typowner) = current_setting('edu_ai.src') LOOP "
          f"EXECUTE format('ALTER TYPE %s OWNER TO {owner}', r.ty); END LOOP; ") if types else ""
    done = ("IF current_setting('edu_ai.src') <> '" + owner + "' AND (EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
            f"WHERE s.nspname = '{schema}' AND c.relkind IN ('r','p','v','m','f','S') AND " + NOT_EXT.format(cls="pg_class", alias="c") + " AND pg_get_userbyid(c.relowner) = current_setting('edu_ai.src')) "
            + ("OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace WHERE s.nspname = '" + schema + "' AND " + NOT_EXT.format(cls="pg_proc", alias="p")
               + " AND pg_get_userbyid(p.proowner) = current_setting('edu_ai.src'))" if with_functions else "")
            + ") THEN RAISE EXCEPTION 'objects of the old owner remain, rolled back'; END IF; ")
    return "DO $own$ DECLARE r record; BEGIN " + rel + fn + ty + done + "END $own$;"


def gen_edu_ai(m: dict) -> str:
    d = m["edu_ai_db"]
    tables = sorted(d["table_dml"])
    runtime = ", ".join(d["S_all_tables_for"])
    others = "edu_ai_search, edu_ai_ingest, edu_ai_migrator, edu_ai_langflow"
    out = ["-- GENERATED from postgres/role_contract/matrix.json; do not edit by hand.",
           "-- In database edu_ai, as a superuser. Variable src = the role that owns the objects now (edu_ai_admin on a clean install, the old bootstrap role on adoption).",
           *PRELUDE, "\\if :{?src}", "\\else", "\\set src edu_ai_admin", "\\endif"]
    out += guard_block("edu_ai", m)
    out.append("BEGIN;")
    out.append("SET LOCAL lock_timeout = '5s';")
    out.append(f"ALTER SCHEMA edu_ai OWNER TO {d['schema_owner']};")
    out.append(ownership_loop("edu_ai", d["object_owner"], True, False))
    out.append("-- PUBLIC: nothing; the application roles start from nothing too, so a re-run converges and no grant option survives")
    out.append("REVOKE ALL ON SCHEMA edu_ai, public, paradedb, pdb FROM PUBLIC;")
    out.append(f"REVOKE ALL ON SCHEMA edu_ai, public, paradedb, pdb FROM {others};")
    out.append("REVOKE ALL ON ALL TABLES IN SCHEMA edu_ai, paradedb, pdb FROM PUBLIC;")
    out.append("REVOKE ALL ON ALL SEQUENCES IN SCHEMA edu_ai FROM PUBLIC;")
    out.append("REVOKE ALL ON ALL ROUTINES IN SCHEMA edu_ai, paradedb, pdb FROM PUBLIC;")
    out.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA edu_ai, paradedb, pdb FROM {others};")
    out.append(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA edu_ai FROM {others};")
    out.append(f"REVOKE ALL ON ALL ROUTINES IN SCHEMA edu_ai, paradedb, pdb FROM {others};")
    out.append("-- schema USAGE")
    for schema, roles in d["schema_usage"].items():
        out.append(f"GRANT USAGE ON SCHEMA {schema} TO {', '.join(roles)};")
    out.append("-- extension objects used at runtime: read/execute only")
    out.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA paradedb, pdb TO {runtime};")
    out.append(f"GRANT EXECUTE ON ALL ROUTINES IN SCHEMA edu_ai, paradedb, pdb TO {runtime}, {d['object_owner']};")
    out.append("-- tables: SELECT for the runtime roles, DML only per the explicit matrix")
    out.append(f"GRANT SELECT ON ALL TABLES IN SCHEMA edu_ai TO {runtime};")
    letter = {"I": "INSERT", "U": "UPDATE", "D": "DELETE"}
    for t in tables:
        for role, privs in sorted(d["table_dml"][t].items()):
            out.append(f"GRANT {', '.join(letter[p] for p in privs)} ON edu_ai.{t} TO {role};")
    out.append("-- sequences: USAGE and SELECT (never UPDATE) for the tables a role may insert into")
    for t in tables:
        for role, privs in sorted(d["table_dml"][t].items()):
            if "I" in privs:
                out.append("DO $s$ DECLARE q text; BEGIN FOR q IN SELECT DISTINCT format('%I.%I', ns.nspname, sq.relname) FROM pg_depend dep "
                           "JOIN pg_class sq ON sq.oid = dep.objid AND sq.relkind = 'S' JOIN pg_namespace ns ON ns.oid = sq.relnamespace "
                           f"WHERE dep.refobjid = 'edu_ai.{t}'::regclass AND dep.deptype IN ('a','i') LOOP EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO {role}', q); END LOOP; END $s$;")
    out.append("-- later objects of the owner role: nothing for PUBLIC and no automatic grant; new tables and functions enter the matrix, then `db_roles.py reapply`")
    o = d["object_owner"]
    for scope in ("", "IN SCHEMA edu_ai ", "IN SCHEMA public ", "IN SCHEMA paradedb ", "IN SCHEMA pdb "):
        base = f"ALTER DEFAULT PRIVILEGES FOR ROLE {o} {scope}"
        if not scope:
            out.append(base + "REVOKE ALL ON TABLES FROM PUBLIC;")
            out.append(base + "REVOKE ALL ON SEQUENCES FROM PUBLIC;")
            out.append(base + "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;")
            out.append(base + "REVOKE USAGE ON TYPES FROM PUBLIC;")
        out.append(base + f"REVOKE ALL ON TABLES FROM {others};")
        out.append(base + f"REVOKE ALL ON SEQUENCES FROM {others};")
        out.append(base + f"REVOKE ALL ON FUNCTIONS FROM {others};")
        out.append(base + f"REVOKE ALL ON TYPES FROM {others};")
    out.append(f"COMMENT ON DATABASE edu_ai IS '{m['marker']}';")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def gen_langflow(m: dict) -> str:
    owner = m["langflow_db"]["owner"]
    out = ["-- GENERATED from postgres/role_contract/matrix.json; do not edit by hand.",
           "-- In database langflow, as a superuser, ADOPTION and RESTORE only: vendor objects move to the langflow role.",
           "-- (A clean install needs nothing here: the database is created with that owner and Langflow creates its tables as that role.)",
           *PRELUDE, "\\if :{?src}", "\\else", "\\set src edu_ai_admin", "\\endif"]
    out += guard_block("langflow", m)
    out.append("BEGIN;")
    out.append("SET LOCAL lock_timeout = '5s';")
    out.append(ownership_loop("public", owner, False, True))
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def generate(m: dict) -> dict[str, str]:
    return {"_role_contract_roles.psql": gen_roles(m), "_role_contract_cluster.psql": gen_cluster(m),
            "_role_contract_edu_ai.psql": gen_edu_ai(m), "_role_contract_langflow.psql": gen_langflow(m)}


# ── docker exec psql ───────────────────────────────────────────────────────────
def psql(container: str, db: str, sql: str, user: str, variables: dict | None = None, tuples: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec", "-i", container, "psql", "-U", user, "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    for k, v in (variables or {}).items():
        cmd += ["-v", f"{k}={v}"]
    if tuples:
        cmd += ["-At", "-F", "|"]
    # catalog functions only: nothing a database owner can create in `public` may capture a call made as superuser
    return subprocess.run(cmd, input="SET search_path = pg_catalog, pg_temp;\n" + sql, capture_output=True, text=True)


def rows(container: str, db: str, sql: str, user: str) -> list[list[str]]:
    r = psql(container, db, sql, user)
    if r.returncode != 0:
        tail = [l for l in (r.stderr or "").splitlines() if l.strip()][-1:] or ["query failed"]
        raise ContractError(tail[0][:200])
    return [line.split("|") for line in r.stdout.splitlines() if line.strip()]


def bootstrap_user(container: str) -> str:
    r = subprocess.run(["docker", "exec", container, "printenv", "POSTGRES_USER"], capture_output=True, text=True)
    return r.stdout.strip() or "edu_ai_admin"


def admin_user(container: str, given: str | None) -> str:
    if given:
        return given
    ok = psql(container, "postgres", "select 1;", "edu_ai_admin")
    return "edu_ai_admin" if ok.returncode == 0 else bootstrap_user(container)


def run_file(container: str, db: str, user: str, text: str, src: str) -> None:
    r = psql(container, db, text, user, {"src": src}, tuples=False)
    if r.returncode != 0:
        tail = [l for l in (r.stderr or "").splitlines() if l.strip()][-1:] or ["psql failed"]
        raise ContractError(f"{db}: {tail[0][:240]}")


def secrets_from(env: dict, m: dict) -> dict[str, str]:
    out = {}
    for role, key in m["login_secrets"].items():
        pw = env.get(key, "")
        if not PASSWORD.fullmatch(pw) or pw.startswith("CHANGE_ME"):
            raise ContractError(f"{key} is missing or not a generated secret; nothing was written")
        out[role] = pw
    if len(set(out.values())) != len(out):
        raise ContractError("two roles share a password; nothing was written")
    return out


def set_passwords(container: str, user: str, pws: dict[str, str]) -> None:
    # the statements carry clear text: keep them out of the server log, also when one fails
    stmts = ["SET log_statement = 'none';", "SET log_min_error_statement = 'panic';"] + [f"ALTER ROLE {role} PASSWORD '{pw}';" for role, pw in pws.items()]
    r = psql(container, "postgres", "\n".join(stmts) + "\n", user, tuples=False)
    if r.returncode != 0:
        raise ContractError("setting passwords failed")


# ── verification of the live catalog against the matrix ────────────────────────
def verify(container: str, user: str, m: dict, old_role: str = "") -> list[str]:
    """Catalog vs matrix. One batched query per concern (never one docker exec per check)."""
    bad: list[str] = []
    q = lambda db, sql: rows(container, db, sql, user)  # noqa: E731
    roles = contract_roles(m)
    non_super = [r for r in roles if not m["roles"][r]["superuser"]]
    in_nonsuper = ", ".join("'" + r + "'" for r in non_super)
    in_roles = ", ".join("'" + r + "'" for r in roles)
    e = m["edu_ai_db"]
    owner = e["object_owner"]
    app_roles = [r for r in non_super if r != owner]
    in_app = ", ".join("'" + r + "'" for r in app_roles)
    nonext_c = NOT_EXT.format(cls="pg_class", alias="c")
    nonext_p = NOT_EXT.format(cls="pg_proc", alias="p")

    attrs = {r[0]: r[1:] for r in q("postgres", "select rolname, rolsuper, rolcanlogin, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit from pg_roles where rolname !~ '^pg_'")}
    for role, a in m["roles"].items():
        want = ["t" if a[k] else "f" for k in ("superuser", "login", "createdb", "createrole", "replication", "bypassrls", "inherit")]
        if role not in attrs:
            bad.append(f"role {role} missing")
        elif role == "edu_ai_admin":
            if attrs[role][0] != "t":
                bad.append("role edu_ai_admin is not a superuser")
        elif attrs[role] != want:
            bad.append(f"role {role} attributes differ")
    legacy = set(m.get("legacy_roles", [])) | ({old_role} - set(m["roles"]) - {""})
    for x in sorted(set(attrs) - set(m["roles"])):
        if x not in legacy:
            bad.append(f"role {x} is not in the contract")
        elif attrs[x][1] == "t":
            bad.append(f"pending: legacy role {x} can still log in (demote-old)")
    pw = {r[0]: r[1] for r in q("postgres", "select rolname, case when rolpassword is null then 'none' when rolpassword like 'SCRAM-SHA-256$%' then 'scram' else 'other' end from pg_authid where rolname !~ '^pg_'")}
    for role, a in m["roles"].items():
        if a["login"] and pw.get(role) != "scram":
            bad.append(f"role {role} has no SCRAM password")
    for x in sorted(legacy & set(pw)):
        if attrs[x][1] != "t" and pw[x] != "none":
            bad.append(f"legacy role {x} is NOLOGIN but still has a password")
    for r in q("postgres", f"select rolname from pg_roles where rolname in ({in_roles}) and rolconnlimit <> -1"):
        bad.append(f"role {r[0]} has a connection limit")
    stored = {}
    for role_name, cfg in q("postgres", f"select r.rolname, array_to_string(s.setconfig, ';') from pg_db_role_setting s join pg_roles r on r.oid = s.setrole where r.rolname in ({in_roles}) and s.setdatabase = 0"):
        stored[role_name] = cfg
    for role, a in m["roles"].items():
        want_cfg = "search_path=" + ", ".join(a["search_path"]) if a.get("search_path") else None
        if stored.get(role) != want_cfg:
            bad.append(f"role {role} stored settings differ from the matrix")
    for r in q("postgres", f"select r.rolname from pg_db_role_setting s join pg_roles r on r.oid = s.setrole where r.rolname in ({in_roles}) and s.setdatabase <> 0"):
        bad.append(f"role {r[0]} has per-database settings")
    mem = {tuple(r) for r in q("postgres", "select ro.rolname, u.rolname, a.inherit_option, a.set_option from pg_auth_members a join pg_roles ro on ro.oid=a.roleid join pg_roles u on u.oid=a.member "
                                           f"where u.rolname in ({in_roles}) or ro.rolname in ({in_roles})")}
    want_mem = {(x["role"], x["member"], "t" if x["inherit"] else "f", "t" if x["set"] else "f") for x in m["memberships"]}
    if mem != want_mem:
        bad.append(f"memberships differ: {sorted(mem ^ want_mem)}")

    dbs = m["databases"]
    in_dbs = ", ".join("'" + d + "'" for d in list(dbs) + list(m["public_revoked_databases"]))
    owners = {r[0]: r[1] for r in q("postgres", f"select datname, pg_get_userbyid(datdba) from pg_database where datname in ({in_dbs})")}
    for db, d in dbs.items():
        if owners.get(db) != d["owner"]:
            bad.append(f"database {db} owner is {owners.get(db)}")
    for r in q("postgres", f"select d.datname from pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a where d.datname in ({in_dbs}) and a.grantee = 0"):
        bad.append(f"database {r[0]} still grants to PUBLIC")
    for r in q("postgres", f"select d.datname || ' ' || a.grantee::regrole::text from pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a where d.datname in ({in_dbs}) and a.is_grantable and a.grantee <> d.datdba"):
        bad.append(f"grant option on database {r[0]}")
    for role, db, c, cr, t in q("postgres", "select r.rolname, d.datname, has_database_privilege(r.rolname, d.datname, 'CONNECT'), has_database_privilege(r.rolname, d.datname, 'CREATE'), "
                                f"has_database_privilege(r.rolname, d.datname, 'TEMPORARY') from pg_roles r cross join pg_database d where r.rolname in ({in_nonsuper}) and d.datname in ({in_dbs})"):
        d = dbs.get(db)
        if d is None:
            if (c, t) != ("f", "f"):
                bad.append(f"{role} can connect to {db}")
            continue
        if (c == "t") != (role in d["connect"] or role == d["owner"]):
            bad.append(f"connect {role} -> {db} is {c == 't'}")
        if cr == "t" and role != d["owner"]:
            bad.append(f"{role} can CREATE in {db}")
        if t == "t" and role != d["owner"]:
            bad.append(f"{role} has TEMPORARY in {db}")

    for r in q("edu_ai", "select 'schema ' || nspname || ' owner ' || pg_get_userbyid(nspowner) from pg_namespace "
                         f"where nspname = 'edu_ai' and pg_get_userbyid(nspowner) <> '{e['schema_owner']}'"):
        bad.append(r[0])
    for r in q("edu_ai", f"select c.relname || ' (' || pg_get_userbyid(c.relowner) || ')' from pg_class c join pg_namespace s on s.oid = c.relnamespace "
                         f"where s.nspname = 'edu_ai' and c.relkind in ('r','p','v','S','m','f') and {nonext_c} and pg_get_userbyid(c.relowner) <> '{owner}'"):
        bad.append(f"edu_ai object with another owner: {r[0]}")
    for r in q("edu_ai", "select p.proname || ' (' || pg_get_userbyid(p.proowner) || ')' from pg_proc p join pg_namespace s on s.oid = p.pronamespace "
                         f"where s.nspname = 'edu_ai' and {nonext_p} and pg_get_userbyid(p.proowner) <> '{owner}'"):
        bad.append(f"edu_ai function with another owner: {r[0]}")
    for r in q("edu_ai", "select s.nspname || '.' || c.relname from pg_class c join pg_namespace s on s.oid = c.relnamespace, aclexplode(c.relacl) a where s.nspname = 'edu_ai' and a.grantee = 0"):
        bad.append(f"PUBLIC holds privileges on {r[0]}")
    for r in q("edu_ai", "select s.nspname || '.' || p.proname from pg_proc p join pg_namespace s on s.oid = p.pronamespace, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
                         "where s.nspname in ('edu_ai','paradedb','pdb') and a.grantee = 0"):
        bad.append(f"PUBLIC may execute {r[0]}")
    for r in q("edu_ai", "select n.nspname from pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
                         "where n.nspname in ('public', 'paradedb', 'pdb', 'edu_ai') and a.grantee = 0 and a.privilege_type = 'CREATE'"):
        bad.append(f"PUBLIC may CREATE in schema {r[0]}")
    for r in q("edu_ai", "select n.nspname from pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a where n.nspname in ('public', 'paradedb', 'pdb', 'edu_ai') and a.grantee = 0"):
        bad.append(f"PUBLIC holds a privilege on schema {r[0]}")
    for r in q("edu_ai", "select n.nspname || ' ' || a.grantee::regrole::text from pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
                         "where n.nspname in ('public', 'paradedb', 'pdb', 'edu_ai') and a.is_grantable and a.grantee <> n.nspowner"):
        bad.append(f"grant option on schema {r[0]}")
    for r in q("edu_ai", "select p.proname from pg_proc p join pg_namespace s on s.oid = p.pronamespace, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
                         "where s.nspname in ('edu_ai','paradedb','pdb') and a.is_grantable and a.grantee <> p.proowner"):
        bad.append(f"grant option on function {r[0]}")
    allowed_definer = set(e.get("definer_functions", []))
    for r in q("edu_ai", f"select p.proname from pg_proc p join pg_namespace s on s.oid = p.pronamespace where s.nspname = 'edu_ai' and p.prosecdef and {nonext_p}"):
        if r[0] not in allowed_definer:
            bad.append(f"SECURITY DEFINER function {r[0]} is not in the matrix")
    for r in q("edu_ai", f"select c.relname from pg_class c join pg_namespace s on s.oid = c.relnamespace join pg_attribute a on a.attrelid = c.oid where s.nspname = 'edu_ai' and a.attacl is not null and {nonext_c}"):
        bad.append(f"column privileges on edu_ai.{r[0]}")
    for r in q("edu_ai", f"select pg_get_userbyid(d.defaclrole) || ' ' || d.defaclobjtype::text from pg_default_acl d, aclexplode(d.defaclacl) a where a.grantee not in (d.defaclrole, 0)"):
        bad.append(f"default privileges grant to a role: {r[0]}")
    for r in q("edu_ai", "select 'comment ' || coalesce(shobj_description(oid, 'pg_database'), '<none>') from pg_database where datname = 'edu_ai' "
                         f"and coalesce(shobj_description(oid, 'pg_database'), '') <> '{m['marker']}'"):
        bad.append(f"role contract marker missing ({r[0]})")

    for r in q("edu_ai", "select s.nspname || '.' || c.relname from pg_class c join pg_namespace s on s.oid = c.relnamespace where s.nspname in ('public','paradedb','pdb') and c.relkind in ('r','p','v','m','f','S') "
                         f"and {nonext_c}"):
        bad.append(f"unexpected object {r[0]}")
    for r in q("edu_ai", "select s.nspname || '.' || p.proname from pg_proc p join pg_namespace s on s.oid = p.pronamespace where s.nspname in ('public','paradedb','pdb') "
                         f"and {nonext_p}"):
        bad.append(f"unexpected function {r[0]}")
    for r in q("edu_ai", "select t.typname from pg_type t join pg_namespace s on s.oid = t.typnamespace where s.nspname in ('edu_ai','public','paradedb','pdb') and t.typtype in ('e','d','r','m') "
                         "and not exists (select 1 from pg_depend d where d.classid = 'pg_type'::regclass and d.objid = t.oid and d.deptype = 'e')"):
        bad.append(f"unexpected type {r[0]}")

    inv = q("edu_ai", f"select c.relname, c.relkind from pg_class c join pg_namespace s on s.oid = c.relnamespace where s.nspname = 'edu_ai' and c.relkind in ('r','p','v','S') and {nonext_c}")
    tables = {r[0] for r in inv if r[1] in ("r", "p")}
    views = {r[0] for r in inv if r[1] == "v"}
    seqs = {r[0] for r in inv if r[1] == "S"}
    if tables != set(e["table_dml"]):
        bad.append(f"table inventory differs from the matrix: {sorted(tables ^ set(e['table_dml']))}")
    if views != set(e["views"]):
        bad.append("view inventory differs from the matrix")
    if seqs != set(m["baseline"]["edu_ai"]["sequences"]):
        bad.append(f"sequence inventory differs from the baseline: {sorted(seqs ^ set(m['baseline']['edu_ai']['sequences']))}")

    letter = {"I": "INSERT", "U": "UPDATE", "D": "DELETE"}
    want_priv = set()
    for t in tables | views:
        for role in e["S_all_tables_for"]:
            want_priv.add((role, t, "SELECT"))
        for role, privs in e["table_dml"].get(t, {}).items():
            for pr in privs:
                want_priv.add((role, t, letter[pr]))
    have_priv = {tuple(r) for r in q("edu_ai",
        "select r.rolname, c.relname, p.priv from pg_roles r cross join pg_class c join pg_namespace s on s.oid = c.relnamespace "
        "cross join (values ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER')) p(priv) "
        f"where r.rolname in ({in_app}) and s.nspname = 'edu_ai' and c.relkind in ('r','p','v') and {nonext_c} "
        "and has_table_privilege(r.rolname, c.oid, p.priv)")}
    for x in sorted(have_priv - want_priv):
        bad.append(f"extra privilege {x[2]} on edu_ai.{x[1]} for {x[0]}")
    for x in sorted(want_priv - have_priv):
        bad.append(f"missing privilege {x[2]} on edu_ai.{x[1]} for {x[0]}")

    owned = {}
    for tbl, sq in q("edu_ai", "select t.relname, sq.relname from pg_depend d join pg_class sq on sq.oid = d.objid and sq.relkind = 'S' join pg_class t on t.oid = d.refobjid "
                               "join pg_namespace s on s.oid = t.relnamespace where d.classid = 'pg_class'::regclass and d.refclassid = 'pg_class'::regclass and d.deptype in ('a','i') and s.nspname = 'edu_ai'"):
        owned.setdefault(tbl, set()).add(sq)
    want_seq = set()
    for t in tables:
        for role, privs in e["table_dml"].get(t, {}).items():
            if "I" in privs:
                for sq in owned.get(t, ()):
                    want_seq |= {(role, sq, "USAGE"), (role, sq, "SELECT")}
    have_seq = {tuple(r) for r in q("edu_ai", "select r.rolname, c.relname, p.priv from pg_roles r cross join pg_class c join pg_namespace s on s.oid = c.relnamespace cross join (values ('USAGE'),('SELECT'),('UPDATE')) p(priv) "
                                              f"where r.rolname in ({in_app}) and s.nspname = 'edu_ai' and c.relkind = 'S' and has_sequence_privilege(r.rolname, c.oid, p.priv)")}
    for x in sorted(have_seq - want_seq):
        bad.append(f"extra sequence privilege {x[2]} on {x[1]} for {x[0]}")
    for x in sorted(want_seq - have_seq):
        bad.append(f"missing sequence privilege {x[2]} on {x[1]} for {x[0]}")

    fn_all = {r[0] for r in q("edu_ai", "select s.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' from pg_proc p join pg_namespace s on s.oid = p.pronamespace where s.nspname in ('edu_ai','paradedb','pdb')")}
    fn_have = {(r[0], r[1]) for r in q("edu_ai", "select r.rolname, s.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' from pg_roles r cross join pg_proc p join pg_namespace s on s.oid = p.pronamespace "
                                                f"where r.rolname in ({in_app}) and s.nspname in ('edu_ai','paradedb','pdb') and has_function_privilege(r.rolname, p.oid, 'EXECUTE')")}
    fn_want = {(role, f) for role in e["S_all_tables_for"] for f in fn_all}
    for x in sorted(fn_have - fn_want)[:10]:
        bad.append(f"extra EXECUTE for {x[0]} on {x[1]}")
    for x in sorted(fn_want - fn_have)[:10]:
        bad.append(f"missing EXECUTE for {x[0]} on {x[1]}")
    names = {r[0] + ":" + r[1] for r in q("edu_ai", f"select p.proname, p.prokind from pg_proc p join pg_namespace s on s.oid = p.pronamespace where s.nspname = 'edu_ai' and {nonext_p}")}
    if names != {f + ":f" for f in m["baseline"]["edu_ai"]["functions"]}:
        bad.append("function inventory differs from the baseline")

    for r in q("edu_ai", "select r.rolname, s.nspname from pg_roles r cross join pg_namespace s "
                         f"where r.rolname in ({in_app}) and s.nspname in ('edu_ai','public','paradedb','pdb') and has_schema_privilege(r.rolname, s.oid, 'CREATE')"):
        bad.append(f"{r[0]} can CREATE in schema {r[1]}")
    want_usage = {(role, s) for s, rs in e["schema_usage"].items() for role in rs}
    have_usage = {tuple(r) for r in q("edu_ai", f"select r.rolname, s.nspname from pg_roles r cross join pg_namespace s where r.rolname in ({in_nonsuper}) and s.nspname in ('edu_ai','public','paradedb','pdb') and has_schema_privilege(r.rolname, s.oid, 'USAGE')")}
    for x in sorted(have_usage ^ want_usage):
        bad.append(f"schema USAGE differs for {x[0]} on {x[1]}")
    for r in q("edu_ai", "select distinct r.rolname from pg_class c join pg_namespace s on s.oid = c.relnamespace, aclexplode(c.relacl) a join pg_roles r on r.oid = a.grantee "
                         f"where s.nspname = 'edu_ai' and a.is_grantable and r.rolname in ({in_nonsuper})"):
        bad.append(f"{r[0]} holds grant options")

    lf = m["langflow_db"]["owner"]
    lfb = m["baseline"]["langflow"]
    for r in q("langflow", "select c.relname || ' (' || pg_get_userbyid(c.relowner) || ')' from pg_class c join pg_namespace s on s.oid = c.relnamespace "
                           f"where s.nspname = 'public' and c.relkind in ('r','p','v','m','f','S') and pg_get_userbyid(c.relowner) <> '{lf}'"):
        bad.append(f"langflow object with another owner: {r[0]}")
    for r in q("langflow", "select t.typname from pg_type t join pg_namespace s on s.oid = t.typnamespace "
                           f"where s.nspname = 'public' and t.typtype = 'e' and pg_get_userbyid(t.typowner) <> '{lf}'"):
        bad.append(f"langflow type with another owner: {r[0]}")
    for r in q("langflow", "select p.proname from pg_proc p join pg_namespace s on s.oid = p.pronamespace where s.nspname = 'public'"):
        bad.append(f"unexpected function {r[0]} in langflow")
    lf_tabs = {r[0] for r in q("langflow", "select c.relname from pg_class c join pg_namespace s on s.oid = c.relnamespace where s.nspname = 'public' and c.relkind in ('r','p')")}
    if lf_tabs and not set(lfb["tables"]) <= lf_tabs:
        bad.append(f"langflow tables missing: {sorted(set(lfb['tables']) - lf_tabs)}")
    return bad


# ── commands ───────────────────────────────────────────────────────────────────
def need_confirm(args) -> bool:
    if not getattr(args, "confirm_isolated", False):
        print("REFUSED: this writes roles, ownership and grants; it is approved for isolated synthetic installations only; pass --confirm-isolated")
        return False
    return True


def cmd_gen_sql(args) -> int:
    m = load_matrix()
    out = pathlib.Path(args.out or INIT_DIR)
    for name, text in generate(m).items():
        (out / name).write_bytes(text.encode("utf-8"))
    print("GENERATED:", ", ".join(FILES), "->", out)
    return 0


def cmd_prepare_env(args) -> int:
    """For an installation that predates the contract: the four new secrets and the ingest user, names only on output."""
    import secrets
    from envfile import is_placeholder, read_env, set_env
    m = load_matrix()
    path = pathlib.Path(args.env)
    env = read_env(path)
    if not env:
        print("REFUSED: the .env is missing or empty")
        return 1
    updates = {}
    for key in m["login_secrets"].values():
        if is_placeholder(env.get(key)):
            updates[key] = secrets.token_urlsafe(24)
    if env.get("POSTGRES_DB_USER") != "edu_ai_ingest":
        updates["POSTGRES_DB_USER"] = "edu_ai_ingest"
    if updates:
        set_env(path, updates)
    print("PREPARED:", ", ".join(sorted(updates)) or "nothing to change")
    return 0


def cmd_check(args) -> int:
    m = load_matrix()
    user = args.admin_user or admin_user(args.container, None)
    src = getattr(args, "old_role", None) or user
    for kind, db in (("edu_ai", "edu_ai"), ("langflow", "langflow")):
        text = "\\set ON_ERROR_STOP on\n" + "\n".join(guard_block(kind, m)) + "\n"
        r = psql(args.container, db, text, user, {"src": src}, tuples=False)
        print(f"BASELINE_{kind.upper()}: " + ("OK" if r.returncode == 0 else "MISMATCH " + ([l for l in r.stderr.splitlines() if "baseline mismatch" in l] or ["?"])[0][:200]))
        if r.returncode != 0:
            return 1
    return 0


def cmd_adopt(args) -> int:
    from envfile import read_env
    if not need_confirm(args):
        return 1
    m = load_matrix()
    old = args.old_role or m["legacy_roles"][0]
    env = read_env(pathlib.Path(args.env))
    files = generate(m)
    try:
        if old not in m["legacy_roles"]:
            raise ContractError(f"{old} is not a legacy role of the contract; refused")
        if env.get("POSTGRES_DB_USER") != "edu_ai_ingest":
            raise ContractError("POSTGRES_DB_USER in the .env is not edu_ai_ingest: the flows would keep connecting as the legacy superuser; refused before any write")
        pws = secrets_from(env, m)
        if cmd_check(argparse.Namespace(container=args.container, admin_user=old, old_role=old)) != 0:
            raise ContractError("baseline mismatch: refused before any write")
        run_file(args.container, "postgres", old, files["_role_contract_roles.psql"], old)
        set_passwords(args.container, old, pws)
        run_file(args.container, "postgres", old, files["_role_contract_cluster.psql"], old)
        run_file(args.container, "edu_ai", old, files["_role_contract_edu_ai.psql"], old)
        run_file(args.container, "langflow", old, files["_role_contract_langflow.psql"], old)
    except ContractError as exc:
        print("REFUSED:" if "refused" in str(exc) or "nothing was written" in str(exc) else "FAILED:", exc)
        return 1
    bad = verify(args.container, old, m, old)
    hard = [b for b in bad if not b.startswith("pending: ")]
    print("ADOPTED" if not hard else "ADOPTED_WITH_DEVIATIONS")
    for b in bad:
        print(("PENDING:" if b.startswith("pending: ") else "DEVIATION:"), b.replace("pending: ", ""))
    return 0 if not hard else 1


def cmd_reapply(args) -> int:
    if not need_confirm(args):
        return 1
    m = load_matrix()
    user = admin_user(args.container, args.admin_user)
    files = generate(m)
    try:
        run_file(args.container, "postgres", user, files["_role_contract_roles.psql"], user)
        run_file(args.container, "postgres", user, files["_role_contract_cluster.psql"], user)
        run_file(args.container, "edu_ai", user, files["_role_contract_edu_ai.psql"], user)
        run_file(args.container, "langflow", user, files["_role_contract_langflow.psql"], user)
    except ContractError as exc:
        print("FAILED:", exc)
        return 1
    bad = verify(args.container, user, m)
    hard = [b for b in bad if not b.startswith("pending: ")]
    print("REAPPLIED" if not hard else "REAPPLIED_WITH_DEVIATIONS")
    for b in bad:
        print(("PENDING:" if b.startswith("pending: ") else "DEVIATION:"), b.replace("pending: ", ""))
    return 0 if not hard else 1


def cmd_verify(args) -> int:
    m = load_matrix()
    user = admin_user(args.container, args.admin_user)
    try:
        bad = verify(args.container, user, m, args.old_role or "")
    except ContractError as exc:
        print("UNVERIFIED:", exc)
        return 3
    if args.json:
        print(json.dumps({"deviations": bad}))
    else:
        print("DB_CONTRACT:", "MATCH" if not bad else f"{len(bad)} deviation(s)")
        for b in bad:
            print("DEVIATION:", b.replace("pending: ", "PENDING "))
    return 0 if not bad else 1


def cmd_demote(args) -> int:
    if not need_confirm(args):
        return 1
    m = load_matrix()
    user = admin_user(args.container, args.admin_user)
    if args.old_role not in m["legacy_roles"] or user == args.old_role:
        print("REFUSED: not a legacy role of the contract, or it is the connecting role")
        return 1
    try:
        bad = [b for b in verify(args.container, user, m, args.old_role) if not b.startswith("pending: ")]
        live = rows(args.container, "postgres", f"select count(*) from pg_stat_activity where usename = '{args.old_role}' and backend_type = 'client backend' and pid <> pg_backend_pid()", user)
    except ContractError as exc:
        print("REFUSED: cannot verify first:", exc)
        return 1
    if bad:
        print("REFUSED: the contract does not match yet:", "; ".join(bad[:3]))
        return 1
    if live != [["0"]]:
        print(f"REFUSED: {live[0][0]} live session(s) of {args.old_role}; recreate the application containers first")
        return 1
    r = psql(args.container, "postgres", f"ALTER ROLE {args.old_role} NOLOGIN PASSWORD NULL;\n", user, tuples=False)
    print("DEMOTED" if r.returncode == 0 else "FAILED", args.old_role, "(NOLOGIN, no password; a bootstrap superuser cannot lose SUPERUSER)")
    return 0 if r.returncode == 0 else 1


def cmd_globals(args) -> int:
    user = admin_user(args.container, args.admin_user)
    r = subprocess.run(["docker", "exec", args.container, "pg_dumpall", "-U", user, "--globals-only"], capture_output=True)
    if r.returncode != 0:
        print("FAILED: pg_dumpall")
        return 1
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(r.stdout)
    os.chmod(args.out, 0o600)
    print("GLOBALS_WRITTEN:", args.out, f"({len(r.stdout)} bytes; private: contains password hashes)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen-sql")
    g.add_argument("--out")
    pe = sub.add_parser("prepare-env")
    pe.add_argument("--env", required=True)
    for name in ("check", "adopt", "reapply", "verify", "demote-old", "globals"):
        s = sub.add_parser(name)
        s.add_argument("--container", required=True)
        s.add_argument("--admin-user")
        if name in ("check", "adopt", "verify", "demote-old"):
            s.add_argument("--old-role")
        if name in ("adopt", "reapply", "demote-old"):
            s.add_argument("--confirm-isolated", action="store_true")
        if name == "adopt":
            s.add_argument("--env", required=True)
        if name == "verify":
            s.add_argument("--json", action="store_true")
        if name == "globals":
            s.add_argument("--out", required=True)
    args = p.parse_args(argv)
    return {"gen-sql": cmd_gen_sql, "prepare-env": cmd_prepare_env, "check": cmd_check, "adopt": cmd_adopt, "reapply": cmd_reapply, "verify": cmd_verify,
            "demote-old": cmd_demote, "globals": cmd_globals}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
