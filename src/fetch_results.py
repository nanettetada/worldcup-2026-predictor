"""Pull actual World Cup 2026 results into data/wc2026.sqlite.

Two data sources, tried in order:

1. football-data.org v4 — World Cup competition (code 'WC').
   Set FOOTBALL_DATA_TOKEN in the environment for a higher rate limit;
   without a token the public competitions on the free tier still work.

2. data/manual_results.json — a hand-edited fallback so the dashboard
   can show evaluation even when the API is unreachable or rate-limited.
   Format: list of {match_date, home_team, away_team, home_score,
   away_score, status, home_pens?, away_pens?}.

The fetcher canonicalises team names against wc2026_fixtures, then writes
into actual_results. Idempotent — re-running just overwrites the row for
each finished match.

Run from the project root:
    python src/fetch_results.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "wc2026.sqlite"
MANUAL = ROOT / "data" / "manual_results.json"

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"

# API name -> our fixture name. Add entries as they come up.
TEAM_ALIASES = {
    "South Korea": "Korea Republic",
    "Korea, Republic of": "Korea Republic",
    "Republic of Korea": "Korea Republic",
    "Czech Republic": "Czechia",
    "Turkey": "Turkiye",
    "Türkiye": "Turkiye",
    "Ivory Coast": "Cote d'Ivoire",
    "Côte d'Ivoire": "Cote d'Ivoire",
    "Curaçao": "Curacao",
    "USA": "United States",
    "United States of America": "United States",
    "Republic of Ireland": "Ireland",
    "Bosnia": "Bosnia and Herzegovina",
    "DR Congo": "Congo DR",
    "Cape Verde": "Cabo Verde",
}

# football-data.org stage codes -> our stage codes
STAGE_MAP = {
    "GROUP_STAGE": "GROUP",
    "PLAYOFFS_QF": None,        # not used in 48-team format
    "LAST_32": "R32",
    "ROUND_OF_32": "R32",
    "LAST_16": "R16",
    "ROUND_OF_16": "R16",
    "QUARTER_FINALS": "QF",
    "QUARTER_FINAL": "QF",
    "SEMI_FINALS": "SF",
    "SEMI_FINAL": "SF",
    "THIRD_PLACE": "3RD",
    "3RD_PLACE": "3RD",
    "FINAL": "FINAL",
}


def canon(name: str | None) -> str | None:
    if name is None:
        return None
    name = name.strip()
    return TEAM_ALIASES.get(name, name)


def load_fixture_lookup(conn: sqlite3.Connection) -> dict:
    """Map (stage, home_team, away_team) -> fixture_id."""
    rows = conn.execute(
        "SELECT fixture_id, stage, home_team, away_team "
        "FROM wc2026_fixtures "
        "WHERE home_team IS NOT NULL AND away_team IS NOT NULL"
    ).fetchall()
    lookup = {}
    for fid, stage, h, a in rows:
        lookup[(stage, h, a)] = fid
        # Allow reversed home/away (FIFA sometimes designates either side)
        lookup.setdefault((stage, a, h), fid)
    return lookup


def fetch_api() -> list[dict]:
    """Hit football-data.org. Returns the raw matches list, or [] on failure."""
    req = urllib.request.Request(API_URL)
    token = os.environ.get("FOOTBALL_DATA_TOKEN")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return data.get("matches", [])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  API unavailable: {e}")
        return []


def normalise_api_match(m: dict) -> dict | None:
    """Reshape one football-data.org match into our row shape."""
    stage = STAGE_MAP.get(m.get("stage"), None)
    if stage is None:
        return None
    home = canon((m.get("homeTeam") or {}).get("name"))
    away = canon((m.get("awayTeam") or {}).get("name"))
    if not home or not away:
        return None
    score = m.get("score") or {}
    ft = score.get("fullTime") or {}
    et = score.get("extraTime") or {}
    pens = score.get("penalties") or {}
    status = m.get("status", "SCHEDULED")
    utc_date = m.get("utcDate")  # e.g. '2026-06-11T17:00:00Z'
    return {
        "stage": stage,
        "home_team": home,
        "away_team": away,
        "kickoff_utc": utc_date,
        "match_date": utc_date[:10] if utc_date else None,
        "home_score": ft.get("home"),
        "away_score": ft.get("away"),
        "home_score_et": et.get("home"),
        "away_score_et": et.get("away"),
        "home_pens": pens.get("home"),
        "away_pens": pens.get("away"),
        "status": status,
    }


def load_manual() -> list[dict]:
    if not MANUAL.exists():
        return []
    try:
        return json.loads(MANUAL.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  manual_results.json invalid JSON: {e}")
        return []


def normalise_manual(m: dict) -> dict | None:
    home = canon(m.get("home_team"))
    away = canon(m.get("away_team"))
    if not home or not away:
        return None
    return {
        "stage": m.get("stage", "GROUP"),
        "home_team": home,
        "away_team": away,
        "kickoff_utc": m.get("kickoff_utc"),
        "match_date": m.get("match_date"),
        "home_score": m.get("home_score"),
        "away_score": m.get("away_score"),
        "home_score_et": m.get("home_score_et"),
        "away_score_et": m.get("away_score_et"),
        "home_pens": m.get("home_pens"),
        "away_pens": m.get("away_pens"),
        "status": m.get("status", "FINISHED"),
    }


def outcome_label(row: dict) -> str | None:
    h, a = row.get("home_score"), row.get("away_score")
    if h is None or a is None:
        return None
    if h > a:
        return "H"
    if h < a:
        return "A"
    return "D"


def advanced_label(row: dict) -> str | None:
    """For knockouts: who actually advanced after AET/pens."""
    he, ae = row.get("home_score_et"), row.get("away_score_et")
    if he is not None and ae is not None and he != ae:
        return "H" if he > ae else "A"
    hp, ap = row.get("home_pens"), row.get("away_pens")
    if hp is not None and ap is not None and hp != ap:
        return "H" if hp > ap else "A"
    out = outcome_label(row)
    return out if out in ("H", "A") else None


def upsert(conn: sqlite3.Connection, fixture_id: int, row: dict,
           source: str) -> None:
    conn.execute(
        """
        INSERT INTO actual_results (
            fixture_id, match_date, kickoff_utc,
            home_score, away_score,
            home_score_et, away_score_et,
            home_pens, away_pens,
            outcome, advanced, status, source, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fixture_id) DO UPDATE SET
            match_date    = excluded.match_date,
            kickoff_utc   = excluded.kickoff_utc,
            home_score    = excluded.home_score,
            away_score    = excluded.away_score,
            home_score_et = excluded.home_score_et,
            away_score_et = excluded.away_score_et,
            home_pens     = excluded.home_pens,
            away_pens     = excluded.away_pens,
            outcome       = excluded.outcome,
            advanced      = excluded.advanced,
            status        = excluded.status,
            source        = excluded.source,
            fetched_at    = excluded.fetched_at
        """,
        (
            fixture_id,
            row.get("match_date"),
            row.get("kickoff_utc"),
            row.get("home_score"),
            row.get("away_score"),
            row.get("home_score_et"),
            row.get("away_score_et"),
            row.get("home_pens"),
            row.get("away_pens"),
            outcome_label(row),
            advanced_label(row),
            row.get("status"),
            source,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB)
    # Schema may pre-date this table on older clones.
    conn.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
    lookup = load_fixture_lookup(conn)
    print(f"Loaded {len(lookup)} fixture rows for matching.")

    written = 0
    skipped_unmatched: list[tuple[str, str, str]] = []

    print("Trying football-data.org...")
    for m in fetch_api():
        row = normalise_api_match(m)
        if not row:
            continue
        key = (row["stage"], row["home_team"], row["away_team"])
        fid = lookup.get(key)
        if fid is None:
            skipped_unmatched.append(key)
            continue
        upsert(conn, fid, row, "football-data.org")
        written += 1

    print("Merging data/manual_results.json...")
    for m in load_manual():
        row = normalise_manual(m)
        if not row:
            continue
        key = (row["stage"], row["home_team"], row["away_team"])
        fid = lookup.get(key)
        if fid is None:
            skipped_unmatched.append(key)
            continue
        upsert(conn, fid, row, "manual")
        written += 1

    conn.commit()
    finished = conn.execute(
        "SELECT COUNT(*) FROM actual_results WHERE outcome IS NOT NULL"
    ).fetchone()[0]
    print(f"Wrote {written} rows; {finished} have a final outcome.")
    if skipped_unmatched:
        print(f"Could not match {len(skipped_unmatched)} matches "
              "(usually unresolved knockout slots). Sample:")
        for k in skipped_unmatched[:5]:
            print(f"  {k}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
