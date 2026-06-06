"""
Streamlit dashboard for the World Cup 2026 predictor.

    streamlit run app/dashboard.py

Reads predictions and tournament-sim results from data/wc2026.sqlite.
Both the Python and R notebooks write into the same database; this app
displays whichever source is asked for via the sidebar (defaults to python).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "wc2026.sqlite"

st.set_page_config(
    page_title="World Cup 2026 Predictor",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def load_data(source: str) -> dict:
    if not DB.exists():
        return {}
    conn = sqlite3.connect(DB)
    try:
        groups = pd.read_sql("SELECT * FROM wc2026_groups", conn)
        fixtures = pd.read_sql("SELECT * FROM wc2026_fixtures", conn)
        try:
            matchups = pd.read_sql("SELECT * FROM fixture_matchups", conn)
        except Exception:
            matchups = pd.DataFrame()
        try:
            pos = pd.read_sql("SELECT * FROM team_position_strength", conn)
        except Exception:
            pos = pd.DataFrame()
        try:
            preds = pd.read_sql(
                "SELECT * FROM predictions WHERE source = ?",
                conn, params=(source,))
        except Exception:
            preds = pd.DataFrame()
        try:
            tsim = pd.read_sql(
                "SELECT * FROM tournament_sim WHERE source = ?",
                conn, params=(source,)
            ).sort_values("p_champion", ascending=False)
        except Exception:
            tsim = pd.DataFrame()
        try:
            squad = pd.read_sql(
                "SELECT * FROM team_squad_strength "
                "ORDER BY squad_overall_top11 DESC", conn)
        except Exception:
            squad = pd.DataFrame()
    finally:
        conn.close()
    return dict(groups=groups, fixtures=fixtures, preds=preds,
                tsim=tsim, squad=squad, matchups=matchups, pos=pos)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

st.title("World Cup 2026 — Statistical Predictor")
st.caption(
    "Predictions built from international match results 1872 – present, "
    "FIFA world rankings, and EA Sports FC 26 squad ratings. "
    "Model: Dixon – Coles bivariate Poisson with time-decay weighting."
)

if not DB.exists():
    st.error(f"Database not found: {DB.relative_to(ROOT)}. "
             f"Run `python src/build_db.py` first.")
    st.stop()

with st.sidebar:
    st.header("Settings")
    source = st.radio("Predictions source",
                      ["python", "r"], horizontal=True)
    st.write(f"Tournament: 11 Jun – 19 Jul 2026")
    st.write(f"Database: `{DB.name}`")

data = load_data(source)
if data.get("preds", pd.DataFrame()).empty:
    st.warning(
        f"No `{source}` predictions in the database yet. "
        f"Run the matching notebook first.")
    st.stop()

(tab_summary, tab_groups, tab_bracket,
 tab_match, tab_squad, tab_method) = st.tabs([
    "Tournament", "Group Stage", "Knockout Bracket",
    "Match Detail", "Squad Matchup", "Methodology",
])


# ---------------------------------------------------------------------------
# Tournament tab
# ---------------------------------------------------------------------------

with tab_summary:
    st.subheader("Championship probability")
    tsim = data["tsim"]
    if tsim.empty:
        st.info("Run the Monte Carlo simulation in the notebook to populate.")
    else:
        col1, col2 = st.columns([2, 3])
        with col1:
            top = tsim.head(12)[["team", "p_champion"]].copy()
            top["p_champion"] = (top["p_champion"] * 100).round(1)
            top = top.rename(columns={"p_champion": "% to win"})
            st.dataframe(top, hide_index=True, width=400)
        with col2:
            chart_df = tsim.head(15).set_index("team")[
                ["p_champion", "p_reach_final", "p_reach_sf"]] * 100
            st.bar_chart(chart_df, stack=False)

        st.subheader("Tournament journey probabilities")
        st.dataframe(
            (tsim[["team", "p_advance_r32", "p_reach_r16", "p_reach_qf",
                   "p_reach_sf", "p_reach_final", "p_champion"]]
             .head(20)
             .assign(**{c: lambda d, c=c: (d[c] * 100).round(1)
                        for c in ["p_advance_r32", "p_reach_r16",
                                  "p_reach_qf", "p_reach_sf",
                                  "p_reach_final", "p_champion"]})),
            hide_index=True,
        )


# ---------------------------------------------------------------------------
# Group stage tab
# ---------------------------------------------------------------------------

with tab_groups:
    st.subheader("Group-stage predictions")
    groups = data["groups"]
    fixtures = data["fixtures"]
    preds = data["preds"]

    group_id = st.selectbox(
        "Pick a group",
        sorted(groups["group_id"].unique()),
        index=0,
    )
    grp_teams = groups[groups["group_id"] == group_id]["team"].tolist()
    st.write("**Teams:** " + ", ".join(grp_teams))

    grp_fx = fixtures.merge(
        preds, on="fixture_id", how="left", suffixes=("", "_p"))
    grp_fx = grp_fx[grp_fx["group_id"] == group_id]

    # ---- 1. Score and outcome --------------------------------------
    st.markdown("**Score and outcome**")
    score_df = grp_fx[["home_team", "away_team",
                       "modal_home_score", "modal_away_score",
                       "p_home_win", "p_draw", "p_away_win"]].copy()
    score_df["score"] = (score_df["modal_home_score"].astype(int).astype(str)
                         + " – "
                         + score_df["modal_away_score"].astype(int).astype(str))
    for c in ["p_home_win", "p_draw", "p_away_win"]:
        score_df[c] = (score_df[c] * 100).round(1)
    st.dataframe(
        score_df[["home_team", "away_team", "score",
                  "p_home_win", "p_draw", "p_away_win"]]
        .rename(columns={"home_team": "Home", "away_team": "Away",
                         "score": "Modal score",
                         "p_home_win": "P(home win) %",
                         "p_draw": "P(draw) %",
                         "p_away_win": "P(away win) %"}),
        hide_index=True, width=1100,
    )

    # ---- 2. Corners ------------------------------------------------
    st.markdown("**Corners (expected count)**")
    corners_df = grp_fx[["home_team", "away_team",
                         "exp_home_corners", "exp_away_corners"]].copy()
    corners_df["total"] = (corners_df["exp_home_corners"]
                           + corners_df["exp_away_corners"])
    for c in ["exp_home_corners", "exp_away_corners", "total"]:
        corners_df[c] = corners_df[c].round(1)
    st.dataframe(
        corners_df.rename(columns={
            "home_team": "Home", "away_team": "Away",
            "exp_home_corners": "Home corners",
            "exp_away_corners": "Away corners",
            "total": "Match total"}),
        hide_index=True, width=900,
    )

    # ---- 3. Cards --------------------------------------------------
    st.markdown("**Cards (expected yellows, red-card probability)**")
    cards_df = grp_fx[["home_team", "away_team",
                       "exp_home_yellows", "exp_away_yellows",
                       "p_home_red", "p_away_red"]].copy()
    for c in ["exp_home_yellows", "exp_away_yellows"]:
        cards_df[c] = cards_df[c].round(1)
    for c in ["p_home_red", "p_away_red"]:
        cards_df[c] = (cards_df[c] * 100).round(1)
    st.dataframe(
        cards_df.rename(columns={
            "home_team": "Home", "away_team": "Away",
            "exp_home_yellows": "Home yellows",
            "exp_away_yellows": "Away yellows",
            "p_home_red": "P(home red) %",
            "p_away_red": "P(away red) %"}),
        hide_index=True, width=1100,
    )


# ---------------------------------------------------------------------------
# Knockout bracket tab
# ---------------------------------------------------------------------------

with tab_bracket:
    st.subheader("Knockout bracket")
    fixtures = data["fixtures"]
    preds = data["preds"]
    ko = fixtures[fixtures["stage"].isin(
        ["R32", "R16", "QF", "SF", "3RD", "FINAL"])]
    ko = ko.merge(preds, on="fixture_id", how="left")
    cols = ["stage", "home_slot", "away_slot", "home_team", "away_team",
            "modal_home_score", "modal_away_score",
            "p_home_advances", "p_penalties"]
    show = ko[[c for c in cols if c in ko.columns]]
    st.dataframe(show, hide_index=True, width=1200)


# ---------------------------------------------------------------------------
# Match detail tab
# ---------------------------------------------------------------------------

with tab_match:
    st.subheader("Per-match deep dive")
    preds = data["preds"].merge(data["fixtures"], on="fixture_id", how="left")
    preds["label"] = (preds["stage"] + " — " + preds["home_team"].fillna(
        preds["home_slot"]) + " vs " + preds["away_team"].fillna(
        preds["away_slot"]))
    pick = st.selectbox("Match", preds["label"].tolist())
    row = preds[preds["label"] == pick].iloc[0]

    cols = st.columns(3)
    cols[0].metric(
        "Modal score",
        f"{int(row['modal_home_score'])} – {int(row['modal_away_score'])}")
    cols[1].metric("P(home win)", f"{row['p_home_win']*100:.1f}%")
    cols[2].metric("P(away win)", f"{row['p_away_win']*100:.1f}%")

    cols = st.columns(3)
    cols[0].metric("P(draw)", f"{row['p_draw']*100:.1f}%")
    cols[1].metric("Corners (home / away)",
                   f"{row['exp_home_corners']:.1f} / {row['exp_away_corners']:.1f}")
    cols[2].metric("Match total corners",
                   f"{row['exp_home_corners'] + row['exp_away_corners']:.1f}")

    st.markdown("**Cards**")
    cols = st.columns(4)
    cols[0].metric("Yellows — home", f"{row['exp_home_yellows']:.1f}")
    cols[1].metric("Yellows — away", f"{row['exp_away_yellows']:.1f}")
    cols[2].metric("P(red — home)", f"{row['p_home_red']*100:.1f}%")
    cols[3].metric("P(red — away)", f"{row['p_away_red']*100:.1f}%")

    if row["stage"] != "GROUP" and pd.notna(row.get("p_penalties")):
        st.info(
            f"Knockout match: P(goes to penalties) = "
            f"{row['p_penalties']*100:.1f}%, "
            f"P(home advances) = {row['p_home_advances']*100:.1f}%")

    matchups = data.get("matchups", pd.DataFrame())
    if not matchups.empty and row["fixture_id"] in matchups["fixture_id"].values:
        mu = matchups[matchups["fixture_id"] == row["fixture_id"]].iloc[0]
        st.markdown("**Position-level matchup (EA FC 26 top-5 per position)**")
        cols = st.columns(4)
        cols[0].metric("Home attack vs away defense",
                       f"{mu['attack_edge_home']:+.1f}")
        cols[1].metric("Away attack vs home defense",
                       f"{mu['attack_edge_away']:+.1f}")
        cols[2].metric("Midfield balance (home – away)",
                       f"{mu['midfield_balance']:+.1f}")
        cols[3].metric("GK advantage (home – away)",
                       f"{mu['gk_advantage_home']:+.1f}")
        st.caption(
            f"Composite matchup score (home): **{mu['matchup_score_home']:+.1f}**. "
            "Positive numbers favour the home side. This signal feeds the "
            "goal model — see Methodology.")


# ---------------------------------------------------------------------------
# Methodology tab
# ---------------------------------------------------------------------------

with tab_squad:
    st.subheader("Squad strength by position")
    pos = data.get("pos", pd.DataFrame())
    matchups = data.get("matchups", pd.DataFrame())
    fixtures = data["fixtures"]

    if pos.empty:
        st.info("Position-strength table not built yet. "
                "Run `python src/build_db.py` to populate.")
    else:
        wide = pos.pivot(index="team", columns="position_group",
                         values="top5_overall").reset_index()
        wide["Composite"] = (wide[["GK", "DEF", "MID", "FWD"]].mean(axis=1)
                             .round(2))
        wide = wide.sort_values("Composite", ascending=False)
        for c in ["GK", "DEF", "MID", "FWD"]:
            if c in wide.columns:
                wide[c] = wide[c].round(1)
        st.dataframe(
            wide.rename(columns={"team": "Team"}).head(48),
            hide_index=True, width=900,
        )

        st.subheader("Biggest matchup edges (group stage)")
        if not matchups.empty:
            fx = fixtures[fixtures["stage"] == "GROUP"][
                ["fixture_id", "home_team", "away_team", "group_id"]]
            edge = fx.merge(matchups, on="fixture_id", how="left")
            edge["abs_score"] = edge["matchup_score_home"].abs()
            top = edge.nlargest(10, "abs_score")[
                ["group_id", "home_team", "away_team",
                 "attack_edge_home", "attack_edge_away",
                 "midfield_balance", "gk_advantage_home",
                 "matchup_score_home"]]
            for c in ["attack_edge_home", "attack_edge_away",
                      "midfield_balance", "gk_advantage_home",
                      "matchup_score_home"]:
                top[c] = top[c].round(1)
            st.dataframe(top, hide_index=True, width=1100)


with tab_method:
    st.markdown("""
### How the predictions are made

**Goal model.** Dixon – Coles bivariate Poisson, fitted on all international
matches since 2014 (~10 000 games). Exponential time-decay weight with
ξ = 0.0019 / day, the value Dixon & Coles (1997) propose for football.
Outputs a full goal probability matrix per fixture.

**Strength feature.** Blend of two signals — EA Sports FC 26 top-11 squad
overall (60 %) and FIFA world ranking points (40 %). EA leans heavier
because it incorporates club-level performance for each player.

**Corners and cards.** Tournament-average baselines from the last three
World Cups (2014, 2018, 2022), scaled by the team-strength gap. This is a
baseline, not a precision model — public per-match international data for
corners and cards is sparse.

**Penalty shootouts.** When a knockout match is level after regulation +
extra time, the winner is drawn from each nation's historical shootout
conversion rate. Germany, Croatia, Argentina lead; England, Mexico
historically trail.

**Tournament simulation.** 10 000 Monte Carlo runs of the entire
tournament. Group standings tie-broken by points → goal difference →
goals for. Top 2 of each group plus the 8 best third-placed teams advance.

**Honest accuracy expectations**
- Exact-score hit rate: ≈ 12–15 % at best (this is the cap for any model).
- Outcome (W/D/L) hit rate: ≈ 55–60 %.
- Probability calibration is the real measure — judge by Brier score and
  log-loss, not raw accuracy.
""")
