"""
Quick smoke test for the modelling code. Generates synthetic international
matches between a handful of fake teams, fits Dixon - Coles, and checks the
core surfaces.

    python src/smoke_test.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.models import (
    DixonColesModel, corners_baseline, cards_baseline,
    shootout_winner_prob, simulate_match,
)


def synth_matches(n: int = 800, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    teams = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    # latent attack/defence per team
    attack = dict(zip(teams, rng.normal(0, 0.4, len(teams))))
    defense = dict(zip(teams, rng.normal(0, 0.3, len(teams))))
    home_adv = 0.25

    rows = []
    base = pd.Timestamp("2014-01-01")
    for i in range(n):
        h, a = rng.choice(teams, 2, replace=False)
        neutral = rng.random() < 0.3
        lam = np.exp(attack[h] + defense[a] + (0 if neutral else home_adv))
        mu = np.exp(attack[a] + defense[h])
        rows.append({
            "match_date": base + pd.Timedelta(days=int(i * 5)),
            "home_team": h, "away_team": a,
            "home_score": int(rng.poisson(lam)),
            "away_score": int(rng.poisson(mu)),
            "neutral": int(neutral),
        })
    return pd.DataFrame(rows)


def main() -> None:
    print("[1] Generating 800 synthetic matches...")
    matches = synth_matches()
    print(f"    teams: {sorted(set(matches.home_team).union(matches.away_team))}")

    print("[2] Fitting Dixon - Coles...")
    model = DixonColesModel()
    model.fit(matches, ref_date=pd.Timestamp("2026-06-01"))
    print(f"    home_adv = {model.home_adv:.3f}, rho = {model.rho:.3f}")
    print("    attack rankings:")
    for t in sorted(model.teams, key=lambda x: -model.attack[x]):
        print(f"      {t:10s} attack={model.attack[t]:+.3f}  "
              f"defense={model.defense[t]:+.3f}")

    print("\n[3] Score matrix Alpha vs Foxtrot (neutral):")
    m = model.score_matrix("Alpha", "Foxtrot", neutral=True, max_goals=5)
    pd.options.display.float_format = "{:.3f}".format
    print(pd.DataFrame(m).round(3))
    pwh, pdr, pwa = model.outcome_probs("Alpha", "Foxtrot")
    print(f"    P(Alpha) = {pwh:.3f}  P(Draw) = {pdr:.3f}  P(Foxtrot) = {pwa:.3f}")
    print(f"    Modal score: {model.modal_score('Alpha', 'Foxtrot')}")

    print("\n[4] Corners and cards baselines:")
    for stage in ["GROUP", "QF", "FINAL"]:
        ch, ca = corners_baseline(stage, strength_gap=1.0)
        yh, ya, prh, pra = cards_baseline(stage, strength_gap=1.0)
        print(f"    {stage:>5}  corners {ch:.1f}/{ca:.1f}  "
              f"yellows {yh:.1f}/{ya:.1f}  reds {prh:.2f}/{pra:.2f}")

    print("\n[5] Penalty shootout probabilities:")
    for h, a in [("Germany", "England"), ("Brazil", "Croatia"),
                 ("Morocco", "France"), ("Senegal", "Belgium")]:
        print(f"    {h:>8} vs {a:<8}  P({h} wins shootout) = "
              f"{shootout_winner_prob(h, a):.3f}")

    print("\n[6] Simulating 1000 Alpha vs Foxtrot knockouts:")
    rng = np.random.default_rng(42)
    advances = pens = 0
    for _ in range(1000):
        r = simulate_match(model, "Alpha", "Foxtrot",
                           stage="QF", knockout=True, rng=rng)
        advances += r["home_advances"]
        pens += r["went_to_pens"]
    print(f"    Alpha advanced in {advances}/1000 runs "
          f"({pens} went to penalties)")

    print("\n[OK] Smoke test passed.")


if __name__ == "__main__":
    main()
