"""Persistence/restart, point-in-time isolation, refresh and real tool/report wiring."""

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tests.test_macro_reconciliation import observation
from tradingagents.agents.context import get_instrument_context_from_state
from tradingagents.agents.state import AgentState
from tradingagents.agents.tools import get_macro_indicators
from tradingagents.dataflows.config import run_config
from tradingagents.macro.providers import Result
from tradingagents.macro.service import TITLE, collect, tool_report
from tradingagents.macro.store import MacroStore
from tradingagents.macro.transport import ProviderError, Transport
from tradingagents.reporting import write_report_tree

pytestmark = pytest.mark.unit
NOW = "2026-11-08T13:00:00+00:00"


@pytest.fixture
def config(tmp_path):
    return {
        "results_dir": str(tmp_path / "results"),
        "macro_db_path": str(tmp_path / "macro" / "evidence.sqlite3"),
        "macro_countries": "US",
        "macro_indicators": "effr",
        "macro_providers": "nyfed,fred",
        "macro_enabled": True,
    }


def fake_adapters(values, at=NOW):
    class Fake:
        def __init__(self, provider, *args):
            self.provider = provider

        def fetch(self):
            value = values.get(self.provider)
            if value is None:
                return Result(self.provider, "ERROR", errors=["HTTP_503"])
            return Result(
                self.provider,
                observations=[
                    observation(
                        str(value),
                        self.provider,
                        is_primary=self.provider == "nyfed",
                        retrieved_at=at,
                    )
                ],
            )

    return {"nyfed": Fake, "fred": Fake}


def latest(s):
    return s["decisions"][-1]


def test_restart_preserves_old_runs_revisions_and_conflict_transitions(config):
    s1 = collect(
        config, "2026-11-08", "AAPL", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW
    )
    original = MacroStore(config["macro_db_path"]).run(s1["run_id"])
    assert latest(s1)["conflict"] and latest(s1)["status"] == "FIRST_OBSERVATION"
    s2 = collect(
        config,
        "2026-11-08",
        "NVDA",
        adapters=fake_adapters({"nyfed": 4, "fred": 6}, "2026-11-08T14:00:00+00:00"),
        now="2026-11-08T14:00:01+00:00",
    )
    assert latest(s2)["transition"] == "PERSISTING" and latest(s2)["previous"]["value"] == "4"
    s3 = collect(
        config,
        "2026-11-08",
        "AAPL",
        adapters=fake_adapters({"nyfed": 4, "fred": 4}, "2026-11-08T15:00:00+00:00"),
        now="2026-11-08T15:00:01+00:00",
    )
    assert latest(s3)["transition"] == "RESOLVED"
    store = MacroStore(config["macro_db_path"])
    assert store.run(s1["run_id"]) == original
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 3
    assert not store.open_conflicts("2026-11-08", "2026-11-08T23:59:59+00:00")


def test_missing_provider_continues_and_cannot_resolve_conflict(config):
    collect(config, "2026-11-08", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW)
    s2 = collect(
        config, "2026-11-08", adapters=fake_adapters({"nyfed": 4}), now="2026-11-08T14:00:00+00:00"
    )
    assert latest(s2)["conflict"] and latest(s2)["transition"] == "UNRESOLVED_MISSING_DATA"
    assert "HTTP_503" in s2["report"] and latest(s2)["selected"]["value"] == "4"
    for hour in [15, 16]:
        s = collect(
            config, "2026-11-08", adapters=fake_adapters({}), now=f"2026-11-08T{hour}:00:00+00:00"
        )
        assert latest(s)["transition"] == "UNRESOLVED_MISSING_DATA"
        assert latest(s)["selected"] is None


def test_backtest_does_not_fetch_latest_only_provider_or_leak_future_decisions(config):
    collect(config, "2026-11-08", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW)
    config["macro_providers"] = "nyfed"

    class MustNotFetch:
        def __init__(self, *a):
            raise AssertionError("latest-only adapter called in backtest")

    historical = collect(config, "2026-11-06", adapters={"nyfed": MustNotFetch}, now=NOW)
    assert historical["decisions"] == []
    assert historical["providers"][0]["status"] == "HISTORICAL_UNAVAILABLE"
    store = MacroStore(config["macro_db_path"])
    assert store.previous(observation(), "2026-11-06", "2026-11-06T23:59:59+00:00") is None
    assert store.open_conflicts("2026-11-06", "2026-11-06T23:59:59+00:00") == []


def test_captured_historical_data_is_usable_without_inventing_release(config):
    config["macro_providers"] = "nyfed"
    collect(
        config,
        "2026-11-06",
        adapters=fake_adapters({"nyfed": 4}, "2026-11-06T12:00:00+00:00"),
        now="2026-11-06T13:00:00+00:00",
    )
    s = collect(config, "2026-11-06", now=NOW)
    assert latest(s)["selected"]["value"] == "4"
    assert latest(s)["selected"]["published_at"] is None
    assert latest(s)["selected"]["retrieved_at"].startswith("2026-11-06")


def test_tool_arguments_hidden_state_injection_and_report_formats(config, tmp_path):
    s = collect(
        config, "2026-11-08", "AAPL", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_macro_indicators",
                        "args": {
                            "indicator": "effr",
                            "curr_date": "2026-12-01",
                            "look_back_days": 30,
                        },
                        "id": "macro-1",
                    }
                ],
            )
        ],
        "trade_date": "2026-11-08",
        "company_of_interest": "AAPL",
        "instrument_context": "Apple Inc.",
        "macro_report": s["report"],
        "macro_run_id": s["run_id"],
    }
    builder = StateGraph(AgentState)
    builder.add_node("tools", ToolNode([get_macro_indicators]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    with run_config(config):
        result = builder.compile().invoke(state)
    content = result["messages"][-1].content
    assert s["run_id"] in content and "4" in content and "6" in content
    assert "PROVISIONAL_CONFLICT" in content and "unknown / unknown" in content
    schema = get_macro_indicators.tool_call_schema.model_json_schema()
    assert "state" not in schema["properties"] and "trade_date" not in schema["properties"]
    assert set(schema["required"]) == {"indicator", "curr_date"}
    assert "macro_report" in AgentState.__annotations__
    context = get_instrument_context_from_state(state)
    assert "Apple Inc." in context and s["run_id"] in context and "CONFLICT" in context
    report = write_report_tree(state, "AAPL", tmp_path / "report")
    assert TITLE in report.read_text(encoding="utf-8")
    html = report.with_suffix(".html").read_text(encoding="utf-8")
    assert TITLE in html and "markets.newyorkfed.org" in html and "unknown / unknown" in html


def test_tool_cannot_serve_other_date_or_missing_indicator(config):
    s = collect(config, "2026-11-08", adapters=fake_adapters({"nyfed": 4}), now=NOW)
    assert "NO_VALID_DATA" in tool_report(config, s["run_id"], "effr", "2026-10-01")
    assert "NO_VALID_DATA" in tool_report(config, s["run_id"], "core_pce", "2026-11-08")


def test_database_refuses_git_location(config):
    from tradingagents import macro

    path = __import__("pathlib").Path(macro.__file__).parents[2] / "evidence.sqlite3"
    with pytest.raises(ValueError, match="outside Git"):
        MacroStore(path)


def test_http_cache_refresh_and_no_secret_persistence(config, monkeypatch):
    store = MacroStore(config["macro_db_path"])
    calls = []

    class Response:
        status_code = 200
        headers = {}
        content = b'{"value":4}'

    def request(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr("tradingagents.macro.transport.requests.request", request)
    monkeypatch.setattr("tradingagents.macro.transport.time.sleep", lambda *a: None)
    http = Transport(store, "fred", 3600)
    a = http.get(
        "https://api.stlouisfed.org/fred/series/observations",
        {"series_id": "EFFR", "api_key": "secret-test-value"},
    )
    b = http.get(
        "https://api.stlouisfed.org/fred/series/observations",
        {"series_id": "EFFR", "api_key": "secret-test-value"},
    )
    assert a == b and len(calls) == 1 and http.cache_hits == 1
    Transport(store, "fred", 0).get(
        "https://api.stlouisfed.org/fred/series/observations",
        {"series_id": "EFFR", "api_key": "secret-test-value"},
    )
    assert len(calls) == 2
    with store.connect() as db:
        dumped = "\n".join(db.iterdump())
    assert "secret-test-value" not in dumped


def test_http_429_is_not_retried_or_served_as_stale_success(config, monkeypatch):
    store = MacroStore(config["macro_db_path"])

    class Response:
        status_code = 429
        headers = {"Retry-After": "3600"}

    calls = []
    monkeypatch.setattr(
        "tradingagents.macro.transport.requests.request",
        lambda *a, **k: calls.append(a) or Response(),
    )
    monkeypatch.setattr("tradingagents.macro.transport.time.sleep", lambda *a: None)
    with pytest.raises(ProviderError, match="HTTP_429"):
        Transport(store, "oecd", 0).get("https://sdmx.oecd.org/public/rest/v1/data/test")
    assert len(calls) == 1


def test_graph_creation_collects_once_and_records_actual_macro_vendor(config, monkeypatch):
    from tradingagents.graph.propagation import Propagator
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    snapshot = collect(
        config, "2026-11-08", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW
    )
    calls = []
    monkeypatch.setattr(
        "tradingagents.macro.service.collect", lambda *a: calls.append(a) or snapshot
    )
    monkeypatch.setattr("tradingagents.graph.trading_graph.get_current_date", lambda: "2026-11-08")
    graph = object.__new__(TradingAgentsGraph)
    graph.config = config
    graph.propagator = Propagator()
    graph.selected_analysts = ("market", "news")
    graph.resolve_instrument_context = lambda *a: "Apple Inc."
    state = graph.create_run_state("AAPL", "2026-11-08")
    assert len(calls) == 1
    assert state["macro_run_id"] == snapshot["run_id"]
    assert state["macro_report"] == snapshot["report"]
    assert "4" in state["macro_context"] and "6" in state["macro_context"]
    assert graph.run_settings()["data_vendors"]["macro_data"] == "multi_source: nyfed,fred"


def test_refresh_includes_old_open_conflict_after_window_moves(config):
    # November 6 remains contested when outside the nominal one-day window.
    collect(config, "2026-11-08", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW)
    config["macro_lookback_days"] = 1
    starts = []

    class Recording:
        def __init__(self, provider, http, countries, indicators, start, end, options):
            self.provider = provider
            starts.append(start)

        def fetch(self):
            return Result(self.provider, "ERROR")

    s = collect(
        config,
        "2026-11-10",
        adapters={"nyfed": Recording, "fred": Recording},
        now="2026-11-10T13:00:00+00:00",
    )
    assert starts == ["2026-11-06", "2026-11-06"]
    assert latest(s)["transition"] == "UNRESOLVED_MISSING_DATA"


def test_missing_then_single_competitor_does_not_false_resolve(config):
    collect(config, "2026-11-08", adapters=fake_adapters({"nyfed": 4, "fred": 6}), now=NOW)
    collect(config, "2026-11-08", adapters=fake_adapters({}), now="2026-11-08T14:00:00+00:00")
    s = collect(
        config, "2026-11-08", adapters=fake_adapters({"nyfed": 4}), now="2026-11-08T15:00:00+00:00"
    )
    assert latest(s)["conflict"] and latest(s)["transition"] == "UNRESOLVED_MISSING_DATA"


def test_persistent_application_quota_blocks_network(config, monkeypatch):
    store = MacroStore(config["macro_db_path"])
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS requests (provider TEXT, requested_at TEXT)")
        db.executemany(
            "INSERT INTO requests VALUES (?,?)", [("oecd", "2099-01-01T00:00:00+00:00")] * 55
        )
    with pytest.raises(ProviderError, match="LOCAL_QUOTA_EXHAUSTED"):
        Transport(store, "oecd", 0).get("https://sdmx.oecd.org/public/rest/v1/data/test")


def test_excluded_future_values_never_reach_model_or_report_text(config):
    config["macro_providers"] = "fred"

    class Future:
        def __init__(self, *args):
            pass

        def fetch(self):
            return Result(
                "fred",
                observations=[
                    observation(
                        "987654321", "fred", vintage_as_of="2026-11-06", published_at="2026-11-09"
                    )
                ],
            )

    s = collect(config, "2026-11-06", adapters={"fred": Future}, now=NOW)
    assert "987654321" not in s["report"] and "987654321" not in s["context"]
    assert "WITHHELD_POINT_IN_TIME" in s["report"]
    assert s["decisions"][0]["excluded"][0]["observation"]["value"] == "987654321"
