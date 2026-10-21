# Contributing

The figures in the [README](README.md) and in [data-analysis.md](data-analysis.md) come from a few large samples, and runs 1 to 3 overlap heavily. More
independent scans, run by other people on their own computers, make the figures more
reliable. Every scan draws its own random sample, so each new scan adds accounts that the
earlier runs did not see. The scans read the public Reddit archive and need no account or
API key.

## 1. Install the tool

On macOS or Linux, paste this into a terminal:

```bash
curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
```

The installer checks the download's checksum and its attestation itself and needs no
administrator rights. Press Enter at its menu to keep the default platform, Reddit through
the Arctic Shift archive. Then open a new terminal and check the install with
`bot-farm-silence-tracker --version`. The double-click installers for macOS and Windows
and every other option are described in [Install](docs/USAGE.md#install) and in
[INSTALL.md](INSTALL.md).

## 2. Run a scan

Start with a small scan to check that everything works. Without `--event`, a scan uses
the default event, `2026-10-takedown` (`bot-farm-silence-tracker events list` shows it):

```bash
bot-farm-silence-tracker scan --platform reddit-archive --n 30
```

Then run a larger one. The published runs drew 10,000 to 30,000 accounts, and any size
helps. Leave out `--seed`, so that your scan draws its own random sample; the seed it
draws is printed and stored with the run.

```bash
bot-farm-silence-tracker scan --platform reddit-archive --n 1000 --concurrency 2 --source-option max_rps=1.0
```

`--concurrency 2 --source-option max_rps=1.0` is the setting that the
[Reddit archive section](docs/USAGE.md#reddit-archive-no-api-key) recommends for a faster
scan. Without it, the scan sends at most 0.5 requests per second.

The scan prints a summary table, writes a CSV file named
`bot-farm-silence-tracker-run-<id>.csv` into the folder you ran it from, and stores the run
in the cache database of the event. Note the run id that it prints.

## 3. Expect a long scan

- **Time.** The archive is a free community service, and the tool paces its requests to
  stay within the archive's guidance: 0.5 requests per second by default, at most 2, and
  the rate is halved whenever the archive signals load. As a rough guide, fetching the
  histories in the 300-account validation run took 1,033 requests, about 3.4 per account,
  before retries. By default every scan also draws and fetches samples at 3 placebo
  cutoffs in ordinary earlier weeks, which makes about four times the requests of the run
  alone. A scan of 1,000 accounts can therefore take several hours, and a large one can
  take days.
- **Rate limits.** The archive throttles busy periods. During run 4, its timeout refusals
  were 5% to 7% of requests on October 15 to 17 and about 21% on October 20. The tool
  slows down on its own when this happens, and a scan that keeps hitting the limits stops
  and is saved as partial. Never work around the limits (see
  [Ethics and terms of service](#ethics-and-terms-of-service)).
- **Stopping and resuming.** A scan that stops early (a network problem, the archive's
  limits, Ctrl-C) is saved with every account fetched so far, and it prints the exact
  command that continues it, for example
  `bot-farm-silence-tracker scan --platform reddit-archive --from-run <run-id> --event 2026-10-takedown`.
  Accounts already fetched are reused, so a resume does not pay for them again. See
  [Resuming a run](docs/USAGE.md#resuming-a-run).
- **Your computer.** While a scan runs, the tool asks the system not to sleep. Keep a
  laptop's lid open and its charger connected for a long scan. Keep the cache database on
  a local disk, not on a network file system.

## 4. Share the results in a pull request

Results are shared as records in the [shared registry](docs/USAGE.md#shared-registry), the
`registry/` folder of this repository. Fork the repository, clone your fork, create a
branch, and run these commands from the clone's folder, with your run id:

```bash
bot-farm-silence-tracker registry add --run-id <run-id> --event 2026-10-takedown --registry registry
bot-farm-silence-tracker registry merge --registry registry
bot-farm-silence-tracker registry validate --registry registry
```

`registry add` reads the accounts from your cache and writes a record for each fully
scanned account with public activity into `registry/events/2026-10-takedown/`. It makes no
requests except the live profile check of accounts that would become candidates.
`--flagged-only` leaves out the accounts that are not flagged. `registry merge` rewrites
the registry in its canonical form, and `registry validate` checks it. Commit the changed
registry files, then, with your fork's `main` up to date with this repository, run the
gate's checks locally before you push:

```bash
bot-farm-silence-tracker registry verify --base origin/main --head HEAD
```

Then open a pull request into `main` that changes only files under `registry/`. What to
expect from the checks:

- The `Registry gate` check validates the records and re-fetches at least `min(n, 5)` of
  the changed records from the live sources. A record that claims activity that does not
  exist, or that hides later activity, can fail the pull request, and so can too many
  inconclusive checks.
- A pull request that changes registry data and any other file fails the gate. Data and
  code are reviewed separately.
- A pull request that adds or changes up to 15 records, all `scanned` or `candidate` on
  `reddit-archive` or `bluesky`, needs no review.
- A larger pull request, which most scans produce unless `--flagged-only` leaves most
  accounts out, fails the gate
  until the repository owner reviews it, approves its head commit and adds the
  `registry-override` label. The label is removed after every new push, so avoid pushing
  again during the review. Status changes to `verified_inauthentic` or `cleared`, and
  evidence files, need the same approval.

The full rules are in [Checks on registry pull requests](docs/USAGE.md#checks-on-registry-pull-requests)
and [Repository rules](docs/USAGE.md#repository-rules).

## 5. Privacy and framing

- **Registry records name real accounts.** Usernames and IDs are stored base64-encoded,
  which anyone can reverse with one line of code. This is by design: anyone can check each
  record against the public archive, and the gate does so. Treat every registry file as
  containing real account names.
- **Never commit or post your local files.** The CSV and JSON files a scan writes, the
  cache database and any backup of it, `export-ids` files and report drafts hold real
  account names and stay on your computer. `.gitignore` covers the default file names.
- **No verdicts.** A flagged account is a candidate for manual review, not a finding that
  it is a bot. Describe silent accounts as having no activity seen up to their last scan.
  Silence alone is not proof that an account is a bot, and the outage also silenced
  ordinary users.

## Code and documentation changes

Changes to the code or the documentation go through a pull request that the repository
owner reviews. Before you open one, run the same checks as CI:

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run pytest -q
```

## Ethics and terms of service

- Use official platform APIs or openly published research archives through their
  documented APIs, within their terms. Never scrape platform websites, spoof user
  agents, or circumvent access controls or rate limits.
- Store the minimum: handles, ids, timestamps, communities and content hashes. No
  raw post text.
- Output is a list of candidates for manual review. Do not publicly accuse any
  individual account holder of being a bot, a troll or a state actor based on it.
  Corroborate with independent evidence, and treat the people behind accounts fairly.
