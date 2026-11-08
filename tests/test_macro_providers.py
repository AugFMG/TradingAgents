"""Parsers exercised against reduced official API responses, without live HTTP."""

import json
from pathlib import Path

import pytest

from tradingagents.macro.providers import INDICATORS, PROVIDERS, Adapter, jsonstat_rows
from tradingagents.macro.transport import ProviderError

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "macro"


class FixtureHTTP:
    cache_hits = requests = 0

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, body=None):
        self.calls.append((url, params, body))
        if "bls.gov" in url:
            assert body["seriesid"] == ["CUUR0000SA0", "LNS14000000", "CES0000000001"]
            filename = "bls.json"
        elif "newyorkfed.org" in url:
            assert params["endDate"] == "2026-11-08"
            filename = "nyfed.json"
        elif "treasury.gov" in url:
            filename = (
                "treasuryreal_fixed.xml" if "real" in params["data"] else "treasury_fixed.xml"
            )
        elif "stats.bis.org" in url:
            filename = "bis.csv"
        elif "ec.europa.eu" in url:
            filename = "eurostat_hicp.json" if "hicp" in url else "eurostat_unemp.json"
        elif "data-api.ecb.europa.eu" in url:
            filename = "ecb_yield.csv" if "/YC/" in url else "ecb_rates.csv"
        elif "sdmx.oecd.org" in url:
            if "FINMARK" in url:
                filename = "oecd_fin_data.csv"
            elif "PRICES" in url:
                assert ".CPI.PA._T.N.GY" in url
                filename = "oecd_prices_data.csv" if "2018" in url else "oecd_prices_old_data.csv"
            else:
                # Separate synthetic minimal contract (same SDMX columns),
                # age 15+ SA+calendar must not be equated to BLS age 16+ SA.
                return (
                    "REF_AREA,FREQ,MEASURE,UNIT_MEASURE,TRANSFORMATION,ADJUSTMENT,SEX,AGE,ACTIVITY,TIME_PERIOD,OBS_VALUE,UNIT_MULT\nUSA,M,UNE_LF_M,PT_LF_SUB,_Z,Y,_T,Y_GE15,_Z,2026-09,4.1,0\n",
                    "2026-11-08T12:00:00+00:00",
                )
        elif "worldbank.org" in url:
            filename = "wb.json" if "/country/" in url else "wb_meta.json"
        else:
            raise AssertionError(url)
        return (FIXTURES / filename).read_text(encoding="utf-8"), "2026-11-08T12:00:00+00:00"


@pytest.mark.parametrize("provider", PROVIDERS[:-1])
def test_each_public_adapter_parses_official_payloads(provider):
    http = FixtureHTTP()
    r = Adapter(
        provider, http, ["US", "DE", "PL", "EA"], INDICATORS, "2026-09-01", "2026-11-08"
    ).fetch()
    assert r.status == "OK", r.errors
    assert r.observations, provider
    assert all(
        o.source_url.startswith("https://") and o.definition and o.series_id and o.producer
        for o in r.observations
    )
    assert all(o.published_at is None for o in r.observations)
    if provider == "treasury":
        assert all(o.revised_at for o in r.observations)
        assert {o.basis for o in r.observations} == {"real", "nominal"}
    if provider == "worldbank":
        assert {o.frequency for o in r.observations} == {"A"}
        assert all("lag" in o.notes for o in r.observations)
    if provider == "oecd":
        assert any(o.producer == "BLS" and o.indicator == "cpi_yoy" for o in r.observations)
        assert any(o.producer == "Eurostat" and o.indicator == "hicp_yoy" for o in r.observations)


def test_bls_derived_yoy_is_not_an_index():
    r = Adapter("bls", FixtureHTTP(), ["US"], INDICATORS, "2026-09-01", "2026-11-08").fetch()
    indices = [o for o in r.observations if o.indicator == "cpi_index"]
    growth = [o for o in r.observations if o.indicator == "cpi_yoy"]
    assert indices and growth and indices[0].series_key != growth[0].series_key
    assert growth[0].unit == "percent" and growth[0].value < 100


def test_missing_configuration_coverage_and_outage_are_different(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    r = Adapter("fred", FixtureHTTP(), ["US", "DE"], ["effr"], "2026-09-01", "2026-11-08").fetch()
    assert r.status == "NOT_CONFIGURED"
    assert r.coverage == [
        {"country": "US", "indicator": "effr", "status": "NOT_CONFIGURED"},
        {"country": "DE", "indicator": "effr", "status": "NO_COVERAGE"},
    ]
    http = FixtureHTTP()
    monkeypatch.setattr(
        http, "get", lambda *a, **k: (_ for _ in ()).throw(ProviderError("HTTP_503"))
    )
    r = Adapter("nyfed", http, ["US", "DE"], ["effr"], "2026-09-01", "2026-11-08").fetch()
    assert r.status == "ERROR" and r.coverage[0]["status"] == "ERROR"
    assert r.coverage[1]["status"] == "NO_COVERAGE"
    assert (
        not Adapter("nyfed", http, ["DE"], ["effr"], "2026-09-01", "2026-11-08")
        .fetch()
        .observations
    )


def test_fred_pins_realtime_and_does_not_invent_publication(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-dummy")
    http = FixtureHTTP()

    def get(url, params=None, body=None):
        assert params["realtime_start"] == params["realtime_end"] == "2026-11-06"
        return json.dumps(
            {"observations": [{"date": "2026-11-05", "value": "4.1"}]}
        ), "2026-11-08T12:00:00+00:00"

    monkeypatch.setattr(http, "get", get)
    r = Adapter("fred", http, ["US"], ["effr"], "2026-09-01", "2026-11-06").fetch()
    assert r.observations[0].vintage_as_of == "2026-11-06"
    assert r.observations[0].published_at is None
    assert r.observations[0].exclusion("2026-11-06", True) is None


def test_jsonstat_sparse_and_dense_values_are_equivalent():
    data = {
        "id": ["geo", "time"],
        "dimension": {
            "geo": {"category": {"index": {"DE": 0, "PL": 1}}},
            "time": {"category": {"index": {"2026-08": 0, "2026-09": 1}}},
        },
        "value": {"1": 0, "2": 4},
    }
    sparse = list(jsonstat_rows(data))
    data["value"] = [None, 0, 4, None]
    assert list(jsonstat_rows(data)) == sparse
    assert sparse == [({"geo": "DE", "time": "2026-09"}, 0), ({"geo": "PL", "time": "2026-08"}, 4)]
