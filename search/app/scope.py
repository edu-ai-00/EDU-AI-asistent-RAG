# last change in code by Jan Drozd (2026-08-03).
"""One enforcement point for entity, organization and private visibility.

The server resolves entity_scope to the caller's own entity only. Organization
visibility uses the full tenant tree; private visibility requires the verified
owner within that tree. Neither entity nor private content grants parent,
child or sibling access merely through hierarchy. Unknown entities fail closed.

The old entity_visible_to_parent flag is not read by search. Current production
and this source use the strict model documented in architecture decision D-19.
"""

from __future__ import annotations


def not_excluded(alias_m: str, schema: str = "edu_ai") -> str:
    """AND-fragment dropping documents whose TYPE never answers.

    taxonomy_rules.search_role = 'excluded' (migration 172) is a curatorial
    statement about a whole document type: a rozcestník carries no fact of its own,
    only links to where the facts are. Such a document must not enter retrieval AT
    ALL — filtering it later in `answer` worked, but the retrievers had already
    spent their candidate window on it and the corpus df was computed with it in.
    Takes no parameters, so it composes into any of the retrieval SQL strings; the
    subquery is an InitPlan, evaluated once per statement.
    """
    return (f" AND NOT EXISTS (SELECT 1 FROM {schema}.taxonomy_rules tr_x"
            f"                  WHERE tr_x.taxonomy_type_code = 'DOCUMENT_TYPE'"
            f"                    AND tr_x.is_active AND tr_x.search_role = 'excluded'"
            f"                    AND tr_x.type_code = COALESCE({alias_m}.document_type_user,"
            f"                                                  {alias_m}.document_type_llm))")


def domain_where(alias: str, column: str,
                 domain_ids: list[int] | None) -> tuple[str, list]:
    """AND-fragment restricting a scanned row to the caller's chosen AREAS.

    The thematic filter, kept here for the same reason `where()` is: eleven SQL
    strings across seven nodes have to apply it identically, and the one that
    forgets is not a worse ranking but an answer from an area the user excluded.
    It is a SEPARATE function from `where()` on purpose — that one is identity
    and permission, this one is a user preference that grants nothing. Both are
    ANDed; neither can widen the other.

    THREE STATES, and the difference between the last two is the whole point:

        None          no thematic restriction — the fragment is EMPTY, so the
                      query is byte-for-byte the one that ran before this
                      feature existed
        [ids…]        the row must carry at least one of them (array overlap;
                      the GIN indexes on domain_ids / document_domain_id are
                      what makes this cheap)
        []            the codes were valid and resolved to NO active id in this
                      caller's reach → `AND false`. NEVER an omitted filter:
                      `ids or None` would turn "you may see nothing in this
                      area" into "you may see everything", which is the exact
                      shape of the private_public_flag failure this module
                      exists to prevent.

    An explicit filter also excludes NULL and empty labels — array overlap is
    false for both — and that is intended: an untagged source is not "in every
    area", it is a source nobody has classified yet (spec §4).

    `alias`/`column` are INTERNAL CONSTANTS chosen by the calling node, never
    values from a request: the QA tables call the column domain_ids and
    document_chunks calls it document_domain_id, so one name could not serve
    both, and interpolating a caller-supplied one would be an injection.
    """
    if domain_ids is None:
        return "", []
    if not domain_ids:
        return " AND false", []
    return f" AND {alias}.{column} && %s::bigint[]", [list(domain_ids)]


def where(alias: str, entity_scope: list[str], org_entities: list[str],
          user_id: str | None, audience_role: str | None = None,
          validity: bool = False) -> tuple[str, list]:
    """SQL fragment (leading AND) + parameters, for a table aliased `alias`.

    `alias` must carry legal_entity_code, visibility and owner_user_id — all three
    are denormalised onto document_chunks and the qa_* tables precisely so this can
    be applied during the scan. rag_vector orders by `embedding <=> query` BEFORE
    any join to metadata, so a predicate that lived only on the metadata row would
    be applied after the whole table had been ranked.

    `audience_role` (init 127): the caller's role (STUDENT|TEACHER|PARENT). When
    given, a HARD filter — a row is visible only if its `audience_roles` array
    includes that role. It is ANDed onto the ENTITY and ORGANIZATION groups ONLY.
    A PRIVATE row is EXEMPT: a private document belongs to its owner and the role
    is irrelevant — only the searching user (its owner) decides visibility.
    Without this exemption a teacher who tagged a private note "for students"
    could never retrieve their own document (owner is not a student). Pass the
    role ONLY for tables that carry the column (document_chunks); the qa_* nodes
    leave it None, so their SQL is unchanged.
    """
    parts: list[str] = []
    params: list = []

    # role clause, ANDed inside each NON-private group (STRICT — no legacy
    # fallback, decided 2026-08-05: an untagged row is a defect, not "for
    # everyone"). Private is deliberately outside it.
    role_sql = f" AND {alias}.audience_roles && %s::text[]" if audience_role else ""

    def _add_role_param():
        if audience_role:
            params.append([audience_role])

    if entity_scope:
        parts.append(f"({alias}.legal_entity_code = ANY(%s) "
                     f"AND {alias}.visibility = 'entity'{role_sql})")
        params.append(entity_scope)
        _add_role_param()

    if org_entities:
        parts.append(f"({alias}.legal_entity_code = ANY(%s) "
                     f"AND {alias}.visibility = 'organization'{role_sql})")
        params.append(org_entities)
        _add_role_param()
        if user_id:
            # own private, wherever in the organisation it was ingested — a person
            # who uploads into a unit still owns it. Bounded by the organisation so
            # a stolen user id cannot reach another tenant's rows. NO role filter:
            # for a private document only the owner matters.
            parts.append(f"({alias}.legal_entity_code = ANY(%s) "
                         f"AND {alias}.visibility = 'private' "
                         f"AND {alias}.owner_user_id = %s)")
            params += [org_entities, user_id]

    # nothing resolved at all → no rows, rather than no predicate
    if not parts:
        return " AND false", []

    cond = " AND (" + " OR ".join(parts) + ")"
    # document VALIDITY as a HARD filter against the server date (init: valid_from/
    # valid_to on document_chunks; NULL = unbounded on that side). Opt-in via
    # `validity` because the qa_* tables carrying no such columns also call where().
    if validity:
        cond += (f" AND ({alias}.valid_from IS NULL OR {alias}.valid_from <= CURRENT_DATE)"
                 f" AND ({alias}.valid_to IS NULL OR {alias}.valid_to >= CURRENT_DATE)")
    return cond, params


def describe(entity_scope: list[str], org_entities: list[str],
             user_id: str | None) -> str:
    """One line for the trace, so a short result set can be explained by what the
    caller was allowed to see rather than by what was indexed."""
    if not entity_scope and not org_entities:
        return "no entity resolved — nothing visible"
    return (f"entity={','.join(entity_scope)}"
            + (f" +own private of {user_id}" if user_id else "")
            + (f" +organization({len(org_entities)} entities)" if org_entities else ""))
