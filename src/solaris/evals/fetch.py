"""
Reference-data fetchers. The only code in the project that makes outbound HTTP.

Everything is cached to ``evals/references/`` and committed, so the harness runs
offline and in CI without hammering a free public service. Refresh is a
deliberate, explicit step::

    python -m solaris.evals.fetch --years 2020 2021 2022
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from solaris.evals.references import (
    CITIES,
    CITY_BY_KEY,
    CLIMATOLOGY_DAY_OF_MONTH,
    FILL_VALUE,
    NASA_POWER_HOURLY_PARAMS,
    NASA_POWER_HOURLY_URL,
    NASA_POWER_PARAMS,
    NASA_POWER_PRECIP_PARAM,
    NASA_POWER_TIME_STANDARD,
    NASA_POWER_URL,
    DailySeries,
    HourlySeries,
    PrecipSeries,
    load_cached,
    load_cached_hourly,
    load_cached_precip,
    save_cached,
    save_cached_hourly,
    save_cached_precip,
)

#: Be a good citizen of a free service.
REQUEST_DELAY_S = 1.0
TIMEOUT_S = 90


def _get_json(url: str) -> dict:
    import json

    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_nasa_power(city_key: str, year: int) -> DailySeries:
    """
    Fetch one city-year of daily NASA POWER irradiance.

    Days carrying the -999 fill value are dropped rather than treated as zero,
    which would silently drag an annual total down.
    """
    city = CITY_BY_KEY[city_key]
    query = urllib.parse.urlencode(
        {
            "parameters": NASA_POWER_PARAMS,
            "community": "RE",
            "latitude": city.lat,
            "longitude": city.lon,
            "start": f"{year}0101",
            "end": f"{year}1231",
            "format": "JSON",
        }
    )
    url = f"{NASA_POWER_URL}?{query}"
    payload = _get_json(url)
    parameters = payload["properties"]["parameter"]

    def clean(name: str) -> dict[str, float]:
        raw = parameters.get(name, {})
        return {day: float(value) for day, value in raw.items() if float(value) != FILL_VALUE}

    series = DailySeries(
        city=city_key,
        year=year,
        ghi=clean("ALLSKY_SFC_SW_DWN"),
        diffuse=clean("ALLSKY_SFC_SW_DIFF"),
        dni=clean("ALLSKY_SFC_SW_DNI"),
    )
    save_cached(
        series,
        {
            "source": "NASA POWER daily point API",
            "url": url,
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "units": "kWh/m2/day",
            "n_valid_days": series.n_valid,
        },
    )
    return series


def fetch_nasa_power_precip(city_key: str, year: int) -> PrecipSeries:
    """
    Fetch one city-year of daily precipitation.

    Same endpoint and same fill-value handling as the irradiance fetch, but a
    separate cache file: the irradiance references were already committed, and
    re-fetching twenty city-years to add one parameter would change files whose
    contents are cited in the validation report.
    """
    city = CITY_BY_KEY[city_key]
    query = urllib.parse.urlencode(
        {
            "parameters": NASA_POWER_PRECIP_PARAM,
            "community": "RE",
            "latitude": city.lat,
            "longitude": city.lon,
            "start": f"{year}0101",
            "end": f"{year}1231",
            "format": "JSON",
        }
    )
    url = f"{NASA_POWER_URL}?{query}"
    payload = _get_json(url)
    raw = payload["properties"]["parameter"].get(NASA_POWER_PRECIP_PARAM, {})
    # A dropped fill value is a missing day, not a dry day. Treating -999 as
    # zero would invent a dry spell and make the soiling model predict a
    # cleaning that never happened.
    precip = {day: float(v) for day, v in raw.items() if float(v) != FILL_VALUE}

    series = PrecipSeries(city=city_key, year=year, precip_mm=precip)
    save_cached_precip(
        series,
        {
            "source": "NASA POWER daily point API",
            "url": url,
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "units": "mm/day",
            "n_valid_days": series.n_valid,
            "annual_mm": round(series.annual_mm, 1),
            "rain_days_ge_1mm": series.rain_days(),
        },
    )
    return series


def fetch_nasa_power_hourly(city_key: str, year: int) -> HourlySeries:
    """
    Fetch a monthly-diurnal climatology: the 15th of each month, hourly, UTC.

    Twelve small requests rather than one year-long one. 288 records is all the
    physics chain needs, and a full 8760-hour year per city would be roughly a
    megabyte of committed JSON each for no gain.

    ``time-standard=UTC`` is passed explicitly because the endpoint defaults to
    local solar time -- see NASA_POWER_TIME_STANDARD for why that matters.
    """
    city = CITY_BY_KEY[city_key]
    series = HourlySeries(city=city_key, year=year)

    for month in range(1, 13):
        day = f"{year}{month:02d}{CLIMATOLOGY_DAY_OF_MONTH:02d}"
        query = urllib.parse.urlencode(
            {
                "parameters": NASA_POWER_HOURLY_PARAMS,
                "community": "RE",
                "latitude": city.lat,
                "longitude": city.lon,
                "start": day,
                "end": day,
                "time-standard": NASA_POWER_TIME_STANDARD,
                "format": "JSON",
            }
        )
        payload = _get_json(f"{NASA_POWER_HOURLY_URL}?{query}")
        parameters = payload["properties"]["parameter"]

        def take(name: str, target: dict, params=parameters) -> None:
            for stamp, value in params.get(name, {}).items():
                if float(value) != FILL_VALUE:
                    target[stamp] = float(value)

        take("ALLSKY_SFC_SW_DWN", series.ghi)
        take("ALLSKY_SFC_SW_DIFF", series.diffuse)
        take("ALLSKY_SFC_SW_DNI", series.dni)
        take("T2M", series.air_temp_c)
        take("WS2M", series.wind_m_s)
        time.sleep(REQUEST_DELAY_S)

    save_cached_hourly(
        series,
        {
            "source": "NASA POWER hourly point API",
            "time_standard": NASA_POWER_TIME_STANDARD,
            "days": f"15th of each month, {year}",
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "units": {
                "irradiance": "W/m2 (reported as Wh/m2 per hour)",
                "T2M": "degC",
                "WS2M": "m/s at 2 m",
            },
            "n_steps": series.n_steps,
        },
    )
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=[2020, 2021, 2022],
        help="calendar years to fetch",
    )
    parser.add_argument("--cities", nargs="*", default=None, help="city keys (default: all)")
    parser.add_argument("--force", action="store_true", help="refetch even if already cached")
    parser.add_argument(
        "--hourly",
        action="store_true",
        help="fetch the monthly-diurnal hourly climatology for the pvlib engine",
    )
    parser.add_argument(
        "--precip",
        action="store_true",
        help="fetch daily precipitation for the soiling model",
    )
    args = parser.parse_args(argv)

    if args.hourly and args.precip:
        parser.error("--hourly and --precip write different caches; run them separately")

    kind = "hourly" if args.hourly else "precip" if args.precip else "daily"
    loaders = {
        "daily": load_cached,
        "hourly": load_cached_hourly,
        "precip": load_cached_precip,
    }
    fetchers = {
        "daily": fetch_nasa_power,
        "hourly": fetch_nasa_power_hourly,
        "precip": fetch_nasa_power_precip,
    }

    keys = args.cities or [c.key for c in CITIES]
    fetched = skipped = failed = 0

    for key in keys:
        for year in args.years:
            if not args.force and loaders[kind](key, year) is not None:
                skipped += 1
                continue
            try:
                series = fetchers[kind](key, year)
            except (urllib.error.URLError, KeyError, ValueError) as exc:
                print(f"[fail] {key} {year}: {type(exc).__name__}: {exc}")
                failed += 1
                continue
            if kind == "hourly":
                print(
                    f"[ok]   {key:<11} {year}  {series.n_steps:>3} steps  "
                    f"annual GHI {series.annual_ghi_kwh_m2():>7.1f} kWh/m2  "
                    f"daytime air {series.mean_daytime_air_temp_c():>5.1f} C"
                )
            elif kind == "precip":
                print(
                    f"[ok]   {key:<11} {year}  {series.n_valid:>3} days  "
                    f"{series.annual_mm:>7.1f} mm  "
                    f"{series.rain_days():>3} cleaning-rain days"
                )
            else:
                print(
                    f"[ok]   {key:<11} {year}  {series.n_valid:>3} days  "
                    f"annual GHI {series.annual_ghi_kwh_m2():>7.1f} kWh/m2"
                )
            fetched += 1
            time.sleep(REQUEST_DELAY_S)

    print(f"\nfetched {fetched}, cached-already {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
