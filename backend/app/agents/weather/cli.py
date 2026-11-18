"""Query the weather layer WITHOUT an LLM.

    uv run python -m app.agents.weather.cli forecast --hours 8
    uv run python -m app.agents.weather.cli current
(Trip advice needs a planned journey: see scripts/commute_advice.py.)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.agents.weather.client import WeatherClient
from app.agents.weather.models import CurrentResult, ForecastResult
from app.agents.weather.service import WeatherService
from app.config import Settings, get_settings
from app.core.memory import load_profile


def build_service(settings: Settings) -> tuple[WeatherService, WeatherClient]:
    client = WeatherClient(
        ttl_s=settings.weather_ttl_seconds, max_stale_s=settings.weather_max_stale_seconds
    )
    return WeatherService(client, load_profile(settings.profile_path)), client


def render(res: ForecastResult | CurrentResult) -> str:
    w = res.weather
    head = (f"live, fetched {w.data_age_s}s ago" if w.source == "live" else "UNAVAILABLE") + (
        f" - {w.note}" if w.note else ""
    )
    lines = [f"({res.latitude:.3f}, {res.longitude:.3f}) {head}"]
    if isinstance(res, CurrentResult):
        lines.append(
            f"{res.as_of}: {res.conditions}, {res.temperature_c}°C (feels {res.feels_like_c}°C), "
            f"wind {res.wind_kmh} km/h (gusts {res.gust_kmh}), humidity {res.humidity_pct}%, "
            f"rain {res.rain_mm} mm"
        )
    else:
        lines.append("time(end of hr)  temp feels rain% rainmm wind gust  UV  conditions")
        for h in res.hours:
            lines.append(
                f"{h.time:<16}{h.temperature_c!s:>5}{h.feels_like_c!s:>6}{h.rain_chance_pct!s:>6}"
                f"{h.rain_mm!s:>7}{h.wind_kmh!s:>5}{h.gust_kmh!s:>5}{h.uv_index!s:>5}  {h.conditions}"
            )
        if res.note:
            lines.append(f"note: {res.note}")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    svc, client = build_service(get_settings())
    try:
        if args.cmd == "forecast":
            res: ForecastResult | CurrentResult = await svc.get_forecast(args.place, args.hours)
        else:
            res = await svc.get_current(args.place)
        print(res.model_dump_json(indent=2) if args.json else render(res))
        return 0
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("forecast", "current"):
        p = sub.add_parser(name)
        p.add_argument("--place", help="'home' (default) or 'lat,lon'")
        if name == "forecast":
            p.add_argument("--hours", type=int, default=12)
    try:
        return asyncio.run(run(ap.parse_args(argv)))
    except Exception as exc:  # CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
