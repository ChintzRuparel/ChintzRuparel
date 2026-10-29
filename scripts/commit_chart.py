#!/usr/bin/env python3
"""
commit_chart.py - quarter-by-quarter, year-over-year commit chart for a GitHub profile README.

Asks the GitHub GraphQL API for commit contributions in each calendar quarter,
then writes two SVGs (dark and light) that a README swaps between with <picture>.

    GH_TOKEN=ghp_xxx python scripts/commit_chart.py --user ChintzRuparel
    python scripts/commit_chart.py --user ChintzRuparel --sample   # fake data, no token

No dependencies beyond the standard library.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

API_URL = "https://api.github.com/graphql"
UTC = dt.timezone.utc
MONTHS = ("Jan to Mar", "Apr to Jun", "Jul to Sep", "Oct to Dec")

# Colours follow GitHub's own UI so the card sits naturally on a profile page.
THEMES = {
    "dark": {
        "bg": "#0d1117", "border": "#30363d", "text": "#e6edf3", "muted": "#7d8590",
        "grid": "#21262d", "ramp": ("#265c38", "#3fb950"), "up": "#3fb950", "down": "#f85149",
    },
    "light": {
        "bg": "#ffffff", "border": "#d0d7de", "text": "#1f2328", "muted": "#656d76",
        "grid": "#eaeef2", "ramp": ("#aee5ba", "#1a7f37"), "up": "#1a7f37", "down": "#cf222e",
    },
}


@dataclass
class ChartData:
    login: str
    years: list[int]
    counts: dict[int, list[int | None]]   # None = quarter hasn't started yet
    current: tuple[int, int]              # (year, quarter) in progress
    ytd_prev: int | None                  # last year's commits up to this date
    generated: dt.datetime


# --------------------------------------------------------------------------- data

def quarter_window(year: int, q: int) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(year, 3 * q - 2, 1, tzinfo=UTC)
    nxt = dt.datetime(year + 1, 1, 1, tzinfo=UTC) if q == 4 else dt.datetime(year, 3 * q + 1, 1, tzinfo=UTC)
    return start, nxt - dt.timedelta(seconds=1)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def one_year_before(t: dt.datetime) -> dt.datetime:
    try:
        return t.replace(year=t.year - 1)
    except ValueError:  # 29 Feb
        return t.replace(year=t.year - 1, day=28)


def gql(query: str, variables: dict, token: str) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "commit-chart",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as err:
        sys.exit(f"GitHub API returned {err.code}: {err.read().decode(errors='replace')[:300]}")
    if payload.get("errors"):
        sys.exit(f"GraphQL error: {payload['errors'][0].get('message')}")
    return payload["data"]


def fetch_data(login: str, token: str, n_years: int, now: dt.datetime) -> ChartData:
    user = gql("query($login: String!) { user(login: $login) { login createdAt } }",
               {"login": login}, token)["user"]
    if not user:
        sys.exit(f"No GitHub user called {login!r}.")
    first_year = int(user["createdAt"][:4])
    years = [y for y in range(now.year - n_years + 1, now.year + 1) if y >= first_year]

    counts: dict[int, list[int | None]] = {}
    ytd_prev = None
    for year in years:
        # One request per year; each quarter is an aliased contributionsCollection.
        fields = []
        for q in range(1, 5):
            start, end = quarter_window(year, q)
            if start > now:
                continue
            fields.append(f'q{q}: contributionsCollection(from: "{iso(start)}", to: "{iso(min(end, now))}") '
                          "{ totalCommitContributions }")
        if year == now.year - 1:
            fields.append(f'ytd: contributionsCollection(from: "{iso(dt.datetime(year, 1, 1, tzinfo=UTC))}", '
                          f'to: "{iso(one_year_before(now))}") {{ totalCommitContributions }}')
        query = "query($login: String!) { user(login: $login) { " + " ".join(fields) + " } }"
        res = gql(query, {"login": login}, token)["user"]
        counts[year] = [res[f"q{q}"]["totalCommitContributions"] if f"q{q}" in res else None
                        for q in range(1, 5)]
        if "ytd" in res:
            ytd_prev = res["ytd"]["totalCommitContributions"]

    return ChartData(user["login"], years, counts, (now.year, (now.month - 1) // 3 + 1), ytd_prev, now)


def sample_data(login: str, n_years: int, now: dt.datetime) -> ChartData:
    """Plausible fake numbers so the design can be previewed without a token."""
    rng = random.Random(login)
    years = list(range(now.year - n_years + 1, now.year + 1))
    cq = (now.month - 1) // 3 + 1
    q_start, q_end = quarter_window(now.year, cq)
    frac = (now - q_start) / (q_end - q_start)
    counts = {}
    for i, year in enumerate(years):
        row = []
        for q in range(1, 5):
            if quarter_window(year, q)[0] > now:
                row.append(None)
                continue
            v = int(55 * 1.45 ** i * rng.uniform(0.55, 1.45))
            row.append(int(v * frac) if (year, q) == (now.year, cq) else v)
        counts[year] = row
    prev = counts.get(now.year - 1)
    ytd_prev = int(sum(prev[: cq - 1]) + prev[cq - 1] * frac) if prev else None
    return ChartData(login, years, counts, (now.year, cq), ytd_prev, now)


# --------------------------------------------------------------------------- drawing

W, H = 800, 410
PAD = 28
CHART_L, CHART_R = 64, W - PAD
CHART_T, CHART_B = 118, 290


def mix(a: str, b: str, t: float) -> str:
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def short(n: float) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(int(n))


def nice_step(max_val: float, divisions: int = 4) -> int:
    raw = max(max_val / divisions, 1)
    exp = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * exp
        if step >= raw and float(step).is_integer():
            return int(step)
    return int(10 * exp)


def change(cur: int, prev: int | None) -> float | None:
    return None if not prev else (cur - prev) / prev * 100


def change_tspan(pct: float | None, t: dict) -> str:
    if pct is None:
        return ""
    arrow, colour = ("▲", t["up"]) if pct >= 0 else ("▼", t["down"])
    return f'<tspan dx="8" style="fill:{colour}">{arrow} {abs(pct):.0f}%</tspan>'


def render(d: ChartData, theme: str) -> str:
    t = THEMES[theme]
    n = len(d.years)
    colours = {y: mix(*t["ramp"], i / (n - 1) if n > 1 else 1) for i, y in enumerate(d.years)}
    values = [v for row in d.counts.values() for v in row if v is not None]
    step = nice_step(max(values, default=0))
    top = step * 4
    cur_year, cur_q = d.current
    login = escape(d.login)

    def y_at(v: float) -> float:
        return CHART_B - (CHART_B - CHART_T) * v / top

    out = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc">
<title id="title">Commits per quarter for {login}</title>
<desc id="desc">Commit contributions grouped by calendar quarter, one bar per year, {d.years[0]} to {d.years[-1]}.</desc>
<style>
  text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif; fill: {t["text"]}; font-variant-numeric: tabular-nums; }}
  .muted {{ fill: {t["muted"]}; }}
  .bar {{ transform-box: fill-box; transform-origin: 50% 100%; animation: grow .8s cubic-bezier(.2,.8,.2,1) both; }}
  .val {{ animation: fade .4s ease-out both; }}
  @keyframes grow {{ from {{ transform: scaleY(0); }} to {{ transform: scaleY(1); }} }}
  @keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @media (prefers-reduced-motion: reduce) {{ .bar, .val {{ animation: none; }} }}
</style>
<defs>
  <pattern id="wip-{theme}" patternUnits="userSpaceOnUse" width="5" height="5" patternTransform="rotate(45)">
    <rect width="2.5" height="5" fill="{colours[cur_year] if cur_year in colours else t["ramp"][1]}"/>
  </pattern>
</defs>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="10" fill="{t["bg"]}" stroke="{t["border"]}"/>''']

    # Header: title on the left, this year's running total on the right.
    out.append(f'<text x="{PAD}" y="46" font-size="19" font-weight="600">Commits, quarter by quarter</text>')
    out.append(f'<text x="{PAD}" y="68" font-size="12" class="muted">'
               f'Each quarter compared across {n} years for @{login}</text>')
    if cur_year in d.counts:
        ytd = sum(v for v in d.counts[cur_year] if v)
        out.append(f'<text x="{W - PAD}" y="50" font-size="28" font-weight="600" text-anchor="end">{ytd:,}</text>')
        out.append(f'<text x="{W - PAD}" y="69" font-size="11" class="muted" text-anchor="end">'
                   f'commits in {cur_year} so far</text>')
        pct = change(ytd, d.ytd_prev)
        if pct is not None:
            arrow, colour = ("▲", t["up"]) if pct >= 0 else ("▼", t["down"])
            out.append(f'<text x="{W - PAD}" y="85" font-size="11" text-anchor="end" style="fill:{colour}">'
                       f'{arrow} {abs(pct):.0f}% on this time last year</text>')

    # Grid and y-axis.
    for i in range(5):
        y = y_at(step * i)
        stroke = t["border"] if i == 0 else t["grid"]
        out.append(f'<line x1="{CHART_L}" x2="{CHART_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{stroke}"/>')
        out.append(f'<text x="{CHART_L - 10}" y="{y + 3.5:.1f}" font-size="10" class="muted" '
                   f'text-anchor="end">{short(step * i)}</text>')

    # Bars, grouped by quarter.
    group_w = (CHART_R - CHART_L) / 4
    gap = 4 if n <= 6 else 2
    bar_w = min(28.0, (group_w * 0.74 - gap * (n - 1)) / n)
    cluster_w = bar_w * n + gap * (n - 1)
    for g in range(4):
        gx = CHART_L + g * group_w
        cx = gx + group_w / 2
        out.append(f'<text x="{cx:.1f}" y="{CHART_B + 22}" font-size="12" font-weight="600" '
                   f'text-anchor="middle">Q{g + 1}</text>')
        out.append(f'<text x="{cx:.1f}" y="{CHART_B + 37}" font-size="10" class="muted" '
                   f'text-anchor="middle">{MONTHS[g]}</text>')
        x0 = gx + (group_w - cluster_w) / 2
        for i, year in enumerate(d.years):
            v = d.counts[year][g]
            if v is None:
                continue
            x = x0 + i * (bar_w + gap)
            h = max(CHART_B - y_at(v), 2.0)
            wip = (year, g + 1) == (cur_year, cur_q)
            delay = 0.1 + (g * n + i) * 0.035
            paint = (f'fill="url(#wip-{theme})" stroke="{colours[year]}" stroke-width="1.2"'
                     if wip else f'fill="{colours[year]}"')
            note = " so far" if wip else ""
            out.append(f'<rect class="bar" x="{x:.1f}" y="{CHART_B - h:.1f}" width="{bar_w:.1f}" '
                       f'height="{h:.1f}" rx="{min(3, bar_w / 4):.1f}" {paint} '
                       f'style="animation-delay:{delay:.2f}s"><title>{year} Q{g + 1}: {v:,} commits{note}</title></rect>')
            if bar_w >= 14:
                latest = year == d.years[-1]
                cls = "val" if latest else "val muted"
                weight = ' font-weight="600"' if latest else ""
                out.append(f'<text class="{cls}" x="{x + bar_w / 2:.1f}" y="{CHART_B - h - 5:.1f}" '
                           f'font-size="10"{weight} text-anchor="middle" '
                           f'style="animation-delay:{delay + 0.6:.2f}s">{short(v)}</text>')

    # Legend: one entry per year with its total and change on the year before.
    item_w = (W - 2 * PAD) / n
    ly = 352
    for i, year in enumerate(d.years):
        x = PAD + i * item_w
        total = sum(v for v in d.counts[year] if v)
        if year == cur_year:
            pct = change(total, d.ytd_prev)
            label = f"{total:,} so far"
        else:
            prev = d.counts.get(year - 1)
            pct = change(total, sum(v for v in prev if v)) if prev else None
            label = f"{total:,} commits"
        out.append(f'<rect x="{x:.1f}" y="{ly - 9}" width="10" height="10" rx="2" fill="{colours[year]}"/>')
        out.append(f'<text x="{x + 16:.1f}" y="{ly}" font-size="12" font-weight="600">{year}</text>')
        out.append(f'<text x="{x:.1f}" y="{ly + 18}" font-size="11"><tspan class="muted">{label}</tspan>'
                   f'{change_tspan(pct, t)}</text>')

    out.append(f'<text x="{PAD}" y="{H - 16}" font-size="10" class="muted">'
               f'Striped bar is the quarter still in progress</text>')
    out.append(f'<text x="{W - PAD}" y="{H - 16}" font-size="10" class="muted" text-anchor="end">'
               f'Updated {d.generated:%d %b %Y}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- main

def main() -> None:
    p = argparse.ArgumentParser(description="Render a quarterly year-over-year commit chart.")
    p.add_argument("--user", default=os.environ.get("GITHUB_REPOSITORY_OWNER"), help="GitHub login")
    p.add_argument("--years", type=int, default=5, help="How many years to show (1 to 8)")
    p.add_argument("--out-dir", default="assets", help="Where to write the SVGs")
    p.add_argument("--sample", action="store_true", help="Use fake data instead of the API")
    args = p.parse_args()

    if not args.user:
        sys.exit("Pass --user <github-login>.")
    n_years = max(1, min(8, args.years))
    now = dt.datetime.now(UTC)

    if args.sample:
        data = sample_data(args.user, n_years, now)
    else:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("Set GH_TOKEN (or GITHUB_TOKEN), or use --sample.")
        data = fetch_data(args.user, token, n_years, now)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        path = out_dir / f"commit-chart-{theme}.svg"
        path.write_text(render(data, theme), encoding="utf-8")
        print(f"wrote {path}")
    for year in data.years:
        print(year, data.counts[year])


if __name__ == "__main__":
    main()
