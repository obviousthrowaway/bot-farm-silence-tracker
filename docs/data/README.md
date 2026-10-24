# Scored accounts

This folder holds the account-level results of the Reddit scans of the default outage event (`2026-10-takedown`): runs 1 to 4 and two later 3-day scans, called scan a and scan b here. The data is published so that other researchers can check and reproduce the counts reported in the main README and in [data-analysis.md](../../data-analysis.md).

These are provisional results. They are not a verdict about any account or about the outage. Only part of run 4's draw was scored, and scan b is incomplete, as described below.

The lists contain public Reddit usernames only. They hold no post text, no subreddit names and no activity counts. If you would like your account removed from these lists, please open an issue in this repository and it will be taken out of every file.

## The files

| File | Run | Horizon (data end) | Accounts drawn | Rows (scored accounts) | Eligible | Silent | Silent share of eligible |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `run-1-scored-accounts.csv` | Run 1 | 2026-10-14T07:10:26Z, 3.28 days after the cutoff | 10,000 | 9,999 | 6,398 | 287 | 4.49% |
| `run-2-scored-accounts.csv` | Run 2 | 2026-10-15T00:30:00Z, 4.0 days (earlier for some accounts, see below) | 10,000 | 10,000 | 6,324 | 213 | 3.37% |
| `run-3-scored-accounts.csv` | Run 3 | 2026-10-15T00:30:00Z, 4.0 days | 20,000 | 19,998 | 12,733 | 421 | 3.31% |
| `run-4-scored-accounts.csv` | Run 4 (22,083 of its draw scored) | 2026-10-18T21:10:09Z, 7.86 days | 30,000 | 22,083 | 13,838 | 191 | 1.38% |
| `scan-3d-a-scored-accounts.csv` | Scan a | 2026-10-14T02:27:03Z, 3.08 days | 3,000 | 3,000 | 1,925 | 80 | 4.16% |
| `scan-3d-b-scored-accounts.csv` | Scan b, incomplete | 2026-10-14T02:27:03Z, 3.08 days | 3,000 | 2,338 | 1,510 | 62 | 4.11% |

The row, eligible and silent counts were recomputed from the CSV files themselves. For runs 1 to 4 they equal the figures published in the main README and in [data-analysis.md](../../data-analysis.md), and for scans a and b they equal the figures in data-analysis.md and on the README's chart.

Each file is a UTF-8 CSV file with one row per scored account and a header row, sorted by `user_id`. Only scored accounts are listed: an account whose fetch from the archive failed, or that was never fetched, is not in the file.

The columns are the same in every file:

- `platform`: the data source. It is `reddit-archive` on every row, which means public Reddit activity read through the Arctic Shift archive.
- `user_id`: the account's identifier as the tool stores it. For this source it is the Reddit username.
- `username`: the public Reddit username, exactly as stored.
- `last_activity_utc`: the time of the account's latest post or comment that the archive returned, at or before the account's data end (defined below), written in ISO 8601 with a +00:00 offset (for example `2026-10-14T05:54:13+00:00`). Only activity from the start of the fetch window, 2026-09-07T18:56:00Z, is counted. The field is empty when no activity was found in that range; this is the case for 3 accounts in run 1, 1 in run 2, 3 in run 3 and none in the other files.
- `eligible`: 1 when the account passed the tool's steady-poster test, otherwise 0.
- `silent`: 1 when an eligible account made no post or comment between the cutoff and its data end, otherwise 0. An account that is not eligible always has 0.

Some rows are Reddit moderation-team system accounts, whose names end in `-ModTeam` (for example `CombatFootage-ModTeam` and `europeanunion-ModTeam`). These accounts post notices on behalf of a subreddit's moderators rather than for a person. There are 11 in run 1 (10 eligible), 11 in run 2 (10 eligible), 9 in run 3 (8 eligible), 13 in run 4 (11 eligible), 5 in scan a (5 eligible) and 3 in scan b (3 eligible). None of them is silent in any file.

## How the accounts were scored

Every row was scored with the tool's own scoring function (`score_account` in `src/bot_farm_silence_tracker/scoring.py`), using the scoring parameters stored with each run. All runs and scans use the same rules and dates, except for the horizon:

- The cutoff is 2026-10-11T00:30:00Z, the reported time of the collapse.
- The event has two markers, earlier moments inside the outage as reported: a strike at Sasovo at 2026-10-08T06:56:00Z and a strike in the Kaluga region at about 2026-10-09T00:00:00Z (approximate). With a margin of 12 hours before the first marker, the baseline ends and the silence window starts at 2026-10-07T18:56:00Z.
- The baseline is the 30 days before that, from 2026-09-07T18:56:00Z to 2026-10-07T18:56:00Z.
- The horizon is the run's stored data horizon, given in the table above. Each account is judged only on activity up to the horizon, or up to the account's own stored window end when that is earlier. That time is called the account's data end. Activity after the data end is ignored, even when it was fetched later, so that every account in a file is judged on the same horizon. Every file uses this strict horizon rule: only activity up to an account's data end can end its silence.

An account is eligible (a steady poster) when all of the following hold:

- it has at least 20 posts and comments in the baseline;
- it was active on at least 12 different UTC days of the baseline;
- it made at least one post or comment in the last 7 days of the baseline;
- the coefficient of variation of its weekly counts over the baseline (population standard deviation divided by the mean) is at most 1.0;
- the tool's status for the account is active, not deleted, suspended, private or otherwise unavailable.

An eligible account is silent when it made no post or comment from the cutoff to its data end, and its last activity before the cutoff fell inside the silence window, from 2026-10-07T18:56:00Z to the cutoff.

The files of runs 2 to 4 and scans a and b were exported by the same method as run 1's file. Applied to run 1's stored data, that method reproduces `run-1-scored-accounts.csv` byte for byte.

## Notes on each run

### Run 1

The first 10,000-account draw (discovery seed 1638893776). One account could not be fetched from the archive, so it was not scored and is not in the file.

An earlier provisional local list of run 1's silent accounts also had 287 entries, but it is not the same set. It differs by one account in each direction. That list was built before the archive backfilled some late activity, and it counted activity after the horizon as evidence that an account was not silent. The file here follows the strict-horizon rule described above.

The snapshot of 2026-10-17 in [data-analysis.md](../../data-analysis.md#runs-1-to-3-provisional) reports run 1 as 288 silent accounts out of 6,399 eligible accounts (4.50%), while this file has 287 out of 6,398 (4.49%). The difference comes from two accounts that were also drawn by a later run, which fetched them from the archive again on 2026-10-18 and replaced their cached histories. For one of them, the archive returned a comment made shortly before run 1's horizon that it had not returned at the first fetch, so that account is no longer silent. For the other, the archive returned no posts or comments at all at the second fetch, although it had returned a steady history at the first, so that account is no longer eligible. The snapshot's figures were computed from the cache before these re-fetches. They will be refreshed together with the 7-day rescore.

### Run 2

A complete 10,000-account draw (discovery seed 738594449); every account was fetched and scored. Run 2 mixes windows: for 2,232 of its accounts (1,550 of its eligible ones) the archive data ends earlier, at 2026-10-14T07:10:26Z, so they are judged over at most 3.28 days, and the rest over 4.0 days. The file does not say which window an account was judged on.

### Run 3

A complete 20,000-account draw (discovery seed 1868357523). Two accounts could not be fetched from the archive, so 19,998 accounts were scored.

### Run 4

A 30,000-account draw (discovery seed 2021540802) judged over 7.86 days. Run 4 ran until there was enough data, and the rest of the draw was not scored (see [data-analysis.md](../../data-analysis.md)). The file holds the scored part, as published in the main README and in data-analysis.md: the first 22,084 positions of the draw, counting from 0, in the order they were fetched, which is close to discovery order. Of these, 22,083 were fetched and scored and 1 (at position 21,203) had a fetch error and is not in the file. The scored part is a prefix of the draw, not a random subsample of it, and its figures describe the scored accounts only.

After the scored copy was taken, the scan fetched 64 more accounts (at positions 22,084 to 22,158, where 11 more fetches failed) before it was interrupted at 22,147 of 30,000 accounts fetched; the other 7,841 positions were never fetched. Those 64 accounts were not scored for the published figures and are not in the file, so that the file covers the same accounts as those figures. None of them would be silent; with them, the run would have 13,881 eligible accounts and the same silent count.

### Scans a and b

Two further 3,000-account draws (discovery seeds 1800391620 for scan a and 969146094 for scan b), each scanned with the reference time pinned to 2026-10-14T03:00:00Z, so that the archive's data horizon lands about 3 days after the cutoff (2026-10-14T02:27:03Z, 3.08 days). They were made with tool version 0.2.1; runs 1 to 4 were made with 0.2.0. The scoring rules are the same.

Scan a is complete: all 3,000 accounts were fetched and scored. Its counts follow the strict horizon rule at its stored horizon, the same rule as every other file.

Scan b is incomplete. It scored 2,338 of its 3,000 accounts in fetch order, and the rest were not scored because of an archive outage: 5 fetches failed and 657 accounts were never fetched. The file holds only the 2,338 scored accounts. They are the first 2,343 positions of the draw in fetch order, less the 5 failed fetches, so they are a prefix of the draw and not a random subsample of it.

One account in scan a and two in scan b made no post or comment between the cutoff and the horizon but did post or comment between the horizon and the pinned reference time. They are silent in these files, by the strict horizon rule; counting that later activity would give 79 of 1,925 for scan a and 60 of 1,510 for scan b.

## Overlap between the files

The runs and scans draw from the same pool of accounts, so the same account can appear in several files, scored on each run's own horizon and data. The six files hold 45,333 distinct accounts. For example, 3,944 accounts are in both run 1 and run 3, and 6,577 are in both run 3 and run 4. An account can be silent in one file and not in another, because the horizons differ and because the archive may have returned different data at different fetches. Never add the counts of different files together; the main README and [data-analysis.md](../../data-analysis.md) count each distinct account once where they pool runs.

## Watchlist of silent accounts

`silent-accounts-watchlist.csv` lists every account that is flagged silent (`silent` is 1) in at least one of the published per-account files (runs 1 to 4 and scans 3d-a and 3d-b), once per `platform` and `user_id`, so that these accounts can be checked again later. Its columns are `platform`, `user_id`, `username`, `last_activity_utc` and `sources`, where `sources` is a semicolon-separated list of the files' run or scan names in which the account was silent, and the rows are sorted by `platform` and then `user_id`. For an account that is silent in more than one file, `last_activity_utc` is the latest value seen across those files.

The value of `last_activity_utc` is the latest activity seen for the account up to each scan's data end, so a later re-check should look for activity after that time. The list mixes scans with different windows, from 3 to 8 days, and the 3-day scans and the runs are different populations of accounts, so the rows are not comparable with each other as if they came from one scan. An account that is silent in one scan is not an established bot account, and an account that is silent in several files has been flagged more than once, which is still not proof of anything about it. A flag means only that the archive holds no activity for the account in that scan's window, and nothing more.

## What these results do and do not mean

A value of 1 in `silent` means only that an eligible account had no activity seen up to its data end: it made no post or comment that the archive holds between the cutoff and its data end. It is not evidence that the account is automated or inauthentic, and silence alone is not proof of anything about an account. Ordinary users stop posting for many reasons, and in an ordinary week several percent of steady posters go quiet for three days. The outage itself also cut off ordinary users of the affected services. In a local spot check, 300 accounts were drawn at random from the silent accounts of runs 1 to 3 and fetched again on 2026-10-18, and 182 of them (60.7%, 95% Wilson interval 55.0% to 66.0%) had posted or commented after their run's horizon. The spot-check files are not published.

The archive can lag behind Reddit by hours or days, and it can have ingestion gaps. It can also miss content that was deleted or removed, and it cannot tell a suspended account from a silent one. An account can therefore appear silent here when it was not.

The sample is not all Reddit users. Each file is a sample of accounts with archived activity in the 30 days before the cutoff in the seed communities listed in `config/seeds.yaml`: the 23 subreddits recorded with every run (seed list digest `306d69fa42e4b430e9b6821eca8e741c6a48e051fe252fd8abe6ad8fd129812f`, computed by the tool's `seeds_digest`), of which 21 contributed accounts in runs 1 to 4 (r/russia and r/estonia contributed none). Discovery is activity-weighted, with an equal quota per subreddit, so an account's chance of discovery is roughly proportional to its activity there, and the draw is uniform only within the discovered pool. The results describe that sample only.

## How to reproduce or verify

Every row can be checked against the public Arctic Shift archive (https://github.com/ArthurHeitmann/arctic_shift), which needs no credentials.

1. For a username, request its posts and comments from `https://arctic-shift.photon-reddit.com/api/posts/search` and `https://arctic-shift.photon-reddit.com/api/comments/search` with the parameters `author=<username>`, `after` and `before` (both exclusive, epoch seconds accepted), `sort=asc` and `limit=100`, paging until the range is covered. Cover the range from 2026-09-07T18:56:00Z to the horizon of the file's run.
2. Apply the eligibility test to the activity in the baseline, and the silence test to the activity from the cutoff to the horizon, with the rules above.
3. Compare the result with the row. Expect some differences: the archive may have backfilled activity since the run was fetched, and it may have dropped content that was deleted since then.

To repeat a whole scan with the tool, run `bot-farm-silence-tracker scan --platform reddit-archive --n <accounts drawn> --seed <discovery seed>` under the default event, with the numbers above; for scans a and b, add `--now 2026-10-14T03:00Z` (`--now` is a hidden option, intended for testing and reproduction). A new scan discovers accounts from the archive as it is at that time, so its sample may not match the published one exactly.

Please be kind to the archive: it is a free community service, so keep the request rate low (the tool uses 0.5 requests per second by default) and respect its rate-limit headers.
