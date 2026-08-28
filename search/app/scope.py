# last change in code by Martin Safranek (2026-08-08).
"""Who may see which rows — one predicate, built in one place.

Every retrieval step has to apply the same rule, and there are four of them
(`document_lexical`, `rag_keywords`, `rag_vector`, `document_siblings`) plus the
three QA steps. A rule that has to be repeated seven times is a rule that will be
forgotten once: `private_public_flag` sat on `document_metadata` from the
beginning and retrieval read it in exactly ZERO places for its whole life, so a
document marked private was returned to anyone. That is the failure this module
exists to make impossible — a new step cannot query the chunk table without going
through `where()`.

THE RULE — three levels, each said explicitly at ingest, none inherited implicitly

    private        owner_user_id = the caller, inside my organisation
    entity         legal_entity_code IN (my whole vertical lineage: me, everything
                   under me, and my ancestors/organisation above me)
    organization   legal_entity_code IN (my organisation: root + all of it)

`entity` runs along the caller's VERTICAL lineage, both ways (2026-08-07):
  * DOWNWARD — an org-unit's `entity` document is visible to its SUBORDINATE
    entities. A whole-school řád / pomůcky ingested under the organisation must
    reach the ZŠ and SŠ units below it (ancestor_entities in server.py adds the
    caller's ancestors + self to the scope).
  * UPWARD — a unit's own document is read by the legal person above it too;
    this half is gated by the `entity_visible_to_parent` switch (subtree_entities),
    which when off makes the upward step stop at the publisher.
A sibling never sees a sibling (neither is an ancestor nor a descendant of the
other). Cross-tenant is impossible — the lineage is resolved within one legal
person, and organization_entities never crosses to another.

Nothing crosses a legal person. Material that several organisations need — the
CERMAT and MŠMT documents — is ingested per organisation, which costs a copy each
and buys an isolation boundary with no exception carved through it.

Three things it deliberately does NOT do:

  * It does not widen `entity` SIDEWAYS. A document of the primary school is not
    visible to the secondary school beside it — only up and down the lineage, never
    across to a sibling. Whoever wants unrelated units to see it says `organization`.
  * It does not share `private`. A private document belongs to the person who
    ingested it, in their entity, and to nobody else — not to the organisation and
    not to a unit below.
  * It does not default. An unresolvable entity yields an EMPTY organisation list
    and no entity of its own, and that means "see nothing" — never "see
    everything". A permissive default here is a silent cross-tenant leak, and with
    `public` gone there is no legitimate way for a row to be visible to everyone.
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
