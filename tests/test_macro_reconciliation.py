"""Economic semantics and decision history, independently of network or LLMs."""

from dataclasses import replace
from decimal import Decimal

import pytest

from tradingagents.macro.models import Observation, relative_difference
from tradingagents.macro.reconcile import reconcile

pytestmark = pytest.mark.unit
AS_OF = "2026-11-08"
CAPTURE = "2026-11-08T12:00:00+00:00"


def observation(value="4", provider="primary", **kwargs):
    args = {
        "country": "US",
        "indicator": "effr",
        "name": "Effective federal funds rate",
        "value": Decimal(value),
        "unit": "percent",
        "period": "2026-11-06",
        "frequency": "D",
        "definition": "EFFR",
        "seasonal_adjustment": "NA",
        "provider": provider,
        "producer": "NYFED",
        "series_id": "EFFR",
        "source_url": "https://markets.newyorkfed.org/",
        "retrieved_at": CAPTURE,
        "is_primary": provider == "primary",
    }
    return Observation(**{**args, **kwargs})


@pytest.mark.parametrize(
    "a,b,conflict",
    [
        ("95", "100", False),
        ("94.999", "100", True),
        ("0", "0", False),
        ("0", "1", True),
        ("-95", "-100", False),
        ("-94", "-100", True),
        ("-1", "1", True),
    ],
)
def test_strict_relative_threshold_and_percentage_points(a, b, conflict):
    d = reconcile([observation(a), observation(b, "relay")], AS_OF)
    assert d.conflict is conflict
    assert Decimal(d.pairs[0]["percentage_points"]) == abs(Decimal(a) - Decimal(b))
    assert relative_difference(Decimal(a), Decimal(b)) == Decimal(d.pairs[0]["relative_difference"])


def test_duplicates_are_not_independent_sources_and_provenance_survives():
    d = reconcile([observation(), observation(provider="fred")], AS_OF)
    assert not d.conflict
    assert d.pairs[0]["duplicate"] and d.pairs[0]["shared_producer"]
    assert [c.provider for c in d.candidates] == ["primary", "fred"]


def test_normalizes_units_before_comparing():
    d = reconcile([observation("4"), observation("0.04", "relay", unit="fraction")], AS_OF)
    assert d.pairs[0]["duplicate"]
    assert d.selected.value == 4


def test_choose_new_value_nearest_previous_even_when_relay():
    prev = observation("3.8", period="2026-11-05")
    d = reconcile([observation("5"), observation("3.9", "relay")], AS_OF, previous=prev)
    assert d.selected.value == Decimal("3.9")
    assert d.selected is not prev
    assert d.status == "PROVISIONAL_CONFLICT" and "Closest" in d.reason


def test_ties_primary_then_newest_release_and_first_observation():
    previous = observation("4", period="2026-11-05")
    a, b = observation("3"), observation("5", "relay", published_at=AS_OF)
    assert reconcile([a, b], AS_OF, previous=previous).selected.provider == "primary"
    c = observation("5", "other", is_primary=True, published_at=AS_OF)
    assert reconcile([a, c], AS_OF, previous=previous).selected.provider == "other"
    d = reconcile([a, b], AS_OF)
    assert d.status == "FIRST_OBSERVATION" and d.conflict
    assert d.selected == a and d.alerts


@pytest.mark.parametrize(
    "changed",
    [
        {"period": "2026-11-05"},
        {"country": "DE"},
        {"frequency": "M", "period": "2026-10"},
        {"definition": "POLICY_RATE"},
        {"seasonal_adjustment": "SA"},
        {"maturity": "10Y"},
        {"basis": "real"},
        {"indicator": "sofr"},
        {"unit": "index_2015_100"},
    ],
)
def test_rejects_incomparable_candidates(changed):
    d = reconcile([observation(), observation("10", "relay", **changed)], AS_OF)
    assert len(d.candidates) == 1 and not d.conflict
    assert d.excluded[0]["reason"] == "NOT_COMPARABLE"


def test_invalid_or_future_candidates_cannot_win():
    d = reconcile(
        [
            observation(),
            observation("NaN", "bad"),
            observation("3.9", "future", published_at="2026-11-09"),
        ],
        AS_OF,
    )
    assert len(d.candidates) == 1 and len(d.excluded) == 2


def test_real_consensus_jump_is_not_capped_or_marked_as_inter_source_conflict():
    d = reconcile(
        [observation("8"), observation("8", "relay")],
        AS_OF,
        previous=observation("4", period="2026-11-05"),
    )
    assert d.selected.value == 8 and not d.conflict
    assert any("CHANGE_GT_5_PERCENT" in alert for alert in d.alerts)


def test_revision_supersedes_old_value_without_erasing_evidence():
    old = observation("4", revised_at="2026-11-07")
    new = observation("6", revised_at=AS_OF)
    d = reconcile([old, new, observation("6", "relay")], AS_OF)
    assert d.selected.value == 6 and not d.conflict
    assert d.excluded[0]["reason"] == "SUPERSEDED_REVISION"


@pytest.mark.parametrize(
    "future", [{"retrieved_at": CAPTURE}, {"retrieved_at": CAPTURE, "published_at": "2026-11-05"}]
)
def test_old_publication_does_not_certify_current_response_as_historical_vintage(future):
    o = observation(**future)
    assert o.exclusion("2026-11-06", True) == "NO_POINT_IN_TIME_VINTAGE"
    assert replace(o, vintage_as_of="2026-11-06").exclusion("2026-11-06", True) is None
    assert (
        replace(o, retrieved_at="2026-11-06T12:00:00+00:00").exclusion("2026-11-06", True) is None
    )


def test_unavailable_competitor_does_not_resolve_conflict():
    old = reconcile([observation("4"), observation("6", "relay")], AS_OF).to_dict()
    d = reconcile([observation("4")], AS_OF, prior=old)
    assert d.conflict and d.transition == "UNRESOLVED_MISSING_DATA"
    assert d.excluded[0]["reason"] == "MISSING_FRESH_COMPETITOR"
    again = reconcile([observation("4")], AS_OF, prior=d.to_dict())
    assert again.conflict and again.transition == "UNRESOLVED_MISSING_DATA"


@pytest.mark.parametrize(
    "values,transition",
    [(("4", "6"), "PERSISTING"), (("3.8", "6"), "SELECTION_CHANGED"), (("4", "4"), "RESOLVED")],
)
def test_conflict_reassessment(values, transition):
    old = reconcile([observation("4"), observation("6", "relay")], AS_OF).to_dict()
    d = reconcile(
        [observation(values[0]), observation(values[1], "relay")],
        AS_OF,
        previous=Observation.from_dict(old["selected"]),
        prior=old,
    )
    assert d.transition == transition
