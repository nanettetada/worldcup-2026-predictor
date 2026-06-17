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
        try:
            actuals = pd.read_sql("SELECT * FROM actual_results", conn)
        except Exception:
            actuals = pd.DataFrame()
    finally:
        conn.close()
    return dict(groups=groups, fixtures=fixtures, preds=preds,
                tsim=tsim, squad=squad, matchups=matchups, pos=pos,
                actuals=actuals)


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

@st.cache_data(ttl=300)
def available_sources() -> list[str]:
    if not DB.exists():
        return []
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT DISTINCT source FROM predictions").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


sources = available_sources() or ["python"]

with st.sidebar:
    st.header("Settings")
    source = st.radio("Predictions source", sources, horizontal=True,
                      index=0 if "python" in sources else 0)
    st.write(f"Tournament: 11 Jun – 19 Jul 2026")
    st.write(f"Database: `{DB.name}`")
    if len(sources) == 1:
        other = "r" if sources[0] == "python" else "python"
        st.caption(
            f"Only `{sources[0]}` predictions are loaded. To populate "
            f"`{other}`, run the matching notebook or "
            f"`Rscript src/run_predictions.R`.")

data = load_data(source)
if data.get("preds", pd.DataFrame()).empty:
    st.warning(
        f"No `{source}` predictions in the database yet. "
        f"Run the matching notebook first.")
    st.stop()

(tab_summary, tab_groups, tab_bracket,
 tab_match, tab_squad, tab_results, tab_method) = st.tabs([
    "Tournament", "Group Stage", "Knockout Bracket",
    "Match Detail", "Squad Matchup", "Results & Accuracy", "Methodology",
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

    def _favourite(r):
        best = max(r["p_home_win"], r["p_draw"], r["p_away_win"])
        if r["p_draw"] == best:
            return "Draw"
        return r["home_team"] if r["p_home_win"] == best else r["away_team"]
    score_df["favourite"] = score_df.apply(_favourite, axis=1)

    for c in ["p_home_win", "p_draw", "p_away_win"]:
        score_df[c] = (score_df[c] * 100).round(1)
    st.dataframe(
        score_df[["home_team", "away_team", "score", "favourite",
                  "p_home_win", "p_draw", "p_away_win"]]
        .rename(columns={"home_team": "Home", "away_team": "Away",
                         "score": "Modal score",
                         "favourite": "More likely to win",
                         "p_home_win": "P(home win) %",
                         "p_draw": "P(draw) %",
                         "p_away_win": "P(away win) %"}),
        hide_index=True, width=1200,
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
    if "p_home_advances" in ko.columns:
        ko["favourite"] = ko.apply(
            lambda r: (r["home_team"] if r["p_home_advances"] >= 0.5
                       else r["away_team"])
            if pd.notna(r.get("p_home_advances"))
            and pd.notna(r.get("home_team"))
            and pd.notna(r.get("away_team"))
            else "—",
            axis=1,
        )
    cols = ["stage", "home_slot", "away_slot", "home_team", "away_team",
            "modal_home_score", "modal_away_score", "favourite",
            "p_home_advances", "p_penalties"]
    show = ko[[c for c in cols if c in ko.columns]].rename(
        columns={"favourite": "More likely to win"})
    st.dataframe(show, hide_index=True, width=1300)


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


# ---------------------------------------------------------------------------
# Results & Accuracy tab
# ---------------------------------------------------------------------------

with tab_results:
    st.subheader("How the model is doing once results come in")
    st.caption(
        "Populated from `actual_results` in the database. Refresh with "
        "`python src/fetch_results.py` (pulls football-data.org and "
        "anything in `data/manual_results.json`).")

    actuals = data.get("actuals", pd.DataFrame())
    fixtures = data["fixtures"]
    preds = data["preds"]

    if actuals.empty or actuals["outcome"].notna().sum() == 0:
        st.info(
            "No completed matches in the database yet. Once the tournament "
            "is underway, run `python src/fetch_results.py` to load actual "
            "scores and this tab will start grading the predictions.")
    else:
        merged = (actuals
                  .merge(fixtures[["fixture_id", "stage", "group_id",
                                   "home_team", "away_team"]],
                         on="fixture_id", how="left")
                  .merge(preds[["fixture_id", "modal_home_score",
                                "modal_away_score",
                                "p_home_win", "p_draw", "p_away_win"]],
                         on="fixture_id", how="left"))
        finished = merged[merged["outcome"].notna()].copy()

        def predicted_outcome(r):
            best = max(r["p_home_win"], r["p_draw"], r["p_away_win"])
            if r["p_draw"] == best:
                return "D"
            return "H" if r["p_home_win"] == best else "A"

        finished["pred_outcome"] = finished.apply(predicted_outcome, axis=1)
        finished["pred_p_actual"] = finished.apply(
            lambda r: (r["p_home_win"] if r["outcome"] == "H"
                       else r["p_draw"] if r["outcome"] == "D"
                       else r["p_away_win"]),
            axis=1)
        finished["correct"] = finished["pred_outcome"] == finished["outcome"]
        finished["exact_score"] = (
            (finished["modal_home_score"] == finished["home_score"])
            & (finished["modal_away_score"] == finished["away_score"]))

        # Top-line metrics
        n = len(finished)
        acc = finished["correct"].mean()
        exact = finished["exact_score"].mean()
        # Brier score: sum (p_i - y_i)^2 across three outcomes
        b = ((finished["p_home_win"] - (finished["outcome"] == "H")) ** 2
             + (finished["p_draw"] - (finished["outcome"] == "D")) ** 2
             + (finished["p_away_win"] - (finished["outcome"] == "A")) ** 2)
        brier = b.mean()
        # Goal RMSE
        from math import sqrt
        goal_rmse = sqrt((
            (finished["modal_home_score"] - finished["home_score"]) ** 2
            + (finished["modal_away_score"] - finished["away_score"]) ** 2
        ).mean())

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Matches graded", f"{n}")
        c2.metric("Outcome accuracy", f"{acc*100:.1f}%",
                  help="Share of matches where the highest-probability "
                       "outcome (home / draw / away) was the actual one.")
        c3.metric("Exact-score hits", f"{exact*100:.1f}%",
                  help="Share of matches where the modal score line "
                       "exactly matched the final result.")
        c4.metric("Brier score", f"{brier:.3f}",
                  help="Lower is better. Uniform 1/3 guess scores ~0.667; "
                       "a perfect call scores 0.")
        st.caption(f"Goal RMSE (modal vs actual, per side): {goal_rmse:.2f}")

        # Per-match table
        st.markdown("**Per-match grading**")
        WINNER_MAP = {"H": "home", "D": "draw", "A": "away"}
        view = finished.copy()
        view["Date"] = view["match_date"].fillna("")
        view["Stage"] = view["stage"]
        view["Match"] = (view["home_team"] + " vs " + view["away_team"])
        view["Predicted"] = (view["modal_home_score"].astype("Int64").astype(str)
                             + " – "
                             + view["modal_away_score"].astype("Int64").astype(str))
        view["Actual"] = (view["home_score"].astype("Int64").astype(str)
                          + " – "
                          + view["away_score"].astype("Int64").astype(str))
        view["Favourite"] = view.apply(
            lambda r: ("Draw" if r["pred_outcome"] == "D"
                       else r["home_team"] if r["pred_outcome"] == "H"
                       else r["away_team"]),
            axis=1)
        view["Winner"] = view.apply(
            lambda r: ("Draw" if r["outcome"] == "D"
                       else r["home_team"] if r["outcome"] == "H"
                       else r["away_team"]),
            axis=1)
        view["Hit?"] = view["correct"].map({True: "yes", False: "no"})
        view["P(actual) %"] = (view["pred_p_actual"] * 100).round(1)
        st.dataframe(
            view[["Date", "Stage", "Match", "Predicted", "Actual",
                  "Favourite", "Winner", "Hit?", "P(actual) %"]]
            .sort_values("Date"),
            hide_index=True, width=1400,
        )

        # Outcome-level breakdown
        st.markdown("**Calibration by predicted favourite**")
        cal = (finished.assign(bucket=pd.cut(
            finished["pred_p_actual"], bins=[0, .4, .55, .7, 1.0],
            labels=["≤40%", "40–55%", "55–70%", ">70%"], include_lowest=True))
            .groupby("bucket")
            .agg(n=("correct", "size"),
                 hit_rate=("correct", "mean"),
                 mean_predicted=("pred_p_actual", "mean"))
            .reset_index())
        cal["hit_rate"] = (cal["hit_rate"] * 100).round(1)
        cal["mean_predicted"] = (cal["mean_predicted"] * 100).round(1)
        cal = cal.rename(columns={
            "bucket": "Confidence band",
            "n": "Matches",
            "mean_predicted": "Avg predicted %",
            "hit_rate": "Actual hit rate %"})
        st.dataframe(cal, hide_index=True, width=700)
        st.caption(
            "If the model is well-calibrated, 'Actual hit rate %' tracks "
            "'Avg predicted %' inside each band. Sample sizes are small "
            "early in the tournament — read with a grain of salt.")


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
