# Changelog

Each release is published with the notes from its section below. The release workflow
fails when the section for the tag's version is missing.

## [Unreleased]

- The README opens with the provisional runs chart again, directly under the title and above the TL;DR, which had pushed it down. The chart is redrawn with the figures re-scored on 2026-10-20 instead of the snapshot of 2026-10-17: runs 1 to 3 with run 3 complete (run 1: 287 of 6,398; run 2: 213 of 6,324; run 3: 421 of 12,733), the runs 1 to 3 union (698 of 19,868), run 4, which is partial and still being scanned (103 of 7,179 over 7.86 days), and all runs pooled once per distinct account (681 of 23,745), each with a 95% Wilson interval and beside the 3-day and 7-day ordinary-week rates. The row for the run 3 accounts not in runs 1 or 2 is dropped because it has no current figure.
- `scripts/render_run_chart.py` now reads a list of rows from `docs/charts/runs-provisional.json`. A rough matched base rate is drawn only for windows between 3 and 7 days, so run 4 and the pooled row, which mixes windows, have none; a row that gives only the range of its windows, not how many accounts each window holds, gets the range without a weighted marker. The chart's height follows the number of rows.
- The README's TL;DR and a new section add run 4 of the Reddit scan, which is scored over 7.86 days. The operator stopped the run at 11,201 of 30,000 accounts fetched because the archive had slowed down, and then resumed it with the same data horizon. The slowdown came from throttling on the archive's side, made worse by the scan's own retry waits. The README's run 4 figures are for the stopped part and will change when the run completes. In that part, 103 of 7,179 eligible accounts went silent, 1.43% (95% Wilson interval 1.18% to 1.74%). Reweighted to the full draw's community mix, the rate is 1.44%, because the fetched part is not a random subsample.
- The README also adds the figure pooled over all runs, counted once per distinct account (681 of 23,745, 2.87%), with the per-window figures beside it and the reason they must not be compared directly. It lists the work that is still pending, and it gives two rough 7-day estimates for runs 1 to 3 (about 1.8% from a 50-account spot check and about 1.5% from the accounts shared with run 4), both marked as hints only.
- The runs 1 to 3 figures under the chart are re-scored at each run's stored horizon with run 3 complete (run 1: 287 of 6,398; run 2: 213 of 6,324; run 3: 421 of 12,733). The chart and the tables of the runs 1 to 3 section are marked as the earlier snapshot of 2026-10-17.
- The registry chart no longer counts `scanned` records with `cliff` set as potential bot accounts. Its "silent since takedown (automatic)" group is removed, so only `verified_inauthentic` and `candidate` records are charted; `scanned` records, with or without a cliff, were only scanned and not marked for review. The chart's subtitle and the README now say that a charted account had no activity seen up to its last scan, instead of saying that it has had no activity since it stopped posting. The registry file format and the gate's checks are unchanged.
- Added the optional `report --last-seen` for runs on the Reddit archive. For each silent account (`--last-seen-scope all`: every observed account) it asks the archive for the newest post and the newest comment after the end of the scored window, up to the time of the check, with the same one-row existence query, rate limit, back-off and User-Agent as the two-phase pre-check (two requests per account), and adds the columns `newest_activity_utc`, `newest_activity_kind`, `last_seen_status` (`active_after_window`, `no_activity_found` or `inconclusive`) and `scored_silent` to the CSV and JSON report, with a notice and a `last_seen` summary block in the JSON. It is not a closure verdict, because the archive keeps no account state: activity after the window means the account was not closed then, and no activity means it may be closed, suspended, deleted or abandoned. A query that times out or fails, a returned row that cannot be read, or a check that stops early is `inconclusive` and never reads as silence, but activity that was found before a stop is always kept. The check stops when a query fails and no activity is found for three accounts in a row, even when only one kind of query fails, and its requests share one pooled budget (six per account that needs asking, plus ten). The run's database is opened read-only and never written; answers are kept in a separate file (`--last-seen-cache`, tested for writing before any request, bound to the database and the run) so an interrupted check continues, and a rerun within 24 hours asks nothing again. The check always uses the archive's default pace (a stored `max_rps` is ignored) and honours `--trust-env` like a scan. With `--last-seen-scope all`, the accounts that are not silent are listed under `last_seen_other_accounts` in the JSON report, not under `candidates`, and the CSV report says that their rows are not candidates. Without the flag, `report` sends no request and its output is unchanged.
- `registry add` and every other registry check now accept the names of subreddits' system accounts, `<subreddit>-ModTeam` (for example `europeanunion-ModTeam`, 21 characters), which the 3 to 20 character username rule rejected, so one such account aborted the whole `registry add`. The base is a subreddit name (3 to 21 letters, digits and underscores) and the suffix `-ModTeam` is case-sensitive; other names longer than 20 characters are still refused. The ID of a `reddit` record, which is the lower-cased name, ends in `-modteam`.
- `registry add --flagged-only` skips the fully scanned accounts whose new record is unflagged (`cliff` false and not `candidate`), unless the registry already holds a flagged record of the account: that record is merged as usual, so a newer scan can still demote it and no stale accusation stays. The summary reports how many accounts were skipped and how many of those already have an unflagged record, which is left unchanged. Every built record is validated before the skip, so one invalid unflagged record still aborts the run. Without the option nothing changes, and the registry file format and the gate's checks are the same.
- The README now opens with a TL;DR: the pooled share of eligible Reddit accounts that went silent after the outage, counted once per distinct account across runs 1 to 3 as of each run's stored horizon (698 of 19,868, 3.51%, 95% Wilson interval 3.27% to 3.78%), with the ordinary-week comparison and the caveat that it is not a count of bots.
- Run 1's scored accounts are published in `docs/data/run-1-scored-accounts.csv` (9,999 public Reddit usernames with their last activity time and eligible and silent flags, scored at the run's stored horizon), with a description of the columns, the scoring rules and the caveats in `docs/data/README.md`.
- Run chart follow-up: the new-only row label now states its silence window like the other rows, and the README notes that the shared-account window effect (0.96 points) is larger than the matched-base adjustment (0.6 points) and that run 2's 214 is scored to its stored horizon (206 when re-scored to the cutoff window).
- The README now includes a provisional chart of runs 1 to 3 (run 3 partial), with the ordinary-week base rates, a rough base rate matched to each run's silence window, and 95% intervals, drawn by `scripts/render_run_chart.py` from a file of aggregate counts (`docs/charts/runs-provisional.json`).
- The chart and the README also show a row over all distinct accounts across the runs (each account counted once, using its record with the longest scored window) and a row for the accounts of run 3 that are not in runs 1 or 2, because the runs share accounts and must never be added together.
- Known issue, not fixed here: `report` scores a completed run at its stored window end but a running run up to its later reference time, and the scan's console count can include posts after the horizon, so the three counts for the same run are not comparable (run 2: 205 in the scan, 214 in `report`). A follow-up in the tool is needed. The README scores every run as of its stored horizon.
- The README now opens with the provisional runs chart and a short summary; the empty shared-registry chart moved below it.

## [0.2.1]

- The default outage event now starts at the reported collapse, 2026-10-11 00:30 UTC, with markers for the earlier strikes on 8 and 9 October.
- Strike markers now carry hints in the viewer's local time.
- The desktop app keeps the computer awake while a scan is running.
- The README now includes a chart.
- A run that is still being scanned can no longer be continued a second time at once:
  each scan holds a lock on its run and records a heartbeat, and the app shows such a
  run as still running in another window.
- The X daily spend cap holds across outage events and custom cutoffs again: the
  databases in the cache folder share one spend ledger, `spend.db`.
- The move of the old cache database waits for, or skips, open databases and can no
  longer lose spend records when it is interrupted.
- A cancelled scan is saved as `cancelled` instead of `failed`; both can be continued.
- X timelines stop paging at a page cap or when a page token repeats.
- Every run is compared with placebo cutoffs in earlier weeks, each scored on a sample
  of its own that is discovered and fetched in windows moved back by the same weeks,
  and says when its cliff rate is not clearly above them. Platforms that cannot
  discover in earlier weeks (Reddit's own API, X recent search, Bluesky feed seeds)
  have no placebo comparison and say why; on X with full-archive search it is opt-in.
- A synchronized stop is tested against the accounts' own posting hours with 9999
  permutations, measured against the run's largest cluster (which adjusts for many
  clusters), and no longer makes a candidate on its own.
- Candidates need a live profile check, verified records need 21 days of silence, and
  report drafts are neutral and refuse accounts that posted again.

## [0.2.0]

- Added the desktop app.
- Added a macOS .pkg installer and a Windows Setup.exe installer.
- Added named outage events.
- Fixed the output of install.sh.

## [0.1.0]

- First release: a command-line scanner for Reddit (through the Arctic Shift archive), Bluesky and X.
- Added a shared registry and a review gate.
- Added installers.
