"""Principal + Policy — the ONLY decision point for who may do what.

CONTRACT.md §2.1: identity is never read from the request body or from a
client header. A Principal is built server-side (see app.api.deps.get_principal)
and every route asks the Policy, never a client-supplied field, whether the
school in the path is allowed. A future login replaces the PrincipalProvider
and the Policy; the publish/domain logic underneath does not change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Principal:
    kind: str          # "vpn_admin" today; a future login adds more kinds.
    subject: str        # opaque technical identifier, never a client-supplied one.
    schools: "SchoolSelector"


class SchoolSelector:
    """Either every active school, or an explicit allow-set.

    `allows()` is a pure membership check — it does not know which schools
    are actually active; that is a property of the school itself, checked
    separately against the catalog. This keeps the policy testable without a
    live search service.
    """

    def __init__(self, *, all_active: bool = False, codes: frozenset[str] = frozenset()):
        self.all_active = all_active
        self.codes = codes

    def allows(self, school_code: str) -> bool:
        if self.all_active:
            return True
        return school_code in self.codes

    @classmethod
    def all(cls) -> "SchoolSelector":
        return cls(all_active=True)

    @classmethod
    def only(cls, *codes: str) -> "SchoolSelector":
        return cls(codes=frozenset(codes))


class Policy(Protocol):
    def allowed_schools(self, p: Principal) -> SchoolSelector: ...
    def may_read_pack(self, p: Principal, school: str) -> bool: ...
    def may_publish(self, p: Principal, school: str) -> bool: ...
    def may_replace(self, p: Principal, school: str) -> bool: ...
    def may_manage_categories(self, p: Principal) -> bool: ...


class VpnAdminPolicy:
    """MVP policy: one administrator behind the VPN, every active school.

    Being "VPN admin" is a technical context, not proof of a physical person's
    identity (brief.md §Publikování). It still goes through this single
    interface so a future login only swaps the provider + policy.
    """

    def allowed_schools(self, p: Principal) -> SchoolSelector:
        return SchoolSelector.all()

    def may_read_pack(self, p: Principal, school: str) -> bool:
        return self.allowed_schools(p).allows(school)

    def may_publish(self, p: Principal, school: str) -> bool:
        return self.allowed_schools(p).allows(school)

    def may_replace(self, p: Principal, school: str) -> bool:
        return self.allowed_schools(p).allows(school)

    def may_manage_categories(self, p: Principal) -> bool:
        return True


def build_vpn_admin_principal() -> Principal:
    """The only place a Principal is constructed. Never fed by request data."""
    return Principal(kind="vpn_admin", subject="vpn-admin", schools=SchoolSelector.all())
