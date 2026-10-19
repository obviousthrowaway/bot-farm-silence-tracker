# Run 1 scored accounts

This folder holds the account-level results of run 1, the first 10,000-account Reddit scan of the default outage event (`2026-10-takedown`). The data is published so that other researchers can check and reproduce the counts reported in the main README.

The list contains public Reddit usernames only. It holds no post text, no subreddit names and no activity counts. If you would like your account removed from this list, please open an issue in this repository and it will be taken out.

## The file

`run-1-scored-accounts.csv` is a UTF-8 CSV file with one row per scored account and a header row. It has 9,999 rows, sorted by `user_id`. The run drew 10,000 accounts; one account could not be fetched from the archive, so it was not scored and is not in the file.

The columns are:

- `platform`: the data source. It is `reddit-archive` on every row, which means public Reddit activity read through the Arctic Shift archive.
- `user_id`: the account's identifier as the tool stores it. For this source it is the Reddit username.
- `username`: the public Reddit username, exactly as stored.
- `last_activity_utc`: the time of the account's latest post or comment that the archive returned, at or before the account's data end (defined below), in ISO 8601 UTC. Only activity from the start of the fetch window, 2026-09-07T18:56:00Z, is counted. The field is empty when no activity was found in that range; this is the case for 3 accounts.
- `eligible`: 1 when the account passed the tool's steady-poster test, otherwise 0.
- `silent`: 1 when an eligible account made no post or comment between the cutoff and its data end, otherwise 0. An account that is not eligible always has 0.

## How the accounts were scored

Every row was scored with the tool's own scoring function (`score_account` in `src/bot_farm_silence_tracker/scoring.py`), using the scoring parameters stored with run 1. The rules and dates are as follows.

- The cutoff is 2026-10-11T00:30:00Z, the reported time of the collapse.
- The event has two markers, earlier moments inside the outage as reported: a strike at Sasovo at 2026-10-08T06:56:00Z and a strike in the Kaluga region at about 2026-10-09T00:00:00Z (approximate). With a margin of 12 hours before the first marker, the baseline ends and the silence window starts at 2026-10-07T18:56:00Z.
- The baseline is the 30 days before that, from 2026-09-07T18:56:00Z to 2026-10-07T18:56:00Z.
- The horizon of run 1 is 2026-10-14T07:10:26Z, about 3.3 days after the cutoff. This is the run's stored data horizon. Each account is judged only on activity up to the horizon, or up to the account's own stored window end when that is earlier. That time is called the account's data end. Activity after the data end is ignored, even when it was fetched later, so that every account is judged on the same horizon.

An account is eligible (a steady poster) when all of the following hold:

- it has at least 20 posts and comments in the baseline;
- it was active on at least 12 different UTC days of the baseline;
- it made at least one post or comment in the last 7 days of the baseline;
- the coefficient of variation of its weekly counts over the baseline (population standard deviation divided by the mean) is at most 1.0;
- the tool's status for the account is active, not deleted, suspended, private or otherwise unavailable.

An eligible account is silent when it made no post or comment from the cutoff to its data end, and its last activity before the cutoff fell inside the silence window, from 2026-10-07T18:56:00Z to the cutoff.

## Verified totals

The following totals were recomputed from the CSV file itself:

- 9,999 accounts were scored.
- 6,398 accounts are eligible.
- 287 accounts are silent, which is 4.49% of the eligible accounts.

An earlier provisional local list of run 1's silent accounts also had 287 entries, but it is not the same set. It differs by one account in each direction. That list was built before the archive backfilled some late activity, and it counted activity after the horizon as evidence that an account was not silent. The file here follows the strict-horizon rule described above.

## What these results do and do not mean

A value of 1 in `silent` means only that an eligible account made no post or comment that the archive holds between the cutoff and its data end. It is not evidence that the account is automated or inauthentic. Ordinary users stop posting for many reasons, and in an ordinary week several percent of steady posters go quiet for three days. The outage itself also cut off ordinary users of the affected services. Many of the accounts marked silent here posted again after the horizon.

The archive can lag behind Reddit by hours or days, and it can have ingestion gaps. It can also miss content that was deleted or removed, and it cannot tell a suspended account from a silent one. An account can therefore appear silent here when it was not.

The sample is not all Reddit users. It is a random sample (discovery seed 1638893776) of accounts that were active in the seed communities listed in `config/seeds.yaml`: the 23 subreddits recorded with the run (seed list digest `306d69fa42e4b430e9b6821eca8e741c6a48e051fe252fd8abe6ad8fd129812f`, computed by the tool's `seeds_digest`). The results describe that sample only.

## How to reproduce or verify

Every row can be checked against the public Arctic Shift archive (https://github.com/ArthurHeitmann/arctic_shift), which needs no credentials.

1. For a username, request its posts and comments from `https://arctic-shift.photon-reddit.com/api/posts/search` and `https://arctic-shift.photon-reddit.com/api/comments/search` with the parameters `author=<username>`, `after` and `before` (both exclusive, epoch seconds accepted), `sort=asc` and `limit=100`, paging until the range is covered. Cover the range from 2026-09-07T18:56:00Z to 2026-10-14T07:10:26Z.
2. Apply the eligibility test to the activity in the baseline, and the silence test to the activity from the cutoff to the horizon, with the rules above.
3. Compare the result with the row. Expect some differences: the archive may have backfilled activity since the run was fetched, and it may have dropped content that was deleted since then.

To repeat the whole scan with the tool, run `bot-farm-silence-tracker scan --platform reddit-archive --n 10000 --seed 1638893776` under the default event. The run was scanned with version 0.2.0 and scored with the parameters above. A new scan discovers accounts from the archive as it is at that time, so its sample may not match this one exactly.

Please be kind to the archive: it is a free community service, so keep the request rate low (the tool uses 0.5 requests per second by default) and respect its rate-limit headers.
