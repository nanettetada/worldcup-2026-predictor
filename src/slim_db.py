"""Build a slim copy of wc2026.sqlite containing only the tables the
Streamlit dashboard queries. Drops historical_matches, fifa_rankings,
player_ratings - these are used for model training, not display."""

import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "wc2026.sqlite"
DST = ROOT / "data" / "wc2026.sqlite"  # in-place

KEEP = {
    "wc2026_groups", "wc2026_fixtures",
    "predictions", "tournament_sim",
    "team_squad_strength", "team_position_strength", "fixture_matchups",
    "model_params",
}


def main() -> None:
    conn = sqlite3.connect(SRC)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"Found {len(tables)} tables; keeping {len(KEEP)}")
    for t in tables:
        if t not in KEEP and not t.startswith("sqlite_"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            print(f"  dropped {t}")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"Slim DB size: {SRC.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
