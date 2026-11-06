# Phase 1 Review Findings — pre-commit inspection

Date: 2026-11-06 · Status: **3 defects found and fixed; NOT committed**

These are the results of inspecting the three areas flagged for review. All three surfaced real
defects. Each fix has a regression test. The commit decision remains the operator's.

## 1. Price integrity — DEFECT FOUND (high severity)

**Claim under review:** the live CLI reported a DFNS quote of **$5,000 per share**.

Investigation:
- Yahoo's own metadata for `DFNS` (exchange NGM) shows `regularMarketPrice = 2.29` on 2026-11-05.
- The $5,000 came from a **cached 2024-12-30 bar** left behind by the historical replay, which had
  used the same `data/cache/` directory as live quotes. The real current price is ~$2.29.
- Root cause: one cache directory served two purposes (live quotes and historical windows), and
  `latest_quote` returned `bars[-1]` with no age check. A stale entry therefore passed as live.

Fix:
- `latest_quote(..., max_age_days=7)` now returns `None` for a bar older than the limit.
- The replay harness caches into `data/cache/replay/`, isolated from live quotes.
- Regression tests: stale bar rejected, recent bar accepted, unparseable date rejected.

Verified: DFNS now reports **$2.29** (2026-11-05). The "successful API response" was valid data —
just for the wrong date. A valid response is not a valid *current* price.

## 2. Price-basis consistency — DEFECT FOUND (medium severity)

**Claim under review:** historical adjusted closes, raw closes, and live quotes cannot be mixed.

Investigation:
- Valuation (`report.py`) used the **raw close**; gain/erosion alerts (`monitor.py`) used
  **adj_close**. For dividend payers these differ (measured up to −17.5% on VTI, −7.6% on PG), so
  a position's snapshot gain and its gain alert could disagree by several points.

Fix:
- One basis everywhere: the as-printed **split-adjusted close**. `monitor._closes`, `gain_alerts`
  and `erosion_alerts` now use `close`. `adj_close` is retained only for audit/verification.
- Regression test: for a synthetic dividend payer (close 200 vs adj_close 190), the snapshot
  unrealized and the alert gain both read +100%.

Note: `close` is already split-adjusted by the source, so splits remain correct (DFNS 1:125 and
1:8 both verified); the only difference between the two fields is dividend reinvestment.

## 3. Fail-closed behavior — DEFECT FOUND (medium severity)

**Claim under review:** a failed fetch, partial portfolio, malformed cache, or stale quote must
never produce an apparently healthy report.

Investigation — one path was unsafe:
- The staleness gate checked only a **single overall** `price_as_of`. The CLI computed it as the
  **maximum** date across tickers, so one fresh ticker (VTI, 2026-11-05) masked a stale one
  (DFNS, 2024-12-30). Reproduced: report status `WARN`, `trustworthy=True` — **not blocked**.

Fix:
- `build_report` accepts per-ticker `price_dates` and blocks if **any** ticker's price is stale,
  future-dated or malformed. The CLI passes every ticker's date.
- Regression test: fresh VTI + stale DFNS now yields `BLOCKED`.

Verified fail-closed on the real CLI path:

| Scenario | Result |
|---|---|
| Malformed cache JSON | `latest_quote -> None` |
| Unknown ticker, no cache | `latest_quote -> None` |
| Partial portfolio (1 good, 1 bad) | **BLOCKED**, "no fresh quote for ZZZZNOPE" |
| Stale price for one ticker | **BLOCKED**, "stale price for DFNS" |
| No portfolio file | refuses, exit 2 |

## 4. Price-sanity guard (added per operator request)

A fourth defect class was closed proactively: a plausible-looking but wrong price. The guard
**flags suspicious quotes for verification; it does not blindly reject large moves.**

Design (`src/apex_investor/sanity.py`, `check_quote`):
- Compares the latest quote with the previous valid close.
- Configurable thresholds (`PriceSanityLimits`): `warn_move=0.35`, `block_move=0.90`,
  `split_tolerance=0.06`.
- Checks for split/corporate-action explanations: a factor near a common split ratio is treated
  as "consistent with a split" (flag, verify); an operator-supplied `data/known_actions.json`
  can explain a move outright.
- Blocks valuation and risk **only** when the move is extreme and unexplained. A blocked quote
  is dropped from valuation so an unvalidated number cannot flow into equity or risk.
- Preserves the quote and its evidence; flagged checks are rendered in the report.

Outcomes:

| Status | Meaning | Effect |
|---|---|---|
| OK | normal move, or explained by a known action | valued normally |
| SUSPECT | unusually large, or consistent with a split | flagged for verification; still valued |
| UNVERIFIABLE | no previous close to compare | noted; staleness gate still applies |
| BLOCKED | extreme, unexplained move | valuation and risk blocked; evidence preserved |

Verified on the real CLI path:

| Scenario | Result |
|---|---|
| −91% unexplained move | **BLOCKED**; price dropped from valuation; evidence preserved |
| +50% earnings gap | **SUSPECT**; valued; "verify" alert; report proceeds |
| 1:8 reverse split factor | **SUSPECT**; flagged as consistent with a reverse split |
| move matching a known action | **OK** |

## 5. Example portfolio cannot become the live ledger

- `--portfolio` no longer defaults to the example file. The default is `data/portfolio.json`
  (the operator's real snapshot, git-ignored). Running with no real snapshot **refuses**.
- `--example` is required to run the demo, and prints a stderr notice.
- `data/portfolio.example.json` is labelled EXAMPLE ONLY and contains no operator data.
- `data/portfolio.json` is git-ignored.

## 6. Git diff and credentials

- `apex-investor` has **no commits**; nothing staged. Apex Trader untouched at `e33c926`.
- Secret scan of source, tests, config and docs: no keys, tokens or credentials.
- `.gitignore` excludes `.venv`, `__pycache__`, `data/cache/`, `data/portfolio.json`, `*.db`.

## Verification after fixes

- `pytest tests/` — **109 passed** (72 at first review; +5 for the three review fixes, +15 for the
  price-sanity guard, +17 for the second-review findings below).
- `ruff check .` — All checks passed.
- Replay acceptance — DFNS `extraordinary_gain` on 2024-12-17 **PASS**; 10/10 panel evaluated.

## 7. Second review — four findings fixed

A second operator review of the pasted source found four defects and two improvements. All fixed
with regression tests.

**Finding 1 (critical): a known corporate action could incorrectly approve a price.**
`check_quote` matched an action on ticker+date but never checked that the action's stated ratio
agreed with the observed price move. A declared ratio of 0.1 against an observed factor of 10 was
accepted as `OK`. Fixed: `_ratio_matches` requires the declared ratio to be a finite positive
number within `split_tolerance` of the observed factor. A mismatch no longer explains the move —
normal rules apply (SUSPECT/BLOCKED), and the reason names the mismatch. Malformed entries
(non-dict, bad date, zero/negative/NaN ratio) are skipped, not trusted.
Tests: `test_known_action_ratio_that_does_not_match_does_not_explain`,
`test_known_action_missing_ratio_cannot_explain`, `test_malformed_known_action_entries_are_ignored`,
`test_known_action_ratio_near_factor_within_tolerance_is_ok`.

**Finding 2: an UNVERIFIABLE quote could still produce an OK report.**
`DailyReport.status` only degraded on SUSPECT, so a portfolio whose price had no previous close
could read `OK` while the summary said the price could not be checked. Fixed: an UNVERIFIABLE
quote also degrades the report to `WARN`, while keeping the SUSPECT/UNVERIFIABLE distinction
(separate `unverified_tickers` and `unchecked_tickers` properties).
Test: `test_unverifiable_quote_degrades_report_to_warn`.

**Finding 3: invalid risk-limit config could crash instead of failing closed.**
`assess` checked that drawdown limits were finite, then compared them with `<=`; a string limit
raised `TypeError`. Fixed: type/range validation runs first, and the drawdown comparison only runs
when both thresholds are valid finite numbers. Out-of-range values are also blocked.
Tests: `test_non_numeric_drawdown_limits_block_without_crashing`, `test_out_of_range_limits_block`,
`test_both_drawdown_limits_non_numeric_does_not_crash`.

**Finding 4: repeated regeneration could overwrite a backup.**
The backup filename was derived from the timestamp; two regenerations with the same timestamp
overwrote the first backup. Fixed: `_free_backup_path` appends a numeric suffix until the path is
free, so no backup is ever overwritten.
Test: `test_repeated_regeneration_never_overwrites_a_backup`.

**Improvement 5: live path can refresh a stale cache.**
`fetch_bars` returned an existing cache without refreshing, so a stale cache could block a report a
fresh fetch could satisfy. Fixed: an optional `max_cache_age_days` policy makes the live path
refetch a stale cache; if the refresh fails the cached bars are returned and the caller's staleness
check decides. Historical replay passes no policy, so it stays isolated and offline.
Tests: `test_fetch_bars_returns_cache_without_network_when_fresh`,
`test_fetch_bars_refreshes_stale_cache`, `test_fetch_bars_keeps_stale_cache_when_refresh_fails`,
`test_fetch_bars_does_not_refresh_without_policy`.

**Improvement 6: every held ticker needs its own price date.**
`build_report` checked the dates it was given but did not require a date for every position. Fixed:
when `price_dates` is supplied, every held ticker must appear or the report is blocked.
Test: `test_missing_per_ticker_date_blocks_the_report`.

### CLI verification (second review)

| Scenario | Result |
|---|---|
| Malformed / mismatched corporate action (ratio 0.1 vs factor 10) | **SUSPECT**, reason names the mismatch |
| Missing previous close | **UNVERIFIABLE**; report degrades to **WARN**, not OK |
| Invalid drawdown limits (string) | blocked verdict, **no TypeError** |
| Out-of-range limit (max_position_weight 1.5) | blocked, "out of range" |
| Repeated regeneration, identical timestamp | both backups preserved |

## 8. Third review — two more gaps closed

A third review asked two precise questions. Both surfaced real gaps.

**Gap A: omitting `price_dates` entirely bypassed per-ticker freshness.**
The per-ticker check ran only inside `if price_dates is not None`. A caller that omitted
`price_dates` while holding positions skipped the requirement. Fixed: if `price_dates` is omitted
while positions are held, the report is blocked ("no per-ticker price dates supplied; per-ticker
freshness cannot be verified"). An empty ledger is exempt (the requirement is vacuous). The CLI
already supplies `price_dates`, so its behavior is unchanged.
Tests: `test_omitted_price_dates_with_positions_blocks`,
`test_omitted_price_dates_with_empty_ledger_is_not_blocked_for_that_reason`.

**Gap B: the documented corporate-action ratio example was the wrong direction.**
The CLI comment paired `ratio 0.125` with "1:8 reverse split". The price factor for a 1:8 reverse
split is `8.0` (the price rises); `0.125` is the inverse and would not explain the move. The code
was already correct — direction is checked — but the operator-facing example would have led to a
wrong entry. Fixed: the comment now states that `ratio` is the price factor (new/old), gives
`8.0` for a 1:8 reverse split and `0.25` for a 4:1 forward split, and notes that a non-matching
ratio does not explain the move.
Tests: `test_reverse_split_ratio_must_be_greater_than_one`,
`test_forward_split_ratio_must_be_less_than_one`, `test_action_ratio_far_from_factor_does_not_explain`,
`test_action_ratio_outside_tolerance_does_not_explain`.

### Mechanism notes requested by the operator

- **`factor` and `ratio` definition.** `factor = latest_close / previous_close` (new/old price).
  A reverse split raises the price, so its factor and required `ratio` are `> 1`; a forward split
  lowers it, so both are `< 1`. The declared `ratio` is validated with
  `abs(factor - ratio) <= split_tolerance * ratio`, so the inverse direction and unrelated moves
  are rejected (verified live: 1:8 reverse with `ratio 8.0` → OK, with `ratio 0.125` → not OK;
  4:1 forward with `ratio 0.25` → OK, with `ratio 4.0` → not OK).
- **Cache refresh / fallback.** `fetch_bars(max_cache_age_days=N)` returns a cache whose newest bar
  is within N days; otherwise it refetches. If the refetch fails and a cache exists, the cached
  bars are returned (the caller's staleness check then decides); if no cache exists it raises, and
  the caller treats that as "no fresh quote". Verified live: stale cache + successful fetch →
  refreshed price; stale cache + failed fetch → cached price; no cache + failed fetch → raises.

## Verification after all fixes

- `pytest tests/` — **147 passed**.
- `ruff check .` — All checks passed.
- Replay acceptance — DFNS `extraordinary_gain` on 2024-12-17 **PASS**; 10/10 panel evaluated.

## 9. Fourth review — three findings + one hardening

**Finding 1: an action with no ticker could explain any holding.**
`_find_action` accepted `act.get("ticker") is None`, so an action missing a ticker authorized a
large move for any symbol. Fixed: an exact ticker match is required; a missing or different ticker
never applies.
Tests: `test_action_without_ticker_does_not_explain_any_holding`,
`test_action_for_other_ticker_still_does_not_apply`.

**Finding 2: `PriceSanityLimits` was not validated.**
A string `warn_move` could reach `mag < lim.warn_move` and raise a TypeError. Fixed: `_valid_limits`
checks every threshold is a finite number in range and that `warn_move < block_move`; an invalid
config yields a BLOCKED check with a clear reason, before any comparison.
Tests: `test_string_warn_move_blocks_instead_of_raising`, `test_nan_block_move_blocks`,
`test_out_of_range_sanity_limit_blocks`, `test_inverted_sanity_thresholds_block`,
`test_valid_limits_are_unaffected`.

**Finding 3: future-dated prices could pass as fresh.**
The cache check and `latest_quote` only rejected bars that were too old, so a future-dated bar
(negative age) was treated as fresh. Fixed: all three freshness checks (`fetch_bars` cache,
`latest_quote`, CLI `_fresh_latest`) now require `0 <= age <= max`, rejecting future dates.
Tests: `test_latest_quote_rejects_future_dated_bar`,
`test_fetch_bars_refreshes_future_dated_cache`.

**Hardening: concurrent regeneration could race on a backup filename.**
`_free_backup_path` checked existence then wrote, so two processes could pick the same name. Fixed:
`_write_backup_exclusive` creates the backup with `O_CREAT|O_EXCL`, retrying with a numeric suffix
on collision. Verified with 8 concurrent processes: 8 distinct files, no overwrite.
Test: `test_concurrent_backups_do_not_overwrite`.

## Open items for the operator

1. Review the fixes above and decide on the commit.
2. Provide a real holdings-and-cash snapshot (`data/portfolio.json`) before the live ledger is used.
3. The live ledger remains **empty**; no broker is connected.
4. `data/known_actions.json` is optional and operator-supplied; absent by default.

## 10. Fifth review — four findings

**Finding 1: the CLI could write a misleading status after `build_report`.**
`main()` appended missing-quote info to `report.risk` *after* the report was built. `status` is a
property (so it recomputed), but the post-hoc mutation was fragile and never raised the
`risk_check_incomplete` alert. Fixed: `build_report` takes `missing_tickers` and `config_problems`
and folds them into the staleness/config gates, so status, alerts and the blocked list all derive
from one consistent final state. `main()` no longer edits the report.
Tests: `test_missing_ticker_blocks_the_report_and_derives_status`,
`test_cli_missing_quote_blocks_status`, `test_cli_missing_quote_blocks_via_api`.

**Finding 2: the raw prices themselves were not validated.**
Only a non-positive previous close was handled. Fixed: `check_quote` validates both closes with
`_valid_price` before any arithmetic. A malformed **latest** price (zero, negative, NaN, ±inf,
str, bool, None) is BLOCKED and dropped from valuation; a malformed **previous** close makes the
move UNVERIFIABLE, so a bad earlier bar cannot discard an otherwise-valid latest quote.
Tests: `test_zero_latest_close_is_blocked`, `test_negative_latest_close_is_blocked`,
`test_nan_latest_close_is_blocked`, `test_infinite_latest_close_is_blocked`,
`test_non_numeric_latest_close_is_blocked`, `test_nan_previous_close_is_unverifiable`,
`test_infinite_previous_close_is_unverifiable`, `test_non_numeric_previous_close_is_unverifiable`,
`test_negative_previous_close_is_unverifiable`, `test_invalid_latest_price_never_reaches_valuation`.

**Finding 3: a malformed known-actions file was silently treated as "no actions".**
`load_known_actions` returned `[]` on any parse error. Fixed: it returns `(actions, problems)` and
distinguishes absent (no actions) from malformed/unreadable/not-a-list (a config problem). A config
problem is passed to `build_report`, which fails closed with a `config:` blocked reason shown in
sections 4, 5 and the summary.
Tests: `test_load_known_actions_absent_is_empty`, `test_load_known_actions_valid`,
`test_load_known_actions_malformed_reports_problem`,
`test_load_known_actions_not_a_list_reports_problem`, `test_cli_malformed_known_actions_blocks`,
`test_config_problem_blocks_the_report`.

**Finding 4: unverified/unchecked tickers must be visible on the console.**
The console already printed the report body (which carries the markers) but did not call out
UNVERIFIABLE tickers. Fixed: `main()` prints a "valuation NOT fully verified" note for unchecked
tickers and a WARNING per config problem.
Tests: `test_cli_unchecked_ticker_visible_and_warns`, `test_cli_suspect_ticker_visible`.

## 11. Sixth review — ledger input validation + two cleanups

**Finding: `Transaction` accepted non-finite values.**
`__post_init__` only rejected zero/negative values, so `float("nan")` (and `+/-inf`) passed and
could contaminate cash, cost basis and equity. Fixed: a `_finite(x)` helper (real finite number,
never a bool) now validates `shares`, `price` and `fee` before the range checks; `Ledger` also
validates `starting_cash` (finite and >= 0). Zero price remains permitted (worthless shares).
Tests: `test_nan_shares_rejected`, `test_nan_price_rejected`, `test_nan_fee_rejected`,
`test_infinite_values_rejected`, `test_boolean_values_rejected`, `test_non_numeric_values_rejected`,
`test_zero_price_is_permitted`, `test_nan_starting_cash_rejected`,
`test_infinite_starting_cash_rejected`, `test_negative_starting_cash_rejected`,
`test_boolean_starting_cash_rejected`, `test_non_finite_inputs_cannot_contaminate_equity`.

**Cleanup 1: unreachable BLOCKED branch removed from `sanity.check_quote`.**
The `if pc <= 0: ... "BLOCKED"` branch was unreachable once `_valid_price(previous.close)` runs
first (it returns UNVERIFIABLE). Removed; the module docstring's outcome list was updated to match.

**Cleanup 2: unused `monitor._closes` helper removed.**
The single-price-basis rule is documented in the module docstrings; the helper was never called.

Verified after the change: `pytest tests/` 160 passed; `ruff check .` All checks passed;
replay acceptance PASS; risk demo PASS.
