-- World Cup 2026 Predictor — SQLite schema
-- Shared by Python and R notebooks. Single source of truth.

PRAGMA foreign_keys = ON;

-- Master team list with name aliases so historical data joins cleanly
CREATE TABLE IF NOT EXISTS teams (
    team_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL UNIQUE,
    confederation  TEXT,
    fifa_code      TEXT
);

CREATE TABLE IF NOT EXISTS team_aliases (
    alias       TEXT PRIMARY KEY,
    team_id     INTEGER NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(team_id)
);

-- Historical international match results (Kaggle 1872 - present)
CREATE TABLE IF NOT EXISTS historical_matches (
    match_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    match_date   TEXT NOT NULL,            -- ISO yyyy-mm-dd
    home_team    TEXT NOT NULL,
    away_team    TEXT NOT NULL,
    home_score   INTEGER NOT NULL,
    away_score   INTEGER NOT NULL,
    tournament   TEXT,                     -- 'FIFA World Cup', 'Friendly', 'UEFA Euro', ...
    city         TEXT,
    country      TEXT,
    neutral      INTEGER                   -- 1 if neutral venue
);

CREATE INDEX IF NOT EXISTS idx_hist_date ON historical_matches(match_date);
CREATE INDEX IF NOT EXISTS idx_hist_home ON historical_matches(home_team);
CREATE INDEX IF NOT EXISTS idx_hist_away ON historical_matches(away_team);

-- Per-match corners/cards (from FBref scrape, sparser coverage)
CREATE TABLE IF NOT EXISTS match_stats (
    match_id        INTEGER PRIMARY KEY,
    home_corners    INTEGER,
    away_corners    INTEGER,
    home_yellows    INTEGER,
    away_yellows    INTEGER,
    home_reds       INTEGER,
    away_reds       INTEGER,
    referee         TEXT,
    FOREIGN KEY (match_id) REFERENCES historical_matches(match_id)
);

-- FIFA monthly ranking snapshots
CREATE TABLE IF NOT EXISTS fifa_rankings (
    team           TEXT NOT NULL,
    rank_date      TEXT NOT NULL,
    rank           INTEGER,
    points         REAL,
    PRIMARY KEY (team, rank_date)
);

-- EA Sports FC player ratings (FC 26, with FC 25 fallback). Used to build
-- a squad-strength score per nation that complements the FIFA ranking.
CREATE TABLE IF NOT EXISTS player_ratings (
    player_id       INTEGER,
    player_name     TEXT,
    nationality     TEXT,
    club            TEXT,
    position        TEXT,
    age             INTEGER,
    overall_rating  INTEGER,
    potential       INTEGER,
    pace            INTEGER,
    shooting        INTEGER,
    passing         INTEGER,
    dribbling       INTEGER,
    defending       INTEGER,
    physic          INTEGER,
    game_version    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pr_nat ON player_ratings(nationality);

-- Derived squad-strength view (top-23 by overall rating per nation)
CREATE TABLE IF NOT EXISTS team_squad_strength (
    team                 TEXT PRIMARY KEY,
    squad_overall_mean   REAL,
    squad_overall_top11  REAL,
    squad_attack_mean    REAL,
    squad_defense_mean   REAL,
    squad_age_mean       REAL,
    n_players_top23      INTEGER
);

-- 2026 World Cup setup
CREATE TABLE IF NOT EXISTS wc2026_groups (
    group_id        TEXT NOT NULL,             -- 'A' .. 'L'
    team            TEXT NOT NULL,
    pot             INTEGER,
    confederation   TEXT,
    is_host         INTEGER DEFAULT 0,
    PRIMARY KEY (group_id, team)
);

CREATE TABLE IF NOT EXISTS wc2026_fixtures (
    fixture_id      INTEGER PRIMARY KEY,
    stage           TEXT NOT NULL,             -- 'GROUP','R32','R16','QF','SF','3RD','FINAL'
    group_id        TEXT,                      -- NULL for knockout
    match_date      TEXT,
    kickoff_local   TEXT,
    venue_city      TEXT,
    venue_country   TEXT,
    home_team       TEXT,                      -- NULL for not-yet-determined knockout slots
    away_team       TEXT,
    home_slot       TEXT,                      -- e.g. 'Winner Group A' before resolved
    away_slot       TEXT
);

-- Fitted model parameters, written by either notebook
CREATE TABLE IF NOT EXISTS model_params (
    model_name      TEXT NOT NULL,             -- 'dixon_coles','corners_poisson','cards_poisson'
    param_key       TEXT NOT NULL,             -- e.g. 'attack[Brazil]', 'home_advantage'
    param_value     REAL,
    fitted_by       TEXT,                      -- 'python' or 'r'
    fitted_at       TEXT,
    PRIMARY KEY (model_name, param_key, fitted_by)
);

-- Predicted outcome for every WC2026 match — written by both notebooks side-by-side
CREATE TABLE IF NOT EXISTS predictions (
    fixture_id          INTEGER NOT NULL,
    source              TEXT NOT NULL,         -- 'python' or 'r'
    modal_home_score    INTEGER,
    modal_away_score    INTEGER,
    p_home_win          REAL,
    p_draw              REAL,
    p_away_win          REAL,
    exp_home_corners    REAL,
    exp_away_corners    REAL,
    exp_home_yellows    REAL,
    exp_away_yellows    REAL,
    p_home_red          REAL,
    p_away_red          REAL,
    p_penalties         REAL,                  -- knockout only
    p_home_advances     REAL,                  -- knockout only
    generated_at        TEXT,
    PRIMARY KEY (fixture_id, source),
    FOREIGN KEY (fixture_id) REFERENCES wc2026_fixtures(fixture_id)
);

-- Tournament-level Monte Carlo results
CREATE TABLE IF NOT EXISTS tournament_sim (
    team                TEXT NOT NULL,
    source              TEXT NOT NULL,         -- 'python' or 'r'
    p_group_winner      REAL,
    p_group_runnerup    REAL,
    p_advance_r32       REAL,
    p_reach_r16         REAL,
    p_reach_qf          REAL,
    p_reach_sf          REAL,
    p_reach_final       REAL,
    p_champion          REAL,
    n_simulations       INTEGER,
    PRIMARY KEY (team, source)
);
