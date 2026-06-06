"""
Headless runner — exercises the same modelling pipeline as the notebooks
and writes predictions to SQLite. Useful for verification + CI without
opening Jupyter / RStudio.

    python src/run_predictions.py [--n-sim 10000]
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.models import (DixonColesModel, corners_baseline, cards_baseline,
                        shootout_winner_prob, simulate_match,
                        matchup_to_adjust)

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "wc2026.sqlite"


def standings_from(scores: pd.DataFrame) -> pd.DataFrame:
    home = scores[["group_id", "home_team", "h", "a"]].rename(
        columns={"home_team": "team", "h": "gf", "a": "ga"})
    away = scores[["group_id", "away_team", "a", "h"]].rename(
        columns={"away_team": "team", "a": "gf", "h": "ga"})
    long = pd.concat([home, away], ignore_index=True)
    long["pts"] = np.where(long["gf"] > long["ga"], 3,
                  np.where(long["gf"] == long["ga"], 1, 0))
    long["gd"] = long["gf"] - long["ga"]
    agg = (long.groupby(["group_id", "team"])
              .agg(pts=("pts", "sum"), gd=("gd", "sum"), gf=("gf", "sum"))
              .reset_index())
    agg["rank"] = (agg.sort_values(["group_id", "pts", "gd", "gf"],
                                   ascending=[True, False, False, False])
                      .groupby("group_id").cumcount() + 1)
    return agg


def main(n_sim: int = 10_000) -> None:
    t0 = time.time()
    conn = sqlite3.connect(DB)

    print("[1] Loading historical matches (since 2014)...")
    hist = pd.read_sql("""
        SELECT match_date, home_team, away_team, home_score, away_score, neutral
        FROM historical_matches WHERE match_date >= '2014-01-01'
    """, conn)
    hist["match_date"] = pd.to_datetime(hist["match_date"])
    hist["neutral"] = hist["neutral"].fillna(0).astype(int)
    print(f"    {len(hist):,} matches")

    print("[2] Fitting Dixon - Coles...")
    model = DixonColesModel()
    model.fit(hist, ref_date=pd.Timestamp("2026-06-10"))
    print(f"    {len(model.teams)} teams, "
          f"home_adv={model.home_adv:.3f}, rho={model.rho:.3f} "
          f"({time.time() - t0:.1f}s)")

    fx_grp = pd.read_sql("""
        SELECT f.fixture_id, f.group_id, f.home_team, f.away_team,
               m.matchup_score_home
        FROM wc2026_fixtures f
        LEFT JOIN fixture_matchups m ON m.fixture_id = f.fixture_id
        WHERE f.stage = 'GROUP'
    """, conn)
    print(f"[3] {len(fx_grp)} group-stage fixtures "
          f"({fx_grp['matchup_score_home'].notna().sum()} with position matchup)")

    # Filter to teams the model knows
    unknown = set(fx_grp["home_team"]).union(fx_grp["away_team"]) - set(model.teams)
    if unknown:
        print(f"    [warn] unknown to model: {sorted(unknown)}")

    print(f"[4] Monte Carlo: {n_sim:,} tournament runs...")
    rng = np.random.default_rng(2026)
    champion_counts: dict[str, int] = {}
    reach: dict[str, dict[str, int]] = {}

    for sim in range(n_sim):
        # Group stage
        rows = []
        for _, fx in fx_grp.iterrows():
            adj = matchup_to_adjust(fx["matchup_score_home"])
            r = simulate_match(model, fx["home_team"], fx["away_team"],
                               stage="GROUP", neutral=True, rng=rng,
                               matchup_adjust=adj)
            rows.append({"group_id": fx["group_id"],
                         "home_team": fx["home_team"],
                         "away_team": fx["away_team"],
                         "h": r["home_score"], "a": r["away_score"]})
        st = standings_from(pd.DataFrame(rows))
        top2 = st[st["rank"] <= 2]
        thirds = (st[st["rank"] == 3]
                  .sort_values(["pts", "gd", "gf"], ascending=False).head(8))
        advancers = pd.concat([top2, thirds], ignore_index=True)
        bracket = advancers.sort_values(["rank", "group_id"])["team"].tolist()

        # Knockouts
        for stage in ["R32", "R16", "QF", "SF", "FINAL"]:
            for t in bracket:
                reach.setdefault(t, {}).setdefault(stage, 0)
                reach[t][stage] += 1
            nxt = []
            for i in range(0, len(bracket), 2):
                h, a = bracket[i], bracket[i + 1]
                r = simulate_match(model, h, a, stage=stage,
                                   neutral=True, knockout=True, rng=rng)
                nxt.append(h if r["home_advances"] else a)
            bracket = nxt
        champ = bracket[0]
        champion_counts[champ] = champion_counts.get(champ, 0) + 1
        if sim and sim % 500 == 0:
            print(f"    sim {sim:>5}/{n_sim} ({time.time() - t0:.0f}s)")

    print(f"[5] Writing predictions and tournament_sim ...")
    # Per-match predictions
    pred_rows = []
    for _, fx in fx_grp.iterrows():
        h, a = fx["home_team"], fx["away_team"]
        adj = matchup_to_adjust(fx["matchup_score_home"])
        mh, ma = model.modal_score(h, a, matchup_adjust=adj)
        pwh, pwd, pwa = model.outcome_probs(h, a, matchup_adjust=adj)
        # Pass the position matchup to the corners/cards baseline too
        sg = ((model.attack.get(h, 0) - model.attack.get(a, 0)
               + model.defense.get(a, 0) - model.defense.get(h, 0))
              + adj * 2.0)
        ch, ca = corners_baseline("GROUP", sg)
        yh, ya, prh, pra = cards_baseline("GROUP", sg)
        pred_rows.append({
            "fixture_id": int(fx["fixture_id"]), "source": "python",
            "modal_home_score": mh, "modal_away_score": ma,
            "p_home_win": pwh, "p_draw": pwd, "p_away_win": pwa,
            "exp_home_corners": ch, "exp_away_corners": ca,
            "exp_home_yellows": yh, "exp_away_yellows": ya,
            "p_home_red": prh, "p_away_red": pra,
            "p_penalties": None, "p_home_advances": None,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        })
    conn.execute("DELETE FROM predictions WHERE source = 'python'")
    pd.DataFrame(pred_rows).to_sql("predictions", conn,
                                   if_exists="append", index=False)

    teams = pd.read_sql("SELECT team FROM wc2026_groups", conn)["team"]
    sim_rows = []
    for t in teams:
        sim_rows.append({
            "team": t, "source": "python",
            "p_champion":   champion_counts.get(t, 0) / n_sim,
            "p_reach_final": reach.get(t, {}).get("FINAL", 0) / n_sim,
            "p_reach_sf":    reach.get(t, {}).get("SF", 0) / n_sim,
            "p_reach_qf":    reach.get(t, {}).get("QF", 0) / n_sim,
            "p_reach_r16":   reach.get(t, {}).get("R16", 0) / n_sim,
            "p_advance_r32": reach.get(t, {}).get("R32", 0) / n_sim,
            "p_group_winner": None, "p_group_runnerup": None,
            "n_simulations": n_sim,
        })
    conn.execute("DELETE FROM tournament_sim WHERE source = 'python'")
    pd.DataFrame(sim_rows).to_sql("tournament_sim", conn,
                                  if_exists="append", index=False)
    conn.commit()

    print(f"\n[6] Top 12 championship probabilities ({n_sim:,} sims):")
    top = (pd.DataFrame(sim_rows).sort_values("p_champion", ascending=False)
              .head(12)[["team", "p_champion", "p_reach_final", "p_reach_sf"]])
    top["p_champion"] = (top["p_champion"] * 100).round(1)
    top["p_reach_final"] = (top["p_reach_final"] * 100).round(1)
    top["p_reach_sf"] = (top["p_reach_sf"] * 100).round(1)
    print(top.to_string(index=False))

    conn.close()
    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n-sim", type=int, default=10_000)
    args = p.parse_args()
    main(n_sim=args.n_sim)
