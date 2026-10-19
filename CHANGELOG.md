# Changelog

Each release is published with the notes from its section below. The release workflow
fails when the section for the tag's version is missing.

## [Unreleased]

- `registry add` and every other registry check now accept the names of subreddits' system accounts, `<subreddit>-ModTeam` (for example `europeanunion-ModTeam`, 21 characters), which the 3 to 20 character username rule rejected, so one such account aborted the whole `registry add`. The base is a subreddit name (3 to 21 letters, digits and underscores) and the suffix `-ModTeam` is case-sensitive; other names longer than 20 characters are still refused. The ID of a `reddit` record, which is the lower-cased name, ends in `-modteam`.
- `registry add --flagged-only` writes only the flagged accounts (`cliff` true, or `candidate`) and skips the other fully scanned accounts. The summary reports how many were skipped as unflagged and how many of those already have a record, which is left unchanged. Without the option nothing changes, and the registry file format and the gate's checks are the same.
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
