"""Entitlements: what a caller may see. Plans are data (plans + plan_entitlements tables) that map to
these flags; code only ever checks the flags, never a plan key or name.

Phase A has no login yet: every repository call defaults to FULL_ACCESS. The auth phase resolves a
user's active subscription -> plan -> Entitlements with `for_plan` and passes it to the repository,
which enforces it in the queries themselves (not by hiding UI).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from shots.db.models import Plan, PlanEntitlement

KEYS = ("can_view_all_leagues", "allowed_leagues", "max_days_ahead", "team_predictions", "export", "track_detail")


@dataclass(frozen=True)
class Entitlements:
    can_view_all_leagues: bool = True
    allowed_leagues: frozenset[str] = field(default_factory=frozenset)  # used when not can_view_all_leagues
    max_days_ahead: int | None = None  # None = no limit (whole season)
    team_predictions: bool = True
    export: bool = True
    track_detail: bool = True

    def league_allowed(self, competition: str) -> bool:
        return self.can_view_all_leagues or competition in self.allowed_leagues

    @classmethod
    def from_values(cls, values: dict) -> "Entitlements":
        """Unknown keys are ignored; missing keys get the most restrictive value (deny by default)."""
        mda = values.get("max_days_ahead", 0)
        return cls(
            can_view_all_leagues=bool(values.get("can_view_all_leagues", False)),
            allowed_leagues=frozenset(values.get("allowed_leagues") or ()),
            max_days_ahead=None if mda is None else int(mda),
            team_predictions=bool(values.get("team_predictions", False)),
            export=bool(values.get("export", False)),
            track_detail=bool(values.get("track_detail", False)),
        )


FULL_ACCESS = Entitlements()


def plan_values(session: Session, plan_key: str) -> dict:
    rows = session.execute(select(PlanEntitlement.key, PlanEntitlement.value)
                           .join(Plan, Plan.id == PlanEntitlement.plan_id)
                           .where(Plan.key == plan_key, Plan.is_active.is_(True))).all()
    return {k: v for k, v in rows}


def for_plan(session: Session, plan_key: str) -> Entitlements:
    """Entitlements of a plan (an unknown or inactive plan gets nothing: deny by default)."""
    return Entitlements.from_values(plan_values(session, plan_key))


def default_plan_key(session: Session) -> str | None:
    """The plan new registrations get (is_default)."""
    return session.scalar(select(Plan.key).where(Plan.is_default.is_(True), Plan.is_active.is_(True)))
