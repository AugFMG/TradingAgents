"""Comparable observations. Definitions are identifiers, not fuzzy text matches."""

import calendar
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal

THRESHOLD = Decimal("0.05")


def relative_difference(a: Decimal, b: Decimal) -> Decimal:
    denominator = max(abs(a), abs(b))
    return abs(a - b) / denominator if denominator else Decimal(0)


def period_end(period: str, frequency: str) -> str:
    if frequency == "A":
        return f"{int(period):04d}-12-31"
    if frequency == "M":
        year, month = map(int, period.split("-"))
        return date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    if frequency == "Q":
        year, quarter = period.split("-Q")
        month = int(quarter) * 3
        return date(int(year), month, calendar.monthrange(int(year), month)[1]).isoformat()
    return date.fromisoformat(period).isoformat()


@dataclass(frozen=True)
class Observation:
    country: str
    indicator: str
    name: str
    value: Decimal
    unit: str
    period: str
    frequency: str
    definition: str
    seasonal_adjustment: str
    provider: str
    producer: str
    series_id: str
    source_url: str
    retrieved_at: str
    published_at: str | None = None
    revised_at: str | None = None
    quality: str = "OK"
    maturity: str = ""
    basis: str = ""
    is_primary: bool = False
    vintage_as_of: str | None = None
    notes: str = ""

    def normalized(self):
        factors = {
            "fraction": ("percent", Decimal(100)),
            "basis_points": ("percent", Decimal("0.01")),
            "thousand_persons": ("persons", Decimal(1000)),
        }
        unit, factor = factors.get(self.unit, (self.unit, Decimal(1)))
        return replace(self, value=Decimal(str(self.value)) * factor, unit=unit)

    @property
    def series_key(self) -> str:
        o = self.normalized()
        return json.dumps(
            [
                o.country,
                o.indicator,
                o.frequency,
                o.definition,
                o.seasonal_adjustment,
                o.unit,
                o.maturity,
                o.basis,
            ],
            separators=(",", ":"),
        )

    @property
    def key(self) -> str:
        return self.series_key + "|" + self.period

    def to_dict(self):
        data = asdict(self)
        data["value"] = str(self.value)
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "value": Decimal(str(data["value"]))})

    def exclusion(self, as_of: str, historical: bool) -> str | None:
        """Unknown release dates never become invented publication dates.

        Non-vintage history is usable only if *captured* by the cutoff. A
        current response with an old period/publication is not an old vintage.
        """
        if not self.value.is_finite() or self.quality in {"INVALID", "WITHDRAWN", "STALE"}:
            return "INVALID_OR_STALE"
        try:
            if period_end(self.period, self.frequency) > as_of:
                return "FUTURE_PERIOD"
            for stamp in (self.published_at, self.revised_at):
                if stamp and stamp[:10] > as_of:
                    return "FUTURE_PUBLICATION_OR_REVISION"
            datetime.fromisoformat(self.retrieved_at)
        except ValueError:
            return "INVALID_DATE"
        if (
            historical
            and not (self.vintage_as_of and self.vintage_as_of <= as_of)
            and self.retrieved_at[:10] > as_of
        ):
            return "NO_POINT_IN_TIME_VINTAGE"
        return None
