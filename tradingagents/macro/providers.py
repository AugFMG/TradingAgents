"""Official structured adapters. Verified series and definitions: docs/macroeconomics.md.

No economic values are supplied by an LLM. Provider responses are untrusted
data; parsers whitelist dimensions/fields and keep definitions separate.
"""

import csv
import io
import itertools
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .models import Observation
from .transport import ProviderError

PROVIDERS = ("bls", "nyfed", "treasury", "oecd", "bis", "eurostat", "ecb", "worldbank", "fred")
INDICATORS = (
    "cpi_index",
    "cpi_yoy",
    "unemployment",
    "employment",
    "effr",
    "sofr",
    "money_market",
    "nominal_yield",
    "real_yield",
    "policy_rate",
    "hicp_yoy",
    "lending_rate",
)
OECD_AREAS = {
    "US": "USA",
    "DE": "DEU",
    "PL": "POL",
    "FR": "FRA",
    "GB": "GBR",
    "JP": "JPN",
    "CA": "CAN",
    "AU": "AUS",
    "IT": "ITA",
    "ES": "ESP",
    "CH": "CHE",
    "CZ": "CZE",
    "SE": "SWE",
    "NO": "NOR",
    "DK": "DNK",
    "NL": "NLD",
    "KR": "KOR",
}
EURO_AREAS = {
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "EL",
    "ES",
    "FI",
    "FR",
    "HR",
    "HU",
    "IE",
    "IT",
    "LT",
    "LU",
    "LV",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SE",
    "SI",
    "SK",
    "EA20",
    "EA21",
    "EU27_2020",
}


@dataclass
class Result:
    provider: str
    status: str = "OK"
    observations: list[Observation] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cache_hits: int = 0
    requests: int = 0


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


class Adapter:
    def __init__(self, provider, transport, countries, indicators, start, end, options=None):
        self.provider, self.http = provider, transport
        self.countries, self.indicators = countries, indicators
        self.start, self.end = start, end
        self.options = options or {}
        self.result = Result(provider)

    def add(
        self,
        country,
        indicator,
        value,
        period,
        *,
        series,
        url,
        fetched,
        definition,
        frequency="M",
        unit="percent",
        sa="NSA",
        producer=None,
        primary=False,
        name=None,
        **kwargs,
    ):
        if (
            country not in self.countries
            or indicator not in self.indicators
            or value in (None, "", ".", "-")
        ):
            return
        try:
            amount = Decimal(str(value))
            if not amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            self.result.errors.append(f"INVALID_VALUE {series} {period}")
            return
        self.result.observations.append(
            Observation(
                country,
                indicator,
                name or indicator,
                amount,
                unit,
                period,
                frequency,
                definition,
                sa,
                self.provider,
                producer or f"UNKNOWN:{self.provider}:{country}",
                series,
                url,
                fetched,
                is_primary=primary,
                **kwargs,
            )
        )

    def request(self, url, params=None, body=None):
        return self.http.get(url, params, body)

    def json(self, url, params=None, body=None):
        text, at = self.request(url, params, body)
        return json.loads(text), at

    def supports(self, country, indicator):
        if self.provider == "bls":
            return country == "US" and indicator in {
                "cpi_index",
                "cpi_yoy",
                "unemployment",
                "employment",
            }
        if self.provider == "nyfed":
            return country == "US" and indicator in {"effr", "sofr", "money_market"}
        if self.provider == "treasury":
            return country == "US" and indicator in {"nominal_yield", "real_yield"}
        if self.provider == "oecd":
            return country != "EA" and indicator in {
                "cpi_yoy",
                "hicp_yoy",
                "unemployment",
                "money_market",
                "nominal_yield",
            }
        if self.provider == "bis":
            return indicator == "policy_rate"
        if self.provider == "eurostat":
            return country in EURO_AREAS and indicator in {"hicp_yoy", "unemployment"}
        if self.provider == "ecb":
            return country == "EA" and indicator in {"policy_rate", "nominal_yield"}
        if self.provider == "worldbank":
            return country not in {"EA", "EA20", "EA21", "EU27_2020"} and indicator in {
                "cpi_yoy",
                "unemployment",
                "lending_rate",
            }
        return country == "US" and indicator in {
            "cpi_index",
            "unemployment",
            "employment",
            "effr",
            "sofr",
            "nominal_yield",
            "real_yield",
        }

    def fetch(self):
        configured = not (self.provider == "fred" and not os.getenv("FRED_API_KEY"))
        if not configured:
            self.result.status = "NOT_CONFIGURED"
        else:
            try:
                getattr(self, self.provider)()
            except Exception as exc:
                # Never serialize a requests exception/URL containing a key.
                self.result.errors.append(
                    str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                )
                self.result.status = "PARTIAL_ERROR" if self.result.observations else "ERROR"
        if configured and not self.result.observations and not self.result.errors:
            self.result.status = "NO_COVERAGE"
        present = {(o.country, o.indicator) for o in self.result.observations}
        for country, indicator in itertools.product(self.countries, self.indicators):
            status = (
                "NO_COVERAGE"
                if not self.supports(country, indicator)
                else "OK"
                if (country, indicator) in present
                else (
                    self.result.status
                    if self.result.status in {"NOT_CONFIGURED", "ERROR", "PARTIAL_ERROR"}
                    else "NO_COVERAGE"
                )
            )
            self.result.coverage.append(
                {"country": country, "indicator": indicator, "status": status}
            )
        self.result.cache_hits, self.result.requests = self.http.cache_hits, self.http.requests
        return self.result

    def bls(self):
        if "US" not in self.countries:
            return
        specs = {
            "CUUR0000SA0": ("cpi_index", "CPI_U_ALL_URBAN_1982_84", "index_1982_84_100", "NSA"),
            "LNS14000000": ("unemployment", "US_CPS_U3_AGE16PLUS", "percent", "SA"),
            "CES0000000001": (
                "employment",
                "US_CES_TOTAL_NONFARM_PAYROLLS",
                "thousand_persons",
                "SA",
            ),
        }
        series = [
            s
            for s, spec in specs.items()
            if spec[0] in self.indicators or (s == "CUUR0000SA0" and "cpi_yoy" in self.indicators)
        ]
        if not series:
            return
        version = "v2" if os.getenv("BLS_API_KEY") else "v1"
        url = f"https://api.bls.gov/publicAPI/{version}/timeseries/data/"
        first = int(self.start[:4]) - 1  # previous-year CPI required for YoY
        body = {"seriesid": series, "startyear": str(first), "endyear": self.end[:4]}
        if os.getenv("BLS_API_KEY"):
            body["registrationkey"] = os.environ["BLS_API_KEY"]
        # Respect 10-year keyless/20-year registered request windows.
        width = 20 if "registrationkey" in body else 10
        for year in range(first, int(self.end[:4]) + 1, width):
            payload, fetched = self.json(
                url,
                body={
                    **body,
                    "startyear": str(year),
                    "endyear": str(min(year + width - 1, int(self.end[:4]))),
                },
            )
            if payload.get("status") != "REQUEST_SUCCEEDED":
                raise ProviderError("BLS_REQUEST_FAILED (quota or invalid request)")
            for s in payload.get("Results", {}).get("series", []):
                identifier = s["seriesID"]
                if identifier not in specs:
                    continue
                indicator, definition, unit, sa = specs[identifier]
                values = {
                    f"{r['year']}-{r['period'][1:]}": r
                    for r in s.get("data", [])
                    if re.fullmatch(r"M(0[1-9]|1[0-2])", r["period"])
                }
                for period, row in values.items():
                    if period < self.start[:7] or period > self.end[:7]:
                        continue
                    notes = "; ".join(f.get("text", "") for f in row.get("footnotes", []))
                    common = {
                        "period": period,
                        "url": url,
                        "fetched": fetched,
                        "producer": "BLS",
                        "primary": True,
                        "notes": notes,
                    }
                    self.add(
                        "US",
                        indicator,
                        row["value"],
                        series=identifier,
                        definition=definition,
                        unit=unit,
                        sa=sa,
                        **common,
                    )
                    old = values.get(f"{int(period[:4]) - 1}{period[4:]}")
                    if identifier == "CUUR0000SA0" and old and Decimal(old["value"]):
                        yoy = (Decimal(row["value"]) / Decimal(old["value"]) - 1) * 100
                        self.add(
                            "US",
                            "cpi_yoy",
                            yoy,
                            series=identifier + ":YOY",
                            definition="CPI_U_ALL_ITEMS_YOY",
                            **{
                                **common,
                                "notes": notes + "; derived 100*(CPI[t]/CPI[t-12]-1), NSA",
                            },
                        )

    def nyfed(self):
        if "US" not in self.countries or not set(self.indicators) & {
            "effr",
            "sofr",
            "money_market",
        }:
            return
        url = "https://markets.newyorkfed.org/api/rates/all/search.json"
        data, at = self.json(url, {"startDate": self.start, "endDate": self.end})
        for row in data.get("refRates", []):
            kind = row["type"].lower()
            indicator = kind if kind in {"effr", "sofr"} else "money_market"
            common = {
                "period": row["effectiveDate"],
                "url": url,
                "fetched": at,
                "producer": "NYFED",
                "primary": True,
                "frequency": "D",
                "sa": "NA",
                "notes": "effectiveDate is observation, not publication; "
                + "revisionIndicator="
                + row.get("revisionIndicator", ""),
            }
            if "percentRate" in row:
                self.add(
                    "US",
                    indicator,
                    row["percentRate"],
                    series=kind.upper(),
                    definition=kind.upper(),
                    name=kind.upper(),
                    **common,
                )
            if kind == "sofrai":
                for key, days in [
                    ("average30day", "30D"),
                    ("average90day", "90D"),
                    ("average180day", "180D"),
                ]:
                    self.add(
                        "US",
                        "money_market",
                        row.get(key),
                        series="SOFR_AVG_" + days,
                        definition="SOFR_COMPOUNDED_AVERAGE_" + days,
                        name="SOFR average " + days,
                        **common,
                    )
                self.add(
                    "US",
                    "money_market",
                    row.get("index"),
                    series="SOFR_INDEX",
                    definition="SOFR_INDEX_20180402",
                    name="SOFR index",
                    unit="index_20180402_1",
                    **common,
                )

    def treasury(self):
        if "US" not in self.countries:
            return
        url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
        for indicator, dataset, prefix in [
            ("nominal_yield", "daily_treasury_yield_curve", "BC_"),
            ("real_yield", "daily_treasury_real_yield_curve", "TC_"),
        ]:
            if indicator not in self.indicators:
                continue
            for year in range(int(self.start[:4]), int(self.end[:4]) + 1):
                try:
                    text, at = self.request(url, {"data": dataset, "field_tdr_date_value": year})
                    root = ET.fromstring(text)
                    for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
                        fields = {e.tag.rsplit("}", 1)[-1]: e.text for e in entry.iter()}
                        period = (fields.get("NEW_DATE") or "")[:10]
                        if not self.start <= period <= self.end:
                            continue
                        # Atom updated is the entry revision, not original publication.
                        revised = fields.get("updated")
                        for field, value in fields.items():
                            suffix = field.removeprefix(prefix)
                            if not field.startswith(prefix) or not re.fullmatch(
                                r"\d+(?:_\d+)?(?:MONTH|YEAR)", suffix
                            ):
                                continue
                            maturity = (
                                suffix.replace("MONTH", "M").replace("YEAR", "Y").replace("_", ".")
                            )
                            self.add(
                                "US",
                                indicator,
                                value,
                                period,
                                series=field,
                                url=url,
                                fetched=at,
                                definition="US_TREASURY_PAR_"
                                + ("NOMINAL" if prefix == "BC_" else "REAL"),
                                frequency="D",
                                sa="NA",
                                producer="US_TREASURY",
                                primary=True,
                                maturity=maturity,
                                basis="nominal" if prefix == "BC_" else "real",
                                revised_at=revised,
                                notes="Atom updated is revision metadata; original release unknown",
                            )
                except (ProviderError, ET.ParseError) as exc:
                    self.result.errors.append(f"{dataset} {year}: {exc}")
        if self.result.errors:
            self.result.status = "PARTIAL_ERROR" if self.result.observations else "ERROR"

    def bis(self):
        if "policy_rate" not in self.indicators:
            return
        areas = ["XM" if c == "EA" else c for c in self.countries if c != "EA20" and c != "EA21"]
        if not areas:
            return
        freq = self.options.get("bis_frequency", "M")
        if freq not in {"D", "M"}:
            raise ValueError("BIS supports D/M")
        url = "https://stats.bis.org/api/v1/data/WS_CBPOL/" + freq + "." + "+".join(areas)
        text, at = self.request(
            url, {"startPeriod": self.start, "endPeriod": self.end, "format": "csv"}
        )
        for r in csv_rows(text):
            area = "EA" if r.get("REF_AREA") == "XM" else r.get("REF_AREA")
            # Keep the spliced main instrument/corridor-midpoint methodology
            # separate from named ECB deposit/refinancing rates and EFFR.
            definition = "BIS_MAIN_POLICY_INSTRUMENT:" + r.get("COMPILATION", "unspecified")
            self.add(
                area,
                "policy_rate",
                r.get("OBS_VALUE"),
                r["TIME_PERIOD"],
                series=f"WS_CBPOL.{freq}.{r['REF_AREA']}",
                url=url,
                fetched=at,
                definition=definition,
                frequency=r["FREQ"],
                sa="NA",
                producer=r.get("SOURCE_REF") or f"UNKNOWN:CB:{area}",
                notes=r.get("SUPP_INFO_BREAKS", "")
                + "; monthly = last business day; daily publication lag mid-week",
            )

    def eurostat(self):
        base = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
        for country in self.countries:
            if country not in EURO_AREAS:
                continue
            specs = [
                (
                    "hicp_yoy",
                    "prc_hicp_minr",
                    {"coicop18": "TOTAL", "unit": "RCH_A"},
                    "HICP_ALL_ITEMS_YOY_COICOP2018",
                    "NSA",
                ),
                (
                    "unemployment",
                    "une_rt_m",
                    {"sex": "T", "age": "TOTAL", "s_adj": "SA", "unit": "PC_ACT"},
                    "EU_LFS_HARMONISED_UNEMPLOYMENT_15_74",
                    "SA",
                ),
            ]
            for indicator, dataset, filters, definition, sa in specs:
                if indicator not in self.indicators:
                    continue
                url = base + dataset
                try:
                    data, at = self.json(
                        url,
                        {
                            "geo": country,
                            "lang": "en",
                            "sinceTimePeriod": self.start[:7],
                            **filters,
                        },
                    )
                    for dims, value in jsonstat_rows(data):
                        series = (
                            dataset
                            + ":"
                            + ":".join(f"{k}={v}" for k, v in dims.items() if k != "time")
                        )
                        status = data.get("status", {})
                        self.add(
                            country,
                            indicator,
                            value,
                            dims["time"],
                            series=series,
                            url=url,
                            fetched=at,
                            definition=definition,
                            sa=sa,
                            producer="Eurostat",
                            primary=True,
                            notes="National statistical institutes -> Eurostat harmonisation; dataset updated="
                            + str(data.get("updated", "unknown"))
                            + "; not an observation release date; flags="
                            + str(status),
                        )
                except (ProviderError, ValueError) as exc:
                    self.result.errors.append(f"{dataset}/{country}: {exc}")
        if self.result.errors:
            self.result.status = "PARTIAL_ERROR" if self.result.observations else "ERROR"

    def ecb(self):
        if "EA" not in self.countries:
            return
        base = "https://data-api.ecb.europa.eu/service/data/"
        specs = []
        if "policy_rate" in self.indicators:
            specs.append(("FM/D.U2.EUR.4F.KR.DFR+MLFR+MRR_FR.LEV", "policy_rate"))
        if "nominal_yield" in self.indicators:
            specs.append(("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "nominal_yield"))
        for key, indicator in specs:
            url = base + key
            text, at = self.request(
                url, {"startPeriod": self.start, "endPeriod": self.end, "format": "csvdata"}
            )
            for r in csv_rows(text):
                kind = r.get("PROVIDER_FM_ID", "")
                definition = (
                    "ECB_" + kind
                    if indicator == "policy_rate"
                    else "ECB_AAA_SVENSSON_CONTINUOUS_COMPOUND_SPOT"
                )
                self.add(
                    "EA",
                    indicator,
                    r.get("OBS_VALUE"),
                    r["TIME_PERIOD"],
                    series=r["KEY"],
                    url=url,
                    fetched=at,
                    name=r.get("TITLE", indicator),
                    definition=definition,
                    frequency="D",
                    sa="NA",
                    producer="ECB",
                    primary=True,
                    maturity="10Y" if indicator == "nominal_yield" else "",
                    basis="nominal" if indicator == "nominal_yield" else "",
                    notes="Euro area changing composition U2; observation frequency="
                    + r.get("FREQ", "")
                    + "; "
                    + r.get("TITLE_COMPL", ""),
                )

    def oecd(self):
        mapping = {**OECD_AREAS, **self.options.get("oecd_areas", {})}
        if not all(re.fullmatch(r"[A-Z0-9_]+", v) for v in mapping.values()):
            raise ValueError("Invalid OECD country code mapping")
        areas = {mapping.get(c, c): c for c in self.countries if c != "EA"}
        if not areas:
            return
        base = "https://sdmx.oecd.org/public/rest/v1/data/"
        joined = "+".join(areas)
        specs = []
        if set(self.indicators) & {"cpi_yoy", "hicp_yoy"}:
            # 2018 classification carries European 2026 HICP; some national
            # CPI series (including US) remain in the verified 1999 flow.
            for flow in ["DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL", "DSD_PRICES@DF_PRICES_ALL"]:
                specs.append(
                    ("OECD.SDD.TPS," + flow + ",1.0/" + joined + ".M..CPI.PA._T.N.GY", "prices")
                )
        if "unemployment" in self.indicators:
            specs.append(
                (
                    "OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/"
                    + joined
                    + ".UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M",
                    "unemployment",
                )
            )
        if set(self.indicators) & {"money_market", "nominal_yield", "policy_rate"}:
            specs.append(
                (
                    "OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/"
                    + joined
                    + ".M.IRLT+IRSTCI+IR3TIB......",
                    "financial",
                )
            )
        for key, family in specs:
            url = base + key
            try:
                text, at = self.request(
                    url,
                    {
                        "startPeriod": self.start[:7],
                        "endPeriod": self.end[:7],
                        "format": "csvfilewithlabels",
                    },
                )
                for r in csv_rows(text):
                    country = areas.get(r.get("REF_AREA"))
                    if country is None or r.get("FREQ") != "M":
                        continue
                    multiplier = Decimal(10) ** int(r.get("UNIT_MULT") or "0")
                    if not r.get("OBS_VALUE"):
                        continue
                    value = Decimal(r["OBS_VALUE"]) * multiplier
                    common = {
                        "period": r["TIME_PERIOD"],
                        "series": key.split("/")[0]
                        + ":"
                        + ":".join(
                            f"{k}={r[k]}"
                            for k in ["REF_AREA", "FREQ", "MEASURE", "ADJUSTMENT"]
                            if k in r
                        ),
                        "url": url,
                        "fetched": at,
                        "notes": "OECD dissemination; source lineage unknown unless explicitly mapped",
                    }
                    if (
                        family == "prices"
                        and r.get("UNIT_MEASURE") == "PA"
                        and r.get("TRANSFORMATION") == "GY"
                        and r.get("ADJUSTMENT") == "N"
                    ):
                        hicp = r.get("METHODOLOGY") == "HICP"
                        if r.get("METHODOLOGY") not in {"HICP", "N"}:
                            continue
                        common["series"] += ":" + r["METHODOLOGY"]
                        self.add(
                            country,
                            "hicp_yoy" if hicp else "cpi_yoy",
                            value,
                            definition="HICP_ALL_ITEMS_YOY_COICOP2018"
                            if hicp
                            else "CPI_U_ALL_ITEMS_YOY"
                            if country == "US"
                            else "CPI_NATIONAL_ALL_ITEMS_YOY:" + country,
                            producer="Eurostat" if hicp else "BLS" if country == "US" else None,
                            **common,
                        )
                    elif family == "unemployment":
                        self.add(
                            country,
                            "unemployment",
                            value,
                            definition="OECD_HARMONISED_UNEMPLOYMENT_AGE15PLUS",
                            sa="SA_CALENDAR",
                            **common,
                        )
                    elif family == "financial" and r.get("UNIT_MEASURE") == "PA":
                        measure = r.get("MEASURE", "")
                        indicator = "nominal_yield" if measure == "IRLT" else "money_market"
                        self.add(
                            country,
                            indicator,
                            value,
                            definition="OECD_FINMARK_MONTHLY_AVERAGE:" + measure,
                            sa="NA",
                            maturity="10Y"
                            if measure == "IRLT"
                            else "3M"
                            if measure == "IR3TIB"
                            else "",
                            basis="nominal" if measure == "IRLT" else "",
                            **common,
                        )
            except (ProviderError, ValueError) as exc:
                self.result.errors.append(f"{family}: {exc}")
        if self.result.errors:
            self.result.status = "PARTIAL_ERROR" if self.result.observations else "ERROR"

    def worldbank(self):
        specs = {
            "FP.CPI.TOTL.ZG": ("cpi_yoy", "WB_CPI_ANNUAL_AVERAGE_CHANGE"),
            "SL.UEM.TOTL.ZS": ("unemployment", "ILO_MODELLED_UNEMPLOYMENT_ANNUAL"),
            "FR.INR.LEND": ("lending_rate", "IMF_LENDING_RATE_ANNUAL"),
        }
        identifiers = [s for s, (i, _) in specs.items() if i in self.indicators]
        countries = [c for c in self.countries if c not in {"EA", "EA20", "EA21", "EU27_2020"}]
        if not identifiers or not countries:
            return
        url = (
            "https://api.worldbank.org/v2/country/"
            + ";".join(countries)
            + "/indicator/"
            + ";".join(identifiers)
        )
        metadata = {}
        for s in identifiers:
            m, _at = self.json("https://api.worldbank.org/v2/indicator/" + s, {"format": "json"})
            metadata[s] = m[1][0]
        params = {
            "format": "json",
            "source": 2,
            "date": f"{int(self.start[:4]) - 2}:{self.end[:4]}",
            "per_page": 1000,
        }
        page = 1
        while True:
            data, at = self.json(url, {**params, "page": page})
            if (
                not isinstance(data, list)
                or not isinstance(data[0], dict)
                or "pages" not in data[0]
            ):
                raise ProviderError("WORLDBANK_ERROR_RESPONSE")
            for r in data[1] or []:
                s = r["indicator"]["id"]
                indicator, definition = specs[s]
                meta = metadata[s]
                self.add(
                    r["country"]["id"],
                    indicator,
                    r.get("value"),
                    r["date"],
                    series=s,
                    url=url,
                    fetched=at,
                    name=meta["name"],
                    definition=definition,
                    frequency="A",
                    sa="NA",
                    producer=meta.get("sourceOrganization") or "UNKNOWN:WorldBank",
                    notes=meta.get("sourceNote", "")
                    + "; annual data, variable publication lag; dataset lastupdated="
                    + str(data[0].get("lastupdated", "unknown"))
                    + " (not observation release/revision)",
                )
            if page >= int(data[0]["pages"]):
                break
            if page >= 100:
                raise ProviderError("WORLDBANK_PAGINATION_LIMIT")
            page += 1

    def fred(self):
        if "US" not in self.countries:
            return
        specs = {
            "CPIAUCNS": (
                "cpi_index",
                "CPI_U_ALL_URBAN_1982_84",
                "index_1982_84_100",
                "M",
                "NSA",
                "BLS",
                "",
                "",
            ),
            "UNRATE": ("unemployment", "US_CPS_U3_AGE16PLUS", "percent", "M", "SA", "BLS", "", ""),
            "PAYEMS": (
                "employment",
                "US_CES_TOTAL_NONFARM_PAYROLLS",
                "thousand_persons",
                "M",
                "SA",
                "BLS",
                "",
                "",
            ),
            "EFFR": ("effr", "EFFR", "percent", "D", "NA", "NYFED", "", ""),
            "SOFR": ("sofr", "SOFR", "percent", "D", "NA", "NYFED", "", ""),
            "DGS10": (
                "nominal_yield",
                "US_TREASURY_PAR_NOMINAL",
                "percent",
                "D",
                "NA",
                "US_TREASURY",
                "10Y",
                "nominal",
            ),
            "DFII10": (
                "real_yield",
                "US_TREASURY_PAR_REAL",
                "percent",
                "D",
                "NA",
                "US_TREASURY",
                "10Y",
                "real",
            ),
        }
        base = "https://api.stlouisfed.org/fred/series/observations"
        for s, (i, definition, unit, freq, sa, producer, maturity, basis) in specs.items():
            if i not in self.indicators:
                continue
            data, at = self.json(
                base,
                {
                    "series_id": s,
                    "api_key": os.environ["FRED_API_KEY"],
                    "file_type": "json",
                    "observation_start": self.start,
                    "observation_end": self.end,
                    "realtime_start": self.end,
                    "realtime_end": self.end,
                    "limit": 100000,
                },
            )
            for r in data.get("observations", []):
                period = r["date"][:7] if freq == "M" else r["date"]
                self.add(
                    "US",
                    i,
                    r["value"],
                    period,
                    series=s,
                    url="https://fred.stlouisfed.org/series/" + s,
                    fetched=at,
                    definition=definition,
                    unit=unit,
                    frequency=freq,
                    sa=sa,
                    producer=producer,
                    maturity=maturity,
                    basis=basis,
                    vintage_as_of=self.end,
                    notes="ALFRED realtime window pinned; realtime_start is vintage validity, not release date",
                )


def jsonstat_rows(data):
    """JSON-stat is row-major; support sparse and dense values without guessing indexes."""
    dimensions = []
    for name in data["id"]:
        index = data["dimension"][name]["category"]["index"]
        dimensions.append(index if isinstance(index, list) else sorted(index, key=index.get))
    values = data.get("value", {})
    for number, coords in enumerate(itertools.product(*dimensions)):
        value = (
            values[number]
            if isinstance(values, list) and number < len(values)
            else values.get(str(number))
            if isinstance(values, dict)
            else None
        )
        if value is not None:
            yield dict(zip(data["id"], coords, strict=True)), value
