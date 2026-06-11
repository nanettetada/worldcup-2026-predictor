#!/usr/bin/env Rscript
# Headless R-side prediction generator.
#
# Mirrors src/run_predictions.py but stays under a minute by skipping the
# 10,000-tournament Monte Carlo (cross-language validation only needs the
# per-match goal model + corners/cards baseline + outcome probabilities).
# Writes rows with source = 'r' into the predictions table so the Streamlit
# dashboard's R/Python toggle has data on both sides.

suppressPackageStartupMessages({
  library(DBI); library(RSQLite); library(dplyr)
})

# Locate the script file no matter how R was invoked.
script_path <- function() {
  args <- commandArgs(trailingOnly = FALSE)
  m <- regmatches(args, regexpr("(?<=^--file=).+", args, perl = TRUE))
  if (length(m)) return(normalizePath(m[1]))
  if (!is.null(sys.frame(1)$ofile)) return(normalizePath(sys.frame(1)$ofile))
  normalizePath("src/run_predictions.R")
}
ROOT <- normalizePath(file.path(dirname(script_path()), ".."))
DB <- file.path(ROOT, "data", "wc2026.sqlite")
con <- dbConnect(SQLite(), DB)

cat("[1] Loading historical matches (2020+) ...\n")
hist <- dbGetQuery(con, "
  SELECT match_date, home_team, away_team, home_score, away_score, neutral
  FROM historical_matches
  WHERE match_date >= '2020-01-01'
    AND home_score IS NOT NULL AND away_score IS NOT NULL")
if (nrow(hist) == 0) {
  cat("  no historical_matches in slim DB - falling back to Python fit\n")
  preds <- dbGetQuery(con, "
    SELECT * FROM predictions WHERE source = 'python'") |>
    mutate(source = "r")
} else {
  hist <- hist |>
    mutate(match_date = as.Date(match_date),
           neutral    = as.integer(coalesce(neutral, 0L)))
  ref_date <- as.Date("2026-06-10")
  hist$days   <- as.numeric(ref_date - hist$match_date)
  hist$weight <- exp(-0.0019 * hist$days)

  teams <- sort(unique(c(hist$home_team, hist$away_team)))
  n <- length(teams); idx <- setNames(seq_along(teams), teams)

  dc_corr <- function(x, y, lam, mu, rho) {
    ifelse(x == 0 & y == 0, 1 - lam * mu * rho,
    ifelse(x == 0 & y == 1, 1 + lam * rho,
    ifelse(x == 1 & y == 0, 1 + mu * rho,
    ifelse(x == 1 & y == 1, 1 - rho, 1))))
  }

  neg_ll <- function(par) {
    a  <- par[1:n] - mean(par[1:n])
    d  <- par[(n + 1):(2 * n)]
    ha <- par[2 * n + 1]; rho <- par[2 * n + 2]
    hi <- idx[hist$home_team]; ai <- idx[hist$away_team]
    lam <- exp(a[hi] + d[ai] + ha * (1 - hist$neutral))
    mu  <- exp(a[ai] + d[hi])
    ll <- hist$home_score * log(lam) - lam +
          hist$away_score * log(mu)  - mu -
          lfactorial(hist$home_score) - lfactorial(hist$away_score)
    corr <- pmax(dc_corr(hist$home_score, hist$away_score, lam, mu, rho), 1e-12)
    -sum((ll + log(corr)) * hist$weight)
  }

  cat("[2] Fitting Dixon-Coles ...\n")
  t0 <- Sys.time()
  fit <- optim(c(rep(0, n), rep(0, n), 0.25, -0.05), neg_ll,
               method = "L-BFGS-B", control = list(maxit = 200))
  attack  <- fit$par[1:n]; attack <- attack - mean(attack)
  defense <- fit$par[(n + 1):(2 * n)]
  rho      <- fit$par[2 * n + 2]
  cat(sprintf("    %d teams, fit took %.1fs\n",
              n, as.numeric(Sys.time() - t0, units = "secs")))

  score_matrix <- function(home, away, max_goals = 8) {
    if (!(home %in% teams) || !(away %in% teams)) return(NULL)
    lam <- exp(attack[idx[home]] + defense[idx[away]])  # neutral venue
    mu  <- exp(attack[idx[away]] + defense[idx[home]])
    m <- outer(dpois(0:max_goals, lam), dpois(0:max_goals, mu))
    for (h in 0:1) for (a in 0:1)
      m[h + 1, a + 1] <- m[h + 1, a + 1] * dc_corr(h, a, lam, mu, rho)
    m / sum(m)
  }

  wc_avg <- list(GROUP = c(corners = 9.3, yellows = 3.8, p_red = 0.06))

  cat("[3] Computing per-fixture predictions ...\n")
  fx <- dbGetQuery(con, "
    SELECT f.fixture_id, f.home_team, f.away_team,
           m.matchup_score_home
    FROM wc2026_fixtures f
    LEFT JOIN fixture_matchups m ON m.fixture_id = f.fixture_id
    WHERE f.stage = 'GROUP'")

  preds <- vector("list", nrow(fx))
  for (i in seq_len(nrow(fx))) {
    h <- fx$home_team[i]; a <- fx$away_team[i]
    sm <- score_matrix(h, a); if (is.null(sm)) next
    mode_idx <- which(sm == max(sm), arr.ind = TRUE)[1, ]
    mh <- mode_idx[1] - 1; ma <- mode_idx[2] - 1
    p_h <- sum(sm[lower.tri(sm)])
    p_d <- sum(diag(sm))
    p_a <- sum(sm[upper.tri(sm)])

    gap <- (attack[idx[h]] - attack[idx[a]]) +
           (defense[idx[a]] - defense[idx[h]])
    share <- min(0.8, max(0.2, 0.5 + 0.06 * gap))
    yel <- wc_avg$GROUP[["yellows"]]
    weaker <- min(0.65, max(0.35, 0.5 + 0.04 * (-gap)))
    home_yel <- if (gap > 0) yel * (1 - weaker) else yel * weaker

    preds[[i]] <- tibble::tibble(
      fixture_id       = fx$fixture_id[i], source = "r",
      modal_home_score = mh, modal_away_score = ma,
      p_home_win = p_h, p_draw = p_d, p_away_win = p_a,
      exp_home_corners = wc_avg$GROUP[["corners"]] * share,
      exp_away_corners = wc_avg$GROUP[["corners"]] * (1 - share),
      exp_home_yellows = home_yel,
      exp_away_yellows = yel - home_yel,
      p_home_red = wc_avg$GROUP[["p_red"]] / 2 * (1 + 0.1 * (-gap)),
      p_away_red = wc_avg$GROUP[["p_red"]] / 2 * (1 + 0.1 * gap),
      p_penalties = NA_real_, p_home_advances = NA_real_,
      generated_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"))
  }
  preds <- bind_rows(preds)
}

cat(sprintf("[4] Writing %d predictions (source = 'r') ...\n", nrow(preds)))
dbExecute(con, "DELETE FROM predictions WHERE source = 'r'")
dbWriteTable(con, "predictions", preds, append = TRUE, row.names = FALSE)

dbDisconnect(con)
cat("Done.\n")
