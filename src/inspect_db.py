"""Quick data-quality peek at wc2026.sqlite. Run from the project root."""

import sqlite3
import pandas as pd
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "wc2026.sqlite"
conn = sqlite3.connect(DB)
pd.set_option("display.width", 120)

print("=== Historical matches by tournament (top 10) ===")
print(pd.read_sql(
    "SELECT tournament, COUNT(*) AS n FROM historical_matches "
    "GROUP BY tournament ORDER BY n DESC LIMIT 10", conn))

print("\n=== Top 10 squad strengths (EA FC) ===")
print(pd.read_sql(
    "SELECT team, ROUND(squad_overall_top11, 1) AS top11, n_players_top23 "
    "FROM team_squad_strength ORDER BY squad_overall_top11 DESC LIMIT 10",
    conn))

print("\n=== Latest FIFA rankings (top 10) ===")
print(pd.read_sql("""
    WITH latest AS (
        SELECT team, rank, points,
               ROW_NUMBER() OVER (PARTITION BY team ORDER BY rank_date DESC) AS rn
        FROM fifa_rankings
    )
    SELECT team, rank, ROUND(points, 1) AS pts FROM latest
    WHERE rn = 1 ORDER BY rank LIMIT 10
""", conn))

print("\n=== Group L fixtures (sample) ===")
print(pd.read_sql(
    "SELECT home_team, away_team FROM wc2026_fixtures "
    "WHERE group_id = 'L'", conn))

print("\n=== Cross-check: how many WC2026 teams have squad ratings? ===")
print(pd.read_sql("""
    SELECT g.team,
           CASE WHEN s.team IS NULL THEN 'MISSING' ELSE 'ok' END AS squad
    FROM wc2026_groups g LEFT JOIN team_squad_strength s ON s.team = g.team
""", conn).pivot_table(index="squad", values="team",
                       aggfunc="count").to_string())

conn.close()
