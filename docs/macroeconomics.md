# Macro evidence and conflict memory

This is a deterministic data layer, independent of the LLM provider. It works
with the existing ChatGPT subscription adapter. It does not need Gemini or an
OpenAI API key. Public macro APIs are keyless except optional FRED; an optional
BLS registration key increases its quota. Never commit credentials.

## Enable and configure

Opt in in the local, Git-ignored `.env` (or in the graph config):

```dotenv
TRADINGAGENTS_MACRO_ENABLED=true
TRADINGAGENTS_MACRO_COUNTRIES=US,DE,PL,EA
TRADINGAGENTS_MACRO_INDICATORS=cpi_index,cpi_yoy,unemployment,employment,effr,sofr,money_market,nominal_yield,real_yield,policy_rate,hicp_yoy,lending_rate
TRADINGAGENTS_MACRO_PROVIDERS=bls,nyfed,treasury,oecd,bis,eurostat,ecb,worldbank,fred
TRADINGAGENTS_MACRO_LOOKBACK_DAYS=90
TRADINGAGENTS_MACRO_TIMEOUT=30
TRADINGAGENTS_MACRO_REFRESH_SECONDS={"bls":21600,"nyfed":3600,"treasury":3600,"oecd":21600,"bis":21600,"eurostat":21600,"ecb":3600,"worldbank":86400,"fred":3600}
TRADINGAGENTS_MACRO_OPTIONS={"bis_frequency":"M"}
```

`macro_db_path` / `TRADINGAGENTS_MACRO_DB_PATH` may override the default
`Path(results_dir).parent / "macro/evidence.sqlite3"`. Paths inside any Git
checkout are rejected. For this workspace, the database and immutable per-run
JSON evidence are under `E:/Dropbox/Workspace-trading/tradingagents-data/macro/`.
The canonical symbols are in [watchlist.md](../watchlist.md). The list does not
create a portfolio, schedule jobs, or trigger trades.

Countries use ISO alpha-2, except `EA` explicitly means the ECB's changing euro
area (`U2`), mapped to BIS `XM`. `EA20`, `EA21`, `EU27_2020` remain distinct
Eurostat aggregates, never silently equated to `EA`. Eurostat covers supported
European countries. OECD's built-in alpha-2/alpha-3 mapping covers US, DE, PL,
FR, GB, JP, CA, AU, IT, ES, CH, CZ, SE, NO, DK, NL, KR; use `macro_options.oecd_areas`
to extend it with country codes checked against official OECD CL_AREA metadata.
Unknown/unavailable countries/indicators produce visible coverage statuses.
Not every provider serves every indicator. API failures do not stop other providers.

## Official endpoints and verified series

Documentation, SDMX structures and sample API responses were checked on
**2026-11-08**. Reduced response fixtures and their original URLs/capture times
are in `tests/fixtures/macro/SOURCES.json`. A successful live call is evidence
of access at that time, not a promise of future availability. No source-file
publisher checksum was provided; local hashes only identify our captured bytes.

| Provider | API / identifiers used | Definition / provenance and limits |
|---|---|---|
| BLS | POST `https://api.bls.gov/publicAPI/v1/timeseries/data/` (v2 when registered); `CUUR0000SA0`, `LNS14000000`, `CES0000000001` | CPI-U all urban all items, NSA index 1982-84=100; CPS U-3 age16+ SA; total nonfarm payrolls SA in thousands. CPI YoY is explicitly derived as `100*(index[t]/index[t-12]-1)` and stays distinct from index. Batch series and year windows. [Signatures](https://www.bls.gov/developers/api_signature_v2.htm), [FAQ/limits](https://www.bls.gov/developers/api_faqs.htm): unregistered 25 queries/day, 25 series, 10 years/request; registered 500/day, 50 series, 20 years; 50 requests/10s. App budget 20/450 per day. |
| NY Fed | `https://markets.newyorkfed.org/api/rates/all/search.json?startDate=...&endDate=...`; EFFR, SOFR, OBFR, TGCR, BGCR, SOFRAI averages30/90/180 and index | Daily rates remain distinct named definitions. Effective date is observation, revisionIndicator is a flag, neither is a publication date. One batch/window. [Official API docs](https://markets.newyorkfed.org/static/docs/markets-api.html). No numeric quota identified in the consulted documentation. |
| US Treasury | `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=2026`; real curve `data=daily_treasury_real_yield_curve` | Atom/XML, yearly batches; BC_* nominal par yields, TC_* real par yields; all returned maturity fields parsed separately (not *DISPLAY duplicates). Atom entry updated is revision metadata, original publication remains unknown. [XML contract](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed). No numeric quota identified. |
| OECD | `https://sdmx.oecd.org/public/rest/v1/data/`; `OECD.SDD.TPS,DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL,1.0` and legacy `DSD_PRICES@DF_PRICES_ALL,1.0`; `DSD_LFS@DF_IALFS_UNE_M,1.0`; `OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0` | Countries batched with `+`. Prices: M, CPI, PA, total `_T`, N, GY, N/HICP methodology separately. Both classification flows needed for current country coverage. Unemployment UNE_LF_M/PT_LF_SUB, age15+, total sex, SA+calendar Y. Financial IRLT/IRSTCI/IR3TIB monthly averages, not policy rates; IRLT standard long-term government bond benchmark (approximately10y), never daily Treasury par or ECB spot. US national CPI original producer BLS; HICP via Eurostat; other unknown lineage is explicitly unknown, not independent confirmation. [API](https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html), [limits](https://www.oecd.org/en/data/insights/data-explainers/2024/11/Api-best-practices-and-recommendations.html), [platform status](https://www.oecd.org/en/data/insights/data-explainers/2025/02/OECD-Data-Explorer-News.html): 60/hour, bot/VPN checks may apply; app budget55/hour. |
| BIS | `https://stats.bis.org/api/v1/data/WS_CBPOL/M.US+XM+PL?format=csv&startPeriod=...`; D or M configurable | Main policy instrument, potentially spliced or corridor midpoint; metadata COMPILATION, SOURCE_REF and SUPP_INFO_BREAKS preserved. Monthly = last business day; daily releases around mid-week. Not equated to ECB deposit or EFFR. [Topic/methodology](https://data.bis.org/topics/CBPOL), [API](https://stats.bis.org/api-doc/v2/). Original producer from SOURCE_REF. No numeric quota identified. |
| Eurostat | `https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr`; coicop18=TOTAL, unit=RCH_A; `une_rt_m`, age=TOTAL, sex=T, s_adj=SA, unit=PC_ACT | 2026 HICP uses COICOP2018; obsolete prc_hicp_manr is not substituted. Harmonised HICP NSA and EU LFS unemployment SA, age15-74 remain different from national CPI/BLS U3. Country/window-filtered JSON-stat; sparse/dense indexes supported. Eurostat harmonises national producers; OECD relays of its HICP are the same producer lineage. Dataset updated is not an observation publication/revision. [API](https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-getting-started). No numeric quota identified; limit requests to relevant countries and dimensions. |
| ECB | `https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR+MLFR+MRR_FR.LEV`; `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y` | Deposit, marginal lending, fixed-rate main refinancing each distinct; AAA government10y Svensson continuous-compounded spot is distinct from par/real yields. U2 changing composition; business-day observations represented daily with original FREQ in metadata. [API overview](https://data.ecb.europa.eu/help/api/overview), [examples](https://data.ecb.europa.eu/help/api/data-examples). No numeric quota identified. |
| World Bank | `https://api.worldbank.org/v2/country/US;DE;PL/indicator/FP.CPI.TOTL.ZG;SL.UEM.TOTL.ZS;FR.INR.LEND?source=2&format=json`; per-indicator metadata | Annual CPI annual-average change, modelled ILO annual unemployment, lending interest rate (not policy). Frequency A and lag days always explicit; sourceOrganization/sourceNote retained. Dataset lastupdated is not per-observation publication/revision. Paginated batches, no gap-fill. [Query/batch contract](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures). No numeric quota identified. |
| FRED (optional) | `https://api.stlouisfed.org/fred/series/observations`; CPIAUCNS, UNRATE, PAYEMS, EFFR, SOFR, DGS10, DFII10 | Requires FRED_API_KEY; producer maps to BLS/NYFED/US_TREASURY. Both realtime_start and realtime_end pinned to analysis date. [Observations contract](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [limits/terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html). Historical-vintage behavior covered by mocked contract tests; live FRED is NOT_CONFIGURED in this workspace. |

The local default safety budget for other providers is240 requests/hour, with
0.2s minimum pacing per sequential adapter call and maximum4 provider workers.
These are application budgets, not claimed upstream quotas. HTTP429 includes
Retry-After and is not retried; timeouts/errors remain visible. Cache never
turns an expired failed response into a fresh success. Other applications/IP
users can still exhaust a provider's limit. No API/service secrets enter URLs
saved in reports or plaintext cache keys.

## Comparability and decisions

An immutable Observation stores country, indicator id/name, value/unit,
period/frequency, exact definition/adjustment, provider, original producer,
series/source URL, known publication/revision, actual capture time, quality,
maturity, nominal/real basis, optional certified as-of vintage and metadata.
Fractions/basis points normalize to percent; thousands of persons to persons.
Index bases are preserved, never silently rebased. Unknown publication is null.

Comparison keys require equal country, indicator, period, frequency, definition,
adjustment, normalized unit, maturity and basis. CPI index != CPI YoY != HICP;
policy != EFFR != SOFR; SA != NSA != SA+calendar; daily != monthly != annual;
nominal != real; par != modelled spot; different maturities never conflict.
Different definitions remain visible as separate series rather than being fused.

Every comparable pair computes `abs(a-b)/max(abs(a),abs(b))`, bothzero=0.
**Conflict iff >0.05**, not >=, and not5 percentage points. Rates also show the
absolute gap in percentage points. Equal normalized readings are duplicates;
same known producer is common lineage even when values differ. Preserve both
provider paths; never claim those are independent votes. Unknown lineage does
not establish independence.

For conflicting candidates choose the valid new value closest in absolute
distance to the previous accepted selection for the same canonical series.
Tie: primary producer, then newest known publication/revision, then stable
provider/series id for reproducibility. The preceding evaluated observation in
the current chronological window can establish a baseline on first ingestion.
The previous value is never a replacement for a new candidate. Exclude invalid,
incomparable, future, stale and superseded same-provider revisions. Keep their
evidence. Mark PROVISIONAL_CONFLICT; if there is no previous selection mark
FIRST_OBSERVATION and still raise a conflict alert. The independently computed
>5% change versus previous is another alert. Consensus jumps are not capped.

Each later analysis fetches expired data and includes older open-conflict
periods in its refresh window. Append a NEW/PERSISTING/SELECTION_CHANGED/RESOLVED
assessment; a missing competitor produces UNRESOLVED_MISSING_DATA, never false
resolution. SQLite/WAL keeps immutable runs, raw observations/revisions,
run-observation links, comparison pairs, exclusions, previous/selected values,
reasons, alerts and decision timestamps. Cache is separate and replaceable.
No earlier run/decision is overwritten. Runtime JSON makes a run portable.

## Point-in-time limits and reporting

Historical analyses exclude current responses from latest-only APIs, even if
they contain an old period or an old publication date. Unknown vintage history
cannot be reconstructed from a current value. Only snapshots already captured
by the analysis date or certified FRED as-of responses can be used. Future
publication/revision/observation dates and future decision history are excluded.
This is a conservative daily cutoff (UTC end-of-day), not an intraday backtest.
No historical revision accuracy is claimed for providers without vintages.

Collection runs once at graph state creation for CLI and API. Analysts receive
deterministic selected and competing values, dates, definitions, gaps and
warnings. The tool reads that immutable run and clamps dates to graph state;
it cannot silently refetch a different snapshot. Unsupported indicators return
NO_VALID_DATA, without model estimates. Enabling macro evidence intentionally
uses the configured macro_providers instead of the legacy FRED-only router;
when disabled the original routing remains available.

Both Markdown and HTML contain the deterministic section
**Dane makro - źródła, rozbieżności i jakość**. All alternatives for latest and
contested periods appear there; full period/revision history is in runtime
SQLite/JSON. Human reports retain full detail; LLM context omits repetitive
coverage/metadata prose. Collection/persistence failure is visible and does not
abort the rest of the instrument analysis. BRAIN holds synthesis and links,
not the database of competing readings.

## Verification and rollback

Run `python -m pytest -q tests/test_macro_reconciliation.py tests/test_macro_providers.py tests/test_macro_service.py`
for offline contracts, decisions, restarts, date isolation, cache, failure,
tool injection and MD/HTML integration. Frozen responses test parsing; only a
live probe proves that an endpoint is reachable. Review RUN_VERIFICATION in
BRAIN for actual test counts, live status and separately identified AAPL/NVDA
runs. Tests are not an investment-strategy or full security audit.

Disable TRADINGAGENTS_MACRO_ENABLED to return to the existing macro vendor
router without deleting evidence. To revert code, use a separate reviewed Git
revert of the task commit; keep runtime evidence and ChatGPT credentials outside
Git. Never remove the database to hide a prior decision.
