"""Run-scoped macro collection, reconciliation, storage and deterministic reporting."""

import json
import os
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Observation, period_end
from .providers import INDICATORS, PROVIDERS, Adapter, Result
from .reconcile import reconcile
from .store import MacroStore
from .transport import Transport, utc_now

TITLE = "Dane makro - źródła, rozbieżności i jakość"


def _list(value):
    return (
        [p.strip() for p in value.split(",") if p.strip()]
        if isinstance(value, str)
        else list(value)
    )


def database_path(config):
    return config.get("macro_db_path") or str(
        Path(config["results_dir"]).expanduser().resolve().parent / "macro" / "evidence.sqlite3"
    )


def collect(config, as_of, ticker="", *, adapters=None, now=None):
    explicit_now = now is not None
    now = now or utc_now()
    today = datetime.fromisoformat(now).astimezone(UTC).date().isoformat()
    datetime.strptime(as_of, "%Y-%m-%d")
    if as_of > today:
        raise ValueError("Macro analysis date cannot be in the future")
    historical = as_of < today
    cutoff = as_of + "T23:59:59.999999+00:00" if historical else now
    countries = _list(config.get("macro_countries", "US,DE,PL,EA"))
    indicators = _list(config.get("macro_indicators", ",".join(INDICATORS)))
    providers = _list(config.get("macro_providers", ",".join(PROVIDERS)))
    if set(providers) - set(PROVIDERS) or set(indicators) - set(INDICATORS):
        raise ValueError("Unknown macro provider/indicator; see docs/macroeconomics.md")
    if not all(c and c.replace("_", "").isalnum() for c in countries):
        raise ValueError("Invalid macro country/area code")
    store = MacroStore(database_path(config))
    days = int(config.get("macro_lookback_days", 90))
    if days < 1:
        raise ValueError("macro_lookback_days must be positive")
    start = (datetime.fromisoformat(as_of) - timedelta(days=days)).date().isoformat()
    open_conflicts = []
    for d in store.open_conflicts(as_of, cutoff):
        sample = (
            d.get("selected")
            or next(iter(d.get("candidates", [])), None)
            or next((e["observation"] for e in d.get("excluded", [])), None)
        )
        if sample and sample["country"] in countries and sample["indicator"] in indicators:
            open_conflicts.append(d)
    # Refresh older contested periods too, not just the most recent release.
    if open_conflicts:
        start = min(
            start,
            min(
                d["period"][:10]
                + ("-01" if len(d["period"]) == 7 else "-01-01" if len(d["period"]) == 4 else "")
                for d in open_conflicts
            ),
        )
    options = config.get("macro_options", {})
    refresh = config.get("macro_refresh_seconds", 3600)

    def fetch(provider):
        # Only a true as-of API (FRED) may fetch today's response for a
        # historical run. Others may use already-captured evidence below.
        if historical and provider != "fred":
            return Result(
                provider,
                "HISTORICAL_UNAVAILABLE",
                coverage=[
                    {"country": c, "indicator": i, "status": "HISTORICAL_UNAVAILABLE"}
                    for c in countries
                    for i in indicators
                ],
                errors=[
                    "Latest-only API; no certified historical vintage. Captured evidence only."
                ],
            )
        ttl = refresh.get(provider, 3600) if isinstance(refresh, dict) else refresh
        http = Transport(
            store,
            provider,
            float(ttl),
            float(config.get("macro_timeout", 30)),
            registered=bool(os.getenv("BLS_API_KEY")),
        )
        factory = adapters.get(provider, Adapter) if adapters else Adapter
        return factory(provider, http, countries, indicators, start, as_of, options).fetch()

    with ThreadPoolExecutor(max_workers=min(4, len(providers) or 1)) as pool:
        results = list(pool.map(fetch, providers))
    observations = [o for r in results for o in r.observations]
    if historical:
        observations.extend(
            o
            for o in store.captured(as_of)
            if o.provider in providers
            and o.country in countries
            and o.indicator in indicators
            and o.period >= start[: len(o.period)]
        )
    groups = defaultdict(list)
    for observation in observations:
        groups[observation.key].append(observation)
    # Missing responses never resolve an old conflict. Retain its candidates
    # as explicitly stale evidence and append an unresolved assessment.
    for prior in open_conflicts:
        if prior["key"] not in groups:
            from dataclasses import replace

            old = prior["candidates"] or [e["observation"] for e in prior["excluded"]]
            groups[prior["key"]] = [replace(Observation.from_dict(o), quality="STALE") for o in old]
    decisions = []
    prior_by_key, accepted_by_series = store.history(as_of, cutoff)
    last_selected = {}
    for key, candidates in sorted(groups.items(), key=lambda kv: (kv[1][0].period, kv[0])):
        o = candidates[0]
        previous = next(
            (p for p in accepted_by_series.get(o.series_key, []) if p.period <= o.period), None
        )
        # The preceding chronological observation from this same collected
        # window supplies a baseline even on a fresh database.
        earlier = last_selected.get(o.series_key)
        if earlier and (previous is None or earlier.period > previous.period):
            previous = earlier
        decision = reconcile(
            candidates,
            as_of,
            historical=historical,
            previous=previous,
            prior=prior_by_key.get(key),
        )
        decisions.append(decision)
        if decision.selected:
            last_selected[o.series_key] = decision.selected
    snapshot = {
        "run_id": f"macro-{uuid.uuid4().hex}",
        "ticker": ticker,
        "as_of": as_of,
        "evaluated_at": now if explicit_now else utc_now(),
        "historical": historical,
        "configuration": {
            "countries": countries,
            "indicators": indicators,
            "providers": providers,
            "refresh_seconds": refresh,
            "lookback_days": days,
            "options": options,
        },
        "providers": [
            {
                "provider": r.provider,
                "status": r.status,
                "count": len(r.observations),
                "coverage": r.coverage,
                "errors": r.errors,
                "cache_hits": r.cache_hits,
                "requests": r.requests,
            }
            for r in results
        ],
        "decisions": [d.to_dict() for d in decisions],
        "database": str(store.path),
    }
    snapshot["report"] = render(snapshot)
    snapshot["context"] = render_context(snapshot)
    store.save(snapshot, decisions, observations)
    # Immutable portable run evidence alongside SQLite; never in Git/BRAIN.
    evidence = store.path.parent / "runs" / f"{snapshot['run_id']}.json"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


def _escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _disclosed_readings(decision):
    result = [(c, c["quality"]) for c in decision["candidates"]]
    for e in decision["excluded"]:
        c = dict(e["observation"])
        # Keep raw rejected evidence in SQLite, never reveal future values in
        # analyst/tool/report text (even when labelled 'excluded').
        if e["reason"] in {
            "FUTURE_PERIOD",
            "FUTURE_PUBLICATION_OR_REVISION",
            "NO_POINT_IN_TIME_VINTAGE",
        }:
            c["value"] = "WITHHELD_POINT_IN_TIME"
        result.append((c, "EXCLUDED: " + e["reason"]))
    return result


def render(snapshot, indicator=None, look_back_days=None):
    """All alternatives for latest and contested periods, with full metadata.

    All fetched historical rows remain in SQLite/run JSON, not in LLM prose.
    """
    decisions = snapshot["decisions"]
    aliases = {
        "cpi": "cpi_index",
        "inflation": "cpi_yoy",
        "fed_funds_rate": "effr",
        "10y_treasury": "nominal_yield",
        "yield_curve": "nominal_yield",
        "ecb_deposit_rate": "policy_rate",
        "euro_hicp": "hicp_yoy",
    }
    wanted = aliases.get(indicator, indicator)
    if wanted:
        decisions = [
            d
            for d in decisions
            if (d.get("selected") or d["candidates"][:1] or [{}])
            and any(
                o["indicator"] == wanted or o["series_id"] == indicator for o in d["candidates"]
            )
        ]
    if look_back_days is not None:
        start = (date_from(snapshot["as_of"]) - timedelta(days=look_back_days)).isoformat()
        decisions = [
            d for d in decisions if period_end(d["period"], json.loads(d["series_key"])[2]) >= start
        ]
    latest = {}
    for d in decisions:
        if d["period"] > latest.get(d["series_key"], ""):
            latest[d["series_key"]] = d["period"]
    visible = [
        d
        for d in decisions
        if d["period"] == latest.get(d["series_key"])
        or d["conflict"]
        or d["transition"] == "UNRESOLVED_MISSING_DATA"
    ]
    lines = [
        f"Analysis ID: `{snapshot['run_id']}`; as of {snapshot['as_of']}; evaluated {snapshot['evaluated_at']}.",
        "PROVISIONAL_CONFLICT is a working choice, not an established fact. FIRST_OBSERVATION has no prior accepted value. "
        "Do not fill missing data with model memory. Republishers of a common producer are not independent confirmation.",
        "Conflict iff abs(a-b)/max(abs(a),abs(b)) > 0.05 (both zero = 0); exactly 5% is not a conflict. "
        "Rate differences also in percentage points. Previous value is a reference, never a replacement.",
        "Publication 'unknown' stays unknown; retrieval/dataset updated is not publication. "
        "Latest-only APIs excluded from historical backtests unless captured by the cutoff.",
        "",
        "### Dostawcy i braki",
        "",
        "| Provider | Status | Readings | Cache hits / requests | Detail |",
        "|---|---|---:|---|---|",
    ]
    for p in snapshot["providers"]:
        lines.append(
            f"| {p['provider']} | {p['status']} | {p['count']} | {p['cache_hits']} / {p['requests']} | {_escape('; '.join(p['errors']))} |"
        )
        absent = defaultdict(list)
        for c in p["coverage"]:
            if c["status"] != "OK" and (not wanted or c["indicator"] == wanted):
                absent[c["status"]].append(c["country"] + ":" + c["indicator"])
        for status, fields in absent.items():
            lines.append(f"\n{p['provider']} {status}: {', '.join(fields)}.")
    if not visible:
        lines.append(
            "\nNO_VALID_DATA: brak wymaganych porównywalnych odczytów. Do not invent values."
        )
    for d in visible:
        o = d.get("selected") or (
            d["candidates"][0] if d["candidates"] else d["excluded"][0]["observation"]
        )
        lag = (
            date_from(snapshot["as_of"]) - date_from(period_end(o["period"], o["frequency"]))
        ).days
        lines.extend(
            [
                "",
                f"### {o['country']} {o['name']} - {o['period']} ({o['frequency']}) {o['maturity']} {o['basis']}",
                f"Status: **{d['status']}**; conflict={d['conflict']}; re-evaluation={d['transition']}; observation lag={lag} days.",
                f"Definition: {_escape(o['definition'])}; seasonal adjustment={o['seasonal_adjustment']}; unit={o['unit']}.",
                f"Selected: {d['selected']['value'] if d.get('selected') else 'MISSING'}; "
                f"previous: {d['previous']['value'] + ' (' + d['previous']['period'] + ')' if d.get('previous') else 'none'}. {_escape(d['reason'])}",
                "",
                "| Value | Unit | Provider / original producer | Series / source | Publication / revision | Retrieved | Quality |",
                "|---:|---|---|---|---|---|---|",
            ]
        )
        for c, quality in _disclosed_readings(d):
            lines.append(
                f"| {c['value']} | {_escape(c['unit'])} | {_escape(c['provider'] + ' / ' + c['producer'])} | "
                f"[{_escape(c['series_id'])}]({c['source_url']}) | {c['published_at'] or 'unknown'} / {c['revised_at'] or 'unknown'} | "
                f"{c['retrieved_at']} | {quality} |"
            )
        for c in d["candidates"]:
            if c.get("notes"):
                lines.append(f"\n{c['provider']} metadata: {_escape(c['notes'])}")
        for p in d["pairs"]:
            lines.append(
                f"\n{p['a']['provider']} vs {p['b']['provider']}: relative={p['relative_difference']}; "
                f"pp={p['percentage_points'] or 'N/A'}; duplicate={p['duplicate']}; shared producer={p['shared_producer']}; "
                f"conflict={p['conflict']}."
            )
        for alert in d["alerts"]:
            lines.append(f"\n**ALERT: {_escape(alert)}**")
        if d["excluded"]:
            lines.append(
                "\nExcluded: "
                + "; ".join(f"{e['observation']['provider']} {e['reason']}" for e in d["excluded"])
            )
    lines.append(
        f"\nEvidence: SQLite `{snapshot['database']}`; immutable run JSON `runs/{snapshot['run_id']}.json`. "
        "Full periods, revisions, exclusions, duplicate pairs and decisions retained there. No automatic trading action."
    )
    return "\n".join(lines)


def date_from(s):
    return datetime.fromisoformat(s).date()


def render_context(snapshot):
    """Compact deterministic analyst context; human reports retain all details."""
    rows = snapshot["decisions"]
    latest = {}
    for d in rows:
        latest[d["series_key"]] = max(d["period"], latest.get(d["series_key"], ""))
    lines = [
        f"Macro evidence {snapshot['run_id']}, as of {snapshot['as_of']}; evaluated {snapshot['evaluated_at']}.",
        "Never supply missing numbers from model memory. PROVISIONAL_CONFLICT is not an established fact. "
        "FIRST_OBSERVATION has no previous accepted reading. Shared producer means no independent confirmation. "
        "Conflict: abs(a-b)/max(abs(a),abs(b)) >0.05, both zero=0; exactly5% not conflict; rate gaps in pp. "
        "Publication unknown stays unknown. Latest-only historical vintages restricted.",
    ]
    for p in snapshot["providers"]:
        lines.append(
            f"Provider {p['provider']}: {p['status']}, {p['count']} observations; "
            + "; ".join(p["errors"])
        )
    present = {(c["country"], c["indicator"]) for d in rows for c in d["candidates"]}
    missing = [
        f"{c}:{i}"
        for c in snapshot["configuration"]["countries"]
        for i in snapshot["configuration"]["indicators"]
        if (c, i) not in present
    ]
    if missing:
        lines.append("MISSING coverage: " + ", ".join(missing))
    for d in rows:
        if d["period"] != latest[d["series_key"]] and not d["conflict"]:
            continue
        o = d["selected"]
        if not o:
            lines.append(f"NO_VALID_DATA {d['key']} {d['transition']}")
        else:
            lines.append(
                f"{o['country']} {o['indicator']} {o['period']} ({o['frequency']}, {o['seasonal_adjustment']}, "
                f"{o['definition']}, {o['maturity']} {o['basis']}): selected {o['value']} {o['unit']} [{d['status']}]; "
                f"previous={d['previous']['value'] if d['previous'] else 'none'}; {d['transition']}."
            )
        for c, quality in _disclosed_readings(d):
            lines.append(
                f"  candidate {c['value']} {c['unit']}; {c['provider']}/{c['producer']}; {c['series_id']}; "
                f"publication={c['published_at'] or 'unknown'}, revision={c['revised_at'] or 'unknown'}, "
                f"retrieved={c['retrieved_at']}; {quality}; {c['source_url']}"
            )
        for alert in d["alerts"]:
            lines.append("  ALERT " + alert)
        if d["conflict"]:
            lines.append("  Choice rationale: " + d["reason"])
        for p in d["pairs"]:
            lines.append(
                f"  {p['a']['provider']}/{p['b']['provider']}: relative={p['relative_difference']}, "
                f"pp={p['percentage_points']}, duplicate={p['duplicate']}, shared producer={p['shared_producer']}"
            )
    return "\n".join(lines)


def tool_report(config, run_id, indicator, curr_date, look_back_days=None):
    snapshot = MacroStore(database_path(config)).run(run_id)
    if snapshot is None or snapshot["as_of"] != curr_date:
        return "NO_VALID_DATA: run-scoped macro evidence unavailable for this date. Do not use model memory."
    return render(snapshot, indicator, look_back_days)
