# Bot Farm Silence Tracker

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/charts/runs-provisional-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/charts/runs-provisional-light.svg">
  <img alt="Chart: the share of eligible accounts that went silent in the 2026-10-takedown Reddit scan, with a 95% Wilson interval for each row, every row scored as of its run's stored data horizon, as of 2026-10-22. Run 1, 3.3 days after the cutoff: 287 of 6,398, 4.49% (4.01% to 5.02%). Run 2, 3.3 to 4.0 days: 213 of 6,324, 3.37% (2.95% to 3.84%). Run 3, 4.0 days: 421 of 12,733, 3.31% (3.01% to 3.63%). Runs 1 to 3, each distinct account counted once: 698 of 19,868, 3.51% (3.27% to 3.78%). Run 4, which ran until there was enough data (22,083 of a 30,000-account draw were scored, in discovery order, so they are not a random subsample, and the rest of the draw was not scored; see data-analysis.md), 7.86 days: 189 of 13,838, 1.37% (1.19% to 1.57%). All runs pooled, each distinct account counted once on its longest window, mixed windows: 667 of 27,674, 2.41% (2.24% to 2.60%). Hollow markers show a matched base rate under the runs' own definition, from the tool's own placebo draws in three ordinary weeks, adjusted to each run's window: run 1 4.41%, run 2 3.52%, run 3 3.24%, and runs 1 to 3 together 3.47% (short bar 3.41% to 3.63%). The ordinary-week rows, 4.20% at 3 days of silence and 0.86% at 7 days, were counted with another definition (a 48-hour window and no event markers), so they are a reference only and not a matched comparison. Run 4 has no matched base under the runs' definition, and the pooled row mixes windows and compares with neither. The runs overlap, so rows are never summed. All figures are provisional and there is no verdict; silence alone is not proof that an account is a bot." src="docs/charts/runs-provisional-light.svg">
</picture>


Provisional share of eligible Reddit accounts that went silent after the cutoff of the
2026-10-takedown event (2026-10-11 00:30 UTC), per run and pooled once per distinct
account, as of 2026-10-22. Run 4 ran until there was enough data, and the rest of the draw was
not scored (see [data-analysis.md](data-analysis.md#run-4-and-all-runs)): 22,083 of a
30,000-account draw were scored, in discovery order, so they are not a random subsample.
Results from every platform (Reddit, Bluesky and X) go on this one chart, each platform in
its own group and never pooled. It currently shows the Reddit archive, the only platform
scanned so far.
Silence alone is not proof that an account is a bot. The tables, the method and the
caveats are in [data-analysis.md](data-analysis.md).

## TL;DR

> - **3 days of silence:** 698 of 19,868 eligible accounts went silent, **3.51%**, or about **1 in 28** (95% Wilson interval 3.27% to 3.78%). This counts runs 1 to 3 together, each account once, and "silent" means no activity seen up to their last scan, about 3.3 to 4.0 days after the cutoff (2026-10-11 00:30 UTC). This is consistent with an ordinary week: the tool's own placebo draws in three ordinary weeks, with the same rules and adjusted to each run's window, give an expected 3.47%, a difference of +0.05 percentage points (from unrounded values), so there is no excess and no clear deficit. The ordinary-week rate of 4.20% that this README quoted before is not comparable, because it was counted over exactly 3 days, with a 48-hour window and without the event markers, in a different mix of communities.
> - **7 days of silence:** 189 of 13,838 eligible accounts went silent, **1.37%**, or about **1 in 73** (95% Wilson interval 1.19% to 1.57%). This is run 4, scored over 7.86 days. Run 4 ran until there was enough data, and the rest of the draw was not scored (see [data-analysis.md](data-analysis.md#run-4-and-all-runs)): 22,083 of a 30,000-account draw were scored, in discovery order, so they are not a random subsample of the draw, and this figure describes the scored accounts only.
> - **7 days compared with an ordinary week:** under one definition for both (the archive dump's: a 48-hour window and no event markers), run 4's accounts are silent at 1.13% at 7.00 days (154 of 13,594), against 0.86% in ordinary weeks. That is a possible small excess of about 0.27 percentage points, roughly 1 in 370, and it is not established beyond doubt. At 3 days the same accounts were below the ordinary-week rate (3.65% against 4.20%), so part of the gap may come from how slowly silences end, and the comparison assumes that silences in this sample end at the same pace as in ordinary weeks. Both the 3-day and the 7-day figures here are for run 4's 22,083 scored accounts, which are not a random subsample of its draw. The 1.37% above uses the tool's definition, which has no matched ordinary-week figure at 7 days yet.
> - One possible explanation that we have not tested: the targeted data centers may have had backups or regional redundancy, so some accounts that looked silent may have come back online. Of the 120 accounts that were silent in the earlier runs and were among the first 11,201 accounts fetched again in run 4 (an earlier snapshot of run 4), 68 posted again by the 7-day scan.
> - These are not counts of bots, and there is no verdict. Silence alone is not proof that an account is a bot, and the outage also cut off ordinary users.
> - **You can help:** anyone can add data to these figures by installing the tool and running more scans of this event on their own computer. The default source, the Reddit archive, needs no account or API key, a scan that stops can be resumed, and the results are shared as registry records in a pull request. See [Run it yourself](#run-it-yourself) and [Open a PR](#open-a-pr).
> - **Bluesky and X (Twitter) too:** the tool can also be run for Bluesky, which needs no account in its default mode, and for X, which needs your own paid X API access (a bearer token, full-archive search for this event, and a spending cap). Their records are kept as separate platforms in the registry, and the figures above so far are from Reddit only. See [Other platforms](CONTRIBUTING.md#other-platforms-bluesky-and-x).
>
> As of 2026-10-22.

## Why more scans make the data more valid

- Each run is scored only up to the archive's data horizon stored with it. Runs 1 to 3
  stop at 3.3 to 4.0 days of silence, and only run 4 reaches 7 days, so new scans are
  needed to test whether the 7-day figure holds.
- Run 4 scored 22,083 of its 30,000-account draw, in discovery order, so the scored part
  is not a random subsample of the draw. New samples give 7-day figures that do not rest
  on that one run.
- Runs 1 to 3 share many accounts, so they are not independent checks. Every draw comes
  from the same pool, recently active accounts in the seed communities, weighted by
  activity with an equal quota per subreddit. A new 3,000-account draw overlaps the
  existing accounts by roughly 70% (about 2,100) and adds roughly 900 new ones, and
  subsets of new accounts only are reported separately.
- The archive paces and throttles requests, so one computer needs hours to days for a
  large scan. People scanning in parallel finish more complete samples.
- More eligible accounts narrow the 95% intervals. More scans do not make silence proof
  that an account is a bot.

## Run it yourself

The install step differs between [Windows](CONTRIBUTING.md#windows),
[macOS](CONTRIBUTING.md#macos) and [Linux](CONTRIBUTING.md#linux) (full steps in
CONTRIBUTING.md). On Windows, paste this into PowerShell:

```powershell
irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
```

On macOS or Linux, paste this into a terminal:

```bash
curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
```

Press Enter to keep the default platform and open a new terminal. Then, on every system:

```bash
bot-farm-silence-tracker --version
bot-farm-silence-tracker scan --platform reddit-archive --n 30
bot-farm-silence-tracker scan --platform reddit-archive --n 1000 --concurrency 2 --source-option max_rps=1.0
```

The Reddit archive needs no account or API key, and scans use the default event,
`2026-10-takedown`. A scan that stops prints the command that resumes it. See
[CONTRIBUTING.md](CONTRIBUTING.md#3-expect-a-long-scan) for how long a scan takes.

## Open a PR

Fork the repository, clone your fork, create a branch, bring your fork's `main` up to
date with this repository, and run from the clone's folder, with your scan's run number
in place of `RUN_ID`:

```bash
bot-farm-silence-tracker registry add --run-id RUN_ID --event 2026-10-takedown --registry registry
bot-farm-silence-tracker registry merge --registry registry
bot-farm-silence-tracker registry validate --registry registry
git add registry
git commit -m "Add scan results for 2026-10-takedown"
bot-farm-silence-tracker registry verify --base origin/main --head HEAD
```

Push and open a pull request into `main` that changes only files under `registry/`. The
`Registry gate` check validates the records and re-fetches a sample of them. Up to 15
`scanned` or `candidate` records on `reddit-archive` or `bluesky` need no review; a larger
change, or any record on `x` or `reddit`, needs the owner's approval of its head commit and the
`registry-override` label, which is removed after every new push
([all rules](CONTRIBUTING.md#4-share-the-results-in-a-pull-request)). Registry records
hold real account names (base64-encoded, so anyone can check them). Never commit your
local CSV, JSON or database files, and never describe a silent account as a bot.

More: [data-analysis.md](data-analysis.md), [CONTRIBUTING.md](CONTRIBUTING.md),
[docs/USAGE.md](docs/USAGE.md), [INSTALL.md](INSTALL.md), [SECURITY.md](SECURITY.md).
