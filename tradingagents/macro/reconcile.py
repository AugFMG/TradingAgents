"""Selection is deterministic and never averages away real macro changes."""

from dataclasses import dataclass, field
from itertools import combinations

from .models import THRESHOLD, Observation, relative_difference


@dataclass
class Decision:
    key: str
    series_key: str
    period: str
    selected: Observation | None
    previous: Observation | None
    candidates: list[Observation]
    status: str
    conflict: bool = False
    reason: str = ""
    alerts: list[str] = field(default_factory=list)
    pairs: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    transition: str = "NEW"

    def to_dict(self):
        return {
            **self.__dict__,
            "selected": self.selected.to_dict() if self.selected else None,
            "previous": self.previous.to_dict() if self.previous else None,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def _prefer(candidates: list[Observation], previous: Observation | None) -> Observation:
    # Stable id breaks a completely unspecified tie reproducibly. Unknown
    # publication ranks before nothing only after primary-source preference.
    ordered = sorted(candidates, key=lambda o: (o.provider, o.series_id))
    ordered.sort(key=lambda o: max(o.published_at or "", o.revised_at or ""), reverse=True)
    ordered.sort(key=lambda o: not o.is_primary)
    if previous:
        ordered.sort(key=lambda o: abs(o.value - previous.normalized().value))
    return ordered[0]


def reconcile(
    candidates: list[Observation],
    as_of: str,
    *,
    historical=False,
    previous: Observation | None = None,
    prior: dict | None = None,
) -> Decision:
    if not candidates:
        raise ValueError("reconcile requires a target observation")
    target = candidates[0].normalized()
    decision = Decision(
        target.key, target.series_key, target.period, None, previous, [], "NO_VALID_DATA"
    )
    eligible = []
    for raw in candidates:
        o = raw.normalized()
        reason = o.exclusion(as_of, historical)
        if o.key != target.key:
            reason = "NOT_COMPARABLE"
        if reason:
            decision.excluded.append({"observation": raw.to_dict(), "reason": reason})
        else:
            eligible.append(o)
    # Keep revisions as evidence, but only the newest vintage from a given
    # provider/series/period competes. Conflicting values in the same vintage
    # remain candidates, rather than being silently discarded.
    for o in eligible:
        version = (o.revised_at or o.published_at or "", o.retrieved_at)
        superseded = any(
            (n.provider, n.series_id) == (o.provider, o.series_id)
            and (n.revised_at or n.published_at or "", n.retrieved_at) > version
            for n in eligible
        )
        if superseded:
            decision.excluded.append({"observation": o.to_dict(), "reason": "SUPERSEDED_REVISION"})
        else:
            decision.candidates.append(o)
    if previous and (
        previous.series_key != target.series_key
        or previous.period > target.period
        or previous.exclusion(as_of, historical)
    ):
        previous = decision.previous = None
    for a, b in combinations(decision.candidates, 2):
        diff = relative_difference(a.value, b.value)
        shared = a.producer == b.producer and not a.producer.startswith("UNKNOWN")
        decision.pairs.append(
            {
                "a": a.to_dict(),
                "b": b.to_dict(),
                "relative_difference": str(diff),
                "percentage_points": str(abs(a.value - b.value)) if a.unit == "percent" else None,
                "duplicate": a.value == b.value,
                "shared_producer": shared,
                "conflict": diff > THRESHOLD,
            }
        )
        decision.conflict |= diff > THRESHOLD
    if not decision.candidates:
        decision.alerts.append("NO_VALID_DATA: no comparable point-in-time candidates")
        if prior and (
            prior.get("conflict") or prior.get("transition") == "UNRESOLVED_MISSING_DATA"
        ):
            decision.transition = "UNRESOLVED_MISSING_DATA"
            decision.conflict = True
        return decision
    if prior and prior.get("conflict"):
        old_candidates = prior.get("candidates", []) + [
            e["observation"]
            for e in prior.get("excluded", [])
            if e["reason"] == "MISSING_FRESH_COMPETITOR"
            or e["observation"].get("quality") == "STALE"
        ]
        required = {(c["provider"], c["series_id"]) for c in old_candidates}
        available = {(c.provider, c.series_id) for c in decision.candidates}
        if required - available:
            # Keep missing competitors in the immutable evidence, excluded
            # from selection. Absence is not evidence of convergence.
            decision.excluded.extend(
                {"observation": c, "reason": "MISSING_FRESH_COMPETITOR"}
                for c in old_candidates
                if (c["provider"], c["series_id"]) not in available
            )
            decision.conflict = True
            decision.transition = "UNRESOLVED_MISSING_DATA"
            decision.alerts.append("MISSING_FRESH_COMPETITOR: cannot declare conflict resolved")
    selected = decision.selected = _prefer(
        decision.candidates, previous if decision.conflict else None
    )
    decision.status = (
        "FIRST_OBSERVATION"
        if previous is None
        else ("PROVISIONAL_CONFLICT" if decision.conflict else "ACCEPTED")
    )
    decision.reason = (
        f"Closest valid new value to previous {previous.value}; "
        if previous and decision.conflict
        else "No previous accepted value; "
        if previous is None
        else "Comparable sources within tolerance; "
    ) + "ties: primary producer, newest known publication/revision, stable provider/series id."
    if decision.conflict:
        decision.alerts.append(
            "CONFLICT_OPEN: a prior competitor is unavailable; choice remains PROVISIONAL_CONFLICT"
            if decision.transition == "UNRESOLVED_MISSING_DATA"
            else "CONFLICT: relative difference > 0.05; selected value is PROVISIONAL_CONFLICT"
        )
    if previous and relative_difference(selected.value, previous.normalized().value) > THRESHOLD:
        decision.alerts.append(
            "CHANGE_GT_5_PERCENT: selected new value changed >5% vs previous; no smoothing"
        )
    if prior and prior.get("conflict"):
        old = prior.get("selected") or {}
        changed = (old.get("value"), old.get("provider"), old.get("series_id")) != (
            str(selected.value),
            selected.provider,
            selected.series_id,
        )
        if decision.transition != "UNRESOLVED_MISSING_DATA":
            decision.transition = (
                ("SELECTION_CHANGED" if changed else "PERSISTING")
                if decision.conflict
                else "RESOLVED"
            )
    return decision
