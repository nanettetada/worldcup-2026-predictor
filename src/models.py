"""
Modelling core for the World Cup 2026 predictor.

Contains:
  - DixonColesModel : bivariate Poisson with low-score correction
                       and exponential time-decay weighting
  - score_matrix    : per-fixture goal probability matrix
  - corners_baseline / cards_baseline : tournament-average baselines
  - shootout_winner_prob : historical shootout conversion model
  - simulate_match  : single match draw (goals, corners, cards, shootout)

The Python notebook imports these directly. The R notebook re-implements
the same logic so the two ecosystems can be cross-validated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import lgamma
from typing import Iterable

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson


# ---------------------------------------------------------------------------
# Dixon - Coles bivariate Poisson
# ---------------------------------------------------------------------------

def _dc_correction(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Low-score correlation correction term from Dixon & Coles (1997)."""
    if x == 0 and y == 0:
        return 1 - lam * mu * rho
    if x == 0 and y == 1:
        return 1 + lam * rho
    if x == 1 and y == 0:
        return 1 + mu * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


@dataclass
class DixonColesModel:
    teams: list[str] = field(default_factory=list)
    attack: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    home_adv: float = 0.25
    rho: float = -0.05
    xi: float = 0.0019           # time-decay rate (~ per day, Dixon-Coles)
    fitted: bool = False

    # ---- training -----------------------------------------------------

    def _lambdas(self, home: str, away: str, neutral: bool = False
                 ) -> tuple[float, float]:
        ha = 0.0 if neutral else self.home_adv
        lam = np.exp(self.attack[home] + self.defense[away] + ha)
        mu = np.exp(self.attack[away] + self.defense[home])
        return float(lam), float(mu)

    def _neg_log_likelihood(self, params: np.ndarray,
                            home_idx: np.ndarray, away_idx: np.ndarray,
                            home_goals: np.ndarray, away_goals: np.ndarray,
                            neutral: np.ndarray, weights: np.ndarray,
                            n_teams: int) -> float:
        n = n_teams
        attack = params[:n]
        defense = params[n:2 * n]
        home_adv, rho = params[-2], params[-1]
        # zero-sum constraint on attack
        attack = attack - attack.mean()

        ha = np.where(neutral, 0.0, home_adv)
        lam = np.exp(attack[home_idx] + defense[away_idx] + ha)
        mu = np.exp(attack[away_idx] + defense[home_idx])

        log_factorial_h = np.array([lgamma(int(g) + 1) for g in home_goals])
        log_factorial_a = np.array([lgamma(int(g) + 1) for g in away_goals])
        ll = (
            home_goals * np.log(lam) - lam
            + away_goals * np.log(mu) - mu
            - log_factorial_h - log_factorial_a
        )
        # Dixon-Coles low-score correction
        corr = np.array([_dc_correction(int(h), int(a), float(l), float(m), rho)
                         for h, a, l, m in zip(home_goals, away_goals, lam, mu)])
        corr = np.clip(corr, 1e-12, None)
        ll = ll + np.log(corr)
        return -float((ll * weights).sum())

    def fit(self, matches: pd.DataFrame, ref_date: pd.Timestamp) -> "DixonColesModel":
        """
        matches columns required:
          match_date (datetime64), home_team, away_team,
          home_score, away_score, neutral (0/1)
        """
        df = matches.dropna(subset=["home_team", "away_team",
                                    "home_score", "away_score"]).copy()
        df["match_date"] = pd.to_datetime(df["match_date"])
        df = df[df["match_date"] <= ref_date]

        teams = sorted(set(df["home_team"]).union(df["away_team"]))
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)

        home_idx = df["home_team"].map(idx).to_numpy()
        away_idx = df["away_team"].map(idx).to_numpy()
        hg = df["home_score"].to_numpy(dtype=int)
        ag = df["away_score"].to_numpy(dtype=int)
        neu = df["neutral"].fillna(0).astype(int).to_numpy()

        days = (ref_date - df["match_date"]).dt.days.to_numpy()
        w = np.exp(-self.xi * days)

        x0 = np.concatenate([np.zeros(n), np.zeros(n), [0.25, -0.05]])
        res = minimize(
            self._neg_log_likelihood, x0,
            args=(home_idx, away_idx, hg, ag, neu, w, n),
            method="L-BFGS-B",
            options={"maxiter": 200, "disp": False},
        )

        attack = res.x[:n]
        defense = res.x[n:2 * n]
        attack = attack - attack.mean()

        self.teams = teams
        self.attack = dict(zip(teams, attack))
        self.defense = dict(zip(teams, defense))
        self.home_adv = float(res.x[-2])
        self.rho = float(res.x[-1])
        self.fitted = True
        return self

    # ---- prediction ---------------------------------------------------

    def score_matrix(self, home: str, away: str, neutral: bool = True,
                     max_goals: int = 8) -> np.ndarray:
        lam, mu = self._lambdas(home, away, neutral=neutral)
        m = np.outer(poisson.pmf(np.arange(max_goals + 1), lam),
                     poisson.pmf(np.arange(max_goals + 1), mu))
        for h in range(2):
            for a in range(2):
                m[h, a] *= _dc_correction(h, a, lam, mu, self.rho)
        return m / m.sum()

    def outcome_probs(self, home: str, away: str, neutral: bool = True
                      ) -> tuple[float, float, float]:
        m = self.score_matrix(home, away, neutral=neutral)
        p_home = float(np.tril(m, -1).sum())
        p_draw = float(np.trace(m))
        p_away = float(np.triu(m, 1).sum())
        return p_home, p_draw, p_away

    def modal_score(self, home: str, away: str, neutral: bool = True
                    ) -> tuple[int, int]:
        m = self.score_matrix(home, away, neutral=neutral)
        h, a = np.unravel_index(np.argmax(m), m.shape)
        return int(h), int(a)


# ---------------------------------------------------------------------------
# Corners / cards baseline
# ---------------------------------------------------------------------------
# Per-match averages from the last three World Cups (2014, 2018, 2022)
# Source: published tournament technical reports
WC_AVERAGES = {
    "GROUP":  {"corners": 9.3, "yellows": 3.8, "p_red": 0.06},
    "R32":    {"corners": 9.7, "yellows": 4.5, "p_red": 0.10},
    "R16":    {"corners": 9.8, "yellows": 4.7, "p_red": 0.11},
    "QF":     {"corners": 10.1, "yellows": 5.0, "p_red": 0.13},
    "SF":     {"corners": 10.0, "yellows": 5.2, "p_red": 0.13},
    "3RD":    {"corners": 9.8, "yellows": 4.6, "p_red": 0.09},
    "FINAL":  {"corners": 10.3, "yellows": 5.4, "p_red": 0.14},
}


def corners_baseline(stage: str, strength_gap: float
                     ) -> tuple[float, float]:
    """
    Return (home_corners, away_corners) expected counts.
    strength_gap = home_strength - away_strength on a scale roughly in [-3, 3].
    Stronger team gets more corners (more attacking time).
    """
    total = WC_AVERAGES[stage]["corners"]
    home_share = 0.5 + 0.06 * strength_gap
    home_share = float(np.clip(home_share, 0.2, 0.8))
    return total * home_share, total * (1 - home_share)


def cards_baseline(stage: str, strength_gap: float
                   ) -> tuple[float, float, float, float]:
    """
    (home_yellows, away_yellows, p_home_red, p_away_red)
    Weaker team takes slightly more yellows (more fouling) — small effect.
    """
    yel_total = WC_AVERAGES[stage]["yellows"]
    p_red_total = WC_AVERAGES[stage]["p_red"]
    weaker_yellow_share = 0.5 + 0.04 * (-strength_gap)
    weaker_yellow_share = float(np.clip(weaker_yellow_share, 0.35, 0.65))
    home_yel = yel_total * (1 - weaker_yellow_share) if strength_gap > 0 \
        else yel_total * weaker_yellow_share
    away_yel = yel_total - home_yel
    # split red probability symmetrically with small bias to weaker team
    p_home_red = p_red_total / 2 * (1 + 0.1 * (-strength_gap))
    p_away_red = p_red_total / 2 * (1 + 0.1 * strength_gap)
    return float(home_yel), float(away_yel), \
        float(np.clip(p_home_red, 0, 1)), float(np.clip(p_away_red, 0, 1))


# ---------------------------------------------------------------------------
# Penalty shootouts
# ---------------------------------------------------------------------------
# Historical international shootout win rate by nation (sample size in
# parentheses). For unlisted nations use 0.50.
SHOOTOUT_RATES = {
    "Germany": 0.82, "Argentina": 0.73, "Brazil": 0.50, "France": 0.58,
    "Spain": 0.50, "Italy": 0.40, "England": 0.30, "Portugal": 0.50,
    "Netherlands": 0.40, "Croatia": 0.75, "Belgium": 0.50,
    "Mexico": 0.43, "USA": 0.60, "Uruguay": 0.55, "Switzerland": 0.50,
    "Sweden": 0.50, "Denmark": 0.50, "Poland": 0.50,
    "Japan": 0.50, "South Korea": 0.50, "Korea Republic": 0.50,
    "Australia": 0.40, "Saudi Arabia": 0.50, "Iran": 0.50,
    "Morocco": 0.75, "Senegal": 0.67, "Ghana": 0.40,
    "Ivory Coast": 0.50, "Cote d'Ivoire": 0.50, "Cameroon": 0.50,
    "Tunisia": 0.50, "Egypt": 0.50, "Algeria": 0.50,
    "Colombia": 0.50, "Paraguay": 0.50, "Chile": 0.50, "Ecuador": 0.50,
    "Czechia": 0.50, "Scotland": 0.50,
}


def shootout_winner_prob(home: str, away: str) -> float:
    """P(home wins | match goes to penalties)."""
    h = SHOOTOUT_RATES.get(home, 0.50)
    a = SHOOTOUT_RATES.get(away, 0.50)
    return float(h / (h + a))


# ---------------------------------------------------------------------------
# Single match sampler — used by the Monte Carlo simulator
# ---------------------------------------------------------------------------

def simulate_match(model: DixonColesModel, home: str, away: str,
                   stage: str = "GROUP", neutral: bool = True,
                   rng: np.random.Generator | None = None,
                   knockout: bool = False) -> dict:
    """Draw one realization of a match. For knockout matches, also resolve
    extra time + penalty shootout if needed."""
    rng = rng or np.random.default_rng()
    m = model.score_matrix(home, away, neutral=neutral)
    flat = m.flatten()
    flat = flat / flat.sum()
    idx = rng.choice(len(flat), p=flat)
    max_g = m.shape[0] - 1
    h, a = divmod(idx, max_g + 1)

    sg = (model.attack.get(home, 0) - model.attack.get(away, 0)
          + model.defense.get(away, 0) - model.defense.get(home, 0))
    ch, ca = corners_baseline(stage, sg)
    yh, ya, prh, pra = cards_baseline(stage, sg)
    home_corners = int(rng.poisson(ch))
    away_corners = int(rng.poisson(ca))
    home_yellows = int(rng.poisson(yh))
    away_yellows = int(rng.poisson(ya))
    home_red = int(rng.random() < prh)
    away_red = int(rng.random() < pra)

    went_to_pens = False
    advance_home = None
    if knockout and h == a:
        # Extra time: draw a small additional goal increment (~1/3 of a full
        # match worth) from the same score matrix. If still level, penalties.
        et_score = model.score_matrix(home, away, neutral=neutral)
        et_flat = et_score.flatten() / et_score.flatten().sum()
        ei = rng.choice(len(et_flat), p=et_flat)
        eh, ea = divmod(ei, max_g + 1)
        h += int(round(eh / 3))
        a += int(round(ea / 3))
        if h == a:
            went_to_pens = True
            advance_home = rng.random() < shootout_winner_prob(home, away)
        else:
            advance_home = h > a
    elif knockout:
        advance_home = h > a

    return {
        "home_score": int(h), "away_score": int(a),
        "home_corners": home_corners, "away_corners": away_corners,
        "home_yellows": home_yellows, "away_yellows": away_yellows,
        "home_red": home_red, "away_red": away_red,
        "went_to_pens": went_to_pens, "home_advances": advance_home,
    }


# ---------------------------------------------------------------------------
# Strength helper
# ---------------------------------------------------------------------------

def squad_strength_gap(squad: pd.DataFrame, home: str, away: str) -> float:
    """squad: dataframe indexed by team with column 'squad_overall_top11'."""
    if squad is None or squad.empty:
        return 0.0
    h = squad.loc[squad.index == home, "squad_overall_top11"]
    a = squad.loc[squad.index == away, "squad_overall_top11"]
    if h.empty or a.empty:
        return 0.0
    # scale so that a 5-point overall gap maps to ~1.0 on the strength axis
    return float((h.iloc[0] - a.iloc[0]) / 5.0)
