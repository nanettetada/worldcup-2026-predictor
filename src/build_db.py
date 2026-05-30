"""
Build the shared SQLite database used by both notebooks.

Two essential data sources:
  1. International football results, 1872 - present
       Kaggle: martj42/international-football-results-from-1872-to-2017
  2. FIFA world rankings
       Kaggle: cashncarry/fifaworldranking

Run from the project root:
    python src/build_db.py

If kagglehub is not configured the script will fall back to looking for the
CSVs in data/raw/ — drop them in manually and re-run.
"""

from __future__ import annotations

import itertools
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_FIX = ROOT / "data" / "fixtures"
DB_PATH = ROOT / "data" / "wc2026.sqlite"
SCHEMA = ROOT / "src" / "schema.sql"

RESULTS_FILES = ["results.csv"]
PLAYER_FILES = [
    # FC 26 first
    "FC26_20250921.csv", "fc26_players.csv", "players_fc26.csv",
    "players_26.csv",
    # FC 25 fallback
    "male_players.csv", "male_players_legacy.csv",
    "players_25.csv", "fc25_players.csv",
]


def _read_latest_fifa_ranking() -> pd.DataFrame | None:
    """The Kaggle FIFA-ranking dataset ships one CSV per snapshot. Concat all."""
    files = sorted(DATA_RAW.glob("fifa_ranking-*.csv"))
    if not files:
        return None
    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_csv(f))
        except Exception as exc:
            print(f"  [warn] could not read {f.name}: {exc}")
    return pd.concat(dfs, ignore_index=True) if dfs else None


# ---------------------------------------------------------------------------
# Data acquisition
# ---------------------------------------------------------------------------

def acquire_kaggle_datasets() -> None:
    """Download all datasets via kagglehub. Skips silently if unavailable.
    Retries each dataset up to 3 times - the Kaggle CDN sometimes drops the
    first connection on cold cache."""
    try:
        import kagglehub
    except ImportError:
        print("[warn] kagglehub not installed - assuming CSVs are in data/raw/")
        return

    # Primary slugs plus fallbacks in case the primary is renamed/unavailable
    targets = [
        ("martj42/international-football-results-from-1872-to-2017", "results",
         []),
        ("cashncarry/fifaworldranking", "rankings",
         ["tadhgfitzgerald/fifa-international-soccer-mens-ranking-1993now"]),
        ("rovnez/fc-26-fifa-26-player-data", "fc26_players",
         ["talhademirezen/fc-26-player-stats",
          "nyagami/ea-sports-fc-25-database-ratings-and-stats"]),
    ]
    for slug, label, fallbacks in targets:
        for attempt_slug in [slug] + fallbacks:
            ok = False
            for attempt in range(3):
                try:
                    print(f"[kaggle] downloading {label} (try {attempt + 1}): "
                          f"{attempt_slug}")
                    cache_path = Path(kagglehub.dataset_download(attempt_slug))
                    n = 0
                    for csv in cache_path.rglob("*.csv"):
                        dest = DATA_RAW / csv.name
                        if not dest.exists():
                            dest.write_bytes(csv.read_bytes())
                            print(f"  -> copied {csv.name}")
                            n += 1
                    if n > 0 or any(
                            (DATA_RAW / f).exists() for f in
                            cache_path.rglob("*.csv")):
                        ok = True
                        break
                except Exception as exc:
                    print(f"  [retry] {type(exc).__name__}: {exc}")
            if ok:
                break


def _read_first_existing(candidates: list[str]) -> pd.DataFrame | None:
    for name in candidates:
        path = DATA_RAW / name
        if path.exists():
            # latin-1 + utf-8 fallback for the EA FC files that ship with
            # accented characters (Curacao, Cote d'Ivoire) in mixed encoding
            for enc in ("utf-8", "latin-1"):
                try:
                    return pd.read_csv(path, encoding=enc)
                except UnicodeDecodeError:
                    continue
    return None


# Canonical WC2026 team name -> known aliases used in other sources
TEAM_ALIASES = {
    "Czechia":         ["Czech Republic"],
    "Turkiye":         ["Turkey", "Türkiye", "T�rkiye", "T?rkiye"],
    "Cote d'Ivoire":   ["Côte d'Ivoire", "Ivory Coast"],
    "Curacao":         ["Curaçao", "Curacao"],
    "Cabo Verde":      ["Cape Verde Islands", "Cape Verde"],
    "Netherlands":     ["Holland"],
    "Korea Republic":  ["South Korea", "Korea Republic"],
    "United States":   ["USA", "United States of America"],
    "Congo DR":        ["DR Congo", "Democratic Republic of the Congo"],
}


def _apply_team_aliases(series: pd.Series) -> pd.Series:
    """Map alias values back to the canonical WC2026 name."""
    mapping = {alias: canonical
               for canonical, aliases in TEAM_ALIASES.items()
               for alias in aliases}
    return series.replace(mapping)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())


def load_historical_matches(conn: sqlite3.Connection) -> int:
    df = _read_first_existing(RESULTS_FILES)
    if df is None:
        print("[skip] no results.csv - historical matches not loaded")
        return 0

    keep = ["date", "home_team", "away_team", "home_score",
            "away_score", "tournament", "city", "country", "neutral"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df = df.rename(columns={"date": "match_date"})
    before = len(df)
    df = df.dropna(subset=["home_score", "away_score",
                           "home_team", "away_team", "match_date"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    df["neutral"] = df.get("neutral", False).astype(int)
    df["home_team"] = _apply_team_aliases(df["home_team"])
    df["away_team"] = _apply_team_aliases(df["away_team"])
    dropped = before - len(df)
    if dropped:
        print(f"  dropped {dropped} rows with missing scores")
    df.to_sql("historical_matches", conn, if_exists="append", index=False)
    return len(df)


def load_fifa_rankings(conn: sqlite3.Connection) -> int:
    df = _read_latest_fifa_ranking()
    if df is None:
        print("[skip] no FIFA ranking CSV - rankings not loaded")
        return 0

    # The cashncarry/fifaworldranking schema is one of:
    #   rank, country_full, country_abrv, total_points, ..., rank_date
    #   or older: country_name, ...
    rename = {
        "country_full": "team", "country_name": "team", "team_name": "team",
        "total_points": "points", "total_pts": "points",
        "rank_date": "rank_date", "date": "rank_date",
        "rank": "rank", "fifa_rank": "rank",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "team" not in df.columns:
        print(f"  [warn] FIFA ranking CSV has no team column; cols = {list(df.columns)[:10]}")
        return 0
    df = df[["team", "rank_date", "rank", "points"]]
    df["team"] = _apply_team_aliases(df["team"])
    # Dedup AFTER alias mapping or we hit PK conflict
    # (e.g. both Holland and Netherlands -> Netherlands)
    df = df.drop_duplicates(subset=["team", "rank_date"])
    df.to_sql("fifa_rankings", conn, if_exists="append", index=False)
    return len(df)


def load_player_ratings(conn: sqlite3.Connection) -> int:
    """Load EA FC player ratings, preferring FC 26 over FC 25 if both present."""
    df = _read_first_existing(PLAYER_FILES)
    if df is None:
        print("[skip] no EA FC player CSV - squad strength not loaded")
        return 0

    # The two main schemas (FC 25 / FC 26 from Kaggle) have slightly different
    # column names. Map both to a common shape.
    col_map = {
        "long_name": "player_name", "short_name": "player_name",
        "Name": "player_name", "name": "player_name",
        "nationality_name": "nationality", "Nation": "nationality",
        "nation": "nationality",
        "club_name": "club", "Club": "club",
        "player_positions": "position", "Position": "position", "pos": "position",
        "Age": "age", "OVR": "overall_rating", "overall": "overall_rating",
        "Overall": "overall_rating", "Potential": "potential",
        "potential": "potential",
        "PAC": "pace", "SHO": "shooting", "PAS": "passing",
        "DRI": "dribbling", "DEF": "defending", "PHY": "physic",
        "pace": "pace", "shooting": "shooting", "passing": "passing",
        "dribbling": "dribbling", "defending": "defending",
        "physic": "physic", "physical": "physic",
        "sofifa_id": "player_id", "id": "player_id",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    wanted = ["player_id", "player_name", "nationality", "club", "position",
              "age", "overall_rating", "potential", "pace", "shooting",
              "passing", "dribbling", "defending", "physic"]
    for col in wanted:
        if col not in df.columns:
            df[col] = None

    df = df[wanted].copy()
    df["nationality"] = _apply_team_aliases(df["nationality"])
    df["game_version"] = "FC26" if any(
        (DATA_RAW / f).exists() for f in PLAYER_FILES[:4]) else "FC25"
    df.to_sql("player_ratings", conn, if_exists="append", index=False)
    return len(df)


def build_squad_strength(conn: sqlite3.Connection) -> int:
    """Aggregate top-23 players per nation into a single squad-strength row."""
    cur = conn.execute("SELECT COUNT(*) FROM player_ratings")
    if cur.fetchone()[0] == 0:
        return 0

    sql = """
        WITH ranked AS (
            SELECT nationality, overall_rating, pace, shooting, passing,
                   dribbling, defending, physic, age,
                   ROW_NUMBER() OVER (PARTITION BY nationality
                                      ORDER BY overall_rating DESC) AS rn
            FROM player_ratings
            WHERE nationality IS NOT NULL AND overall_rating IS NOT NULL
        ),
        top23 AS (SELECT * FROM ranked WHERE rn <= 23),
        top11 AS (SELECT * FROM ranked WHERE rn <= 11)
        INSERT INTO team_squad_strength
        SELECT
            top23.nationality                                    AS team,
            AVG(top23.overall_rating)                            AS squad_overall_mean,
            (SELECT AVG(overall_rating) FROM top11
                WHERE top11.nationality = top23.nationality)     AS squad_overall_top11,
            AVG((COALESCE(top23.pace,0) + COALESCE(top23.shooting,0)
                 + COALESCE(top23.dribbling,0)) / 3.0)           AS squad_attack_mean,
            AVG((COALESCE(top23.defending,0) + COALESCE(top23.physic,0)
                 + COALESCE(top23.passing,0)) / 3.0)             AS squad_defense_mean,
            AVG(top23.age)                                       AS squad_age_mean,
            COUNT(*)                                             AS n_players_top23
        FROM top23
        GROUP BY top23.nationality;
    """
    conn.executescript(sql)
    return conn.execute(
        "SELECT COUNT(*) FROM team_squad_strength").fetchone()[0]


def load_groups(conn: sqlite3.Connection) -> int:
    df = pd.read_csv(DATA_FIX / "groups.csv")
    df = df.rename(columns={"group": "group_id", "host": "is_host"})
    df.to_sql("wc2026_groups", conn, if_exists="append", index=False)
    return len(df)


# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------

def build_group_fixtures(conn: sqlite3.Connection) -> int:
    """Generate the 72 round-robin group-stage matches from the draw."""
    groups = pd.read_sql("SELECT group_id, team FROM wc2026_groups", conn)
    rows = []
    fixture_id = 1
    for group_id, sub in groups.groupby("group_id"):
        teams = sub["team"].tolist()
        # 6 matches per group of 4: every unique pair
        for home, away in itertools.combinations(teams, 2):
            rows.append({
                "fixture_id": fixture_id,
                "stage": "GROUP",
                "group_id": group_id,
                "match_date": None,
                "kickoff_local": None,
                "venue_city": None,
                "venue_country": None,
                "home_team": home,
                "away_team": away,
                "home_slot": None,
                "away_slot": None,
            })
            fixture_id += 1
    return _insert_fixtures(conn, rows)


def build_knockout_skeleton(conn: sqlite3.Connection) -> int:
    """
    Placeholders for the 32 knockout matches. Teams are unresolved until the
    group sim runs; we record the slot labels so the dashboard can show
    "Winner Group A vs 3rd C/D/E/F" style entries.
    """
    fixture_id = pd.read_sql(
        "SELECT COALESCE(MAX(fixture_id), 0) AS m FROM wc2026_fixtures",
        conn).iloc[0]["m"] + 1
    rows = []

    # Round of 32: 16 matches. The exact 2026 bracket pairs winners/runners-up
    # of one group against 3rd-placed teams or other group runners-up. We
    # encode placeholders that the bracket simulator resolves.
    r32_pairs = [
        ("1A", "2B"), ("1C", "3DEF"), ("1E", "2F"), ("1G", "3HIJ"),
        ("1I", "2J"), ("1K", "3ABCK"), ("1B", "2A"), ("1D", "3ABCL"),
        ("1F", "2E"), ("1H", "3GHIK"), ("1J", "2I"), ("1L", "3BFJL"),
        ("2C", "2D"), ("2G", "2H"), ("2K", "2L"), ("3CDFH", "3EFGL"),
    ]
    for h, a in r32_pairs:
        rows.append({
            "fixture_id": fixture_id, "stage": "R32", "group_id": None,
            "match_date": None, "kickoff_local": None, "venue_city": None,
            "venue_country": None, "home_team": None, "away_team": None,
            "home_slot": h, "away_slot": a,
        })
        fixture_id += 1

    # Round of 16 (8), QF (4), SF (2), 3rd-place (1), Final (1)
    for stage, count in [("R16", 8), ("QF", 4), ("SF", 2),
                         ("3RD", 1), ("FINAL", 1)]:
        for i in range(count):
            rows.append({
                "fixture_id": fixture_id, "stage": stage, "group_id": None,
                "match_date": None, "kickoff_local": None, "venue_city": None,
                "venue_country": None, "home_team": None, "away_team": None,
                "home_slot": f"{stage}-{i+1}-H", "away_slot": f"{stage}-{i+1}-A",
            })
            fixture_id += 1

    return _insert_fixtures(conn, rows)


def _insert_fixtures(conn: sqlite3.Connection, rows: list[dict]) -> int:
    df = pd.DataFrame(rows)
    df.to_sql("wc2026_fixtures", conn, if_exists="append", index=False)
    return len(df)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"[reset] removed existing {DB_PATH.name}")

    acquire_kaggle_datasets()

    conn = sqlite3.connect(DB_PATH)
    try:
        load_schema(conn)
        n_hist = load_historical_matches(conn)
        n_rank = load_fifa_rankings(conn)
        n_play = load_player_ratings(conn)
        n_squad = build_squad_strength(conn)
        n_grp = load_groups(conn)
        n_gfx = build_group_fixtures(conn)
        n_kfx = build_knockout_skeleton(conn)
        conn.commit()
    finally:
        conn.close()

    print()
    print("Database built at", DB_PATH)
    print(f"  historical_matches  : {n_hist:>7,}")
    print(f"  fifa_rankings       : {n_rank:>7,}")
    print(f"  player_ratings      : {n_play:>7,}")
    print(f"  team_squad_strength : {n_squad:>7,}")
    print(f"  wc2026_groups       : {n_grp:>7,}  (48 teams expected)")
    print(f"  group fixtures      : {n_gfx:>7,}  (72 expected)")
    print(f"  knockout skeleton   : {n_kfx:>7,}  (32 expected)")
    print(f"  built at           : {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    sys.exit(main())
