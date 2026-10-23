# Contributing

The figures in the [README](README.md) and in [data-analysis.md](data-analysis.md) come from a few large samples, and runs 1 to 3 overlap heavily. More
scans, run by other people on their own computers, make the figures more reliable. A new
scan is not independent of the earlier runs: every draw comes from the same pool of
accounts active in the seed communities in the 30 days before the cutoff (21 of the 23 seed
subreddits contributed accounts), weighted by activity, with an equal quota per subreddit
and a uniform sample within the discovered pool, so a new draw overlaps the
earlier runs heavily (a 3,000-account draw overlaps the existing set by roughly 70%). A
contributed run still helps, because it measures the shared accounts again and adds new
accounts, and subsets of new accounts only are reported separately. The published figures
come from the public Reddit archive, which needs no account or API key. The tool can also
scan Bluesky and X (formerly Twitter); see [Other platforms: Bluesky and X](#other-platforms-bluesky-and-x) for what
each one needs.

## 1. Install the tool

The install steps differ between Windows, macOS and Linux, so follow the section for your
system: [Windows](#windows), [macOS](#macos) or [Linux](#linux). On every system the
installer needs no administrator rights, checks the download's SHA256 checksum, and also
checks its build provenance attestation when the [GitHub CLI](https://cli.github.com/)
(`gh`) is installed and logged in. It then asks which platforms you want to scan. Press
Enter to keep the default, Reddit through the Arctic Shift archive. After the install,
every command in this guide is the same on all three systems. Every other install
option, such as a specific version, the Python tools or Docker, is described in
[INSTALL.md](INSTALL.md).

### Windows

1. Open PowerShell (press the Windows key, type `PowerShell` and press Enter).
2. Paste this command and press Enter:

   ```powershell
   irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
   ```

   It puts the program in `%LOCALAPPDATA%\Programs\bot-farm-silence-tracker` and adds
   that folder to your user PATH. On Windows on ARM it installs the x64 program, which
   Windows runs through emulation. The `curl ... | sh` command for macOS and Linux does
   not work in PowerShell.
3. Close PowerShell, open a new PowerShell window, and check the install with
   `bot-farm-silence-tracker --version`.

Instead of the command, you can download
`bot-farm-silence-tracker-<version>-windows-x64-setup.exe` from the
[latest release](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases/latest),
[check the download](INSTALL.md#check-the-download-first) and double-click it. It also
installs the desktop app. It is not signed, so Windows SmartScreen may say "Windows
protected your PC": click **More info**, then **Run anyway**. The details are in
[INSTALL.md](INSTALL.md#windows).

### macOS

1. Open the Terminal app (in Finder, Applications > Utilities > Terminal).
2. Paste this command and press Enter:

   ```bash
   curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
   ```

   It puts the program in `~/.local/bin` and adds that folder to your PATH in your
   shell's startup file. Files fetched with `curl` are not quarantined, so macOS does not
   block the program.
3. Open a new Terminal window and check the install with
   `bot-farm-silence-tracker --version`.

Instead of the command, you can download the `.pkg` for your Mac
(`bot-farm-silence-tracker-<version>-macos-arm64.pkg` for Apple silicon,
`bot-farm-silence-tracker-<version>-macos-x64.pkg` for Intel) from the
[latest release](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases/latest),
[check the download](INSTALL.md#check-the-download-first) and double-click it. It also
installs the desktop app. It is not signed by Apple: on macOS 15 and later, open System
Settings > Privacy & Security and click **Open Anyway**; on macOS 14 and earlier,
Control-click the file and choose **Open**. The details are in
[INSTALL.md](INSTALL.md#macos).

### Linux

1. Open a terminal.
2. Paste this command and press Enter:

   ```bash
   curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
   ```

   It puts the program in `~/.local/bin` and adds that folder to your PATH in your
   shell's startup file. The installer needs `curl` or `wget`. When BusyBox `wget` is the
   only downloader (as on Alpine), it refuses to run, because BusyBox `wget` may not
   validate TLS certificates; install `curl` and run it again.
3. Open a new terminal and check the install with `bot-farm-silence-tracker --version`.

There is no double-click installer for Linux. The prebuilt programs are for 64-bit x86
and ARM and need glibc 2.28 or newer (for example Ubuntu 20.04, Debian 11, RHEL 8 or
later). On musl-based systems such as Alpine there is no prebuilt program, so the
installer installs the Python package with [uv](https://docs.astral.sh/uv/) instead, and
asks before it installs uv. The details are in
[INSTALL.md](INSTALL.md#the-one-line-installer).

## 2. Run a scan

Start with a small scan to check that everything works. Without `--event`, a scan uses
the default event, `2026-10-takedown` (`bot-farm-silence-tracker events list` shows it):

```bash
bot-farm-silence-tracker scan --platform reddit-archive --n 30
```

Then run a larger one. The published runs drew 10,000 to 30,000 accounts, and any size
helps. Leave out `--seed`, so that your scan draws its own sample from the same
activity-weighted pool as the earlier runs (it overlaps them heavily, and subsets of new
accounts only are reported separately); the seed it draws is printed and stored with the run.

```bash
bot-farm-silence-tracker scan --platform reddit-archive --n 1000 --concurrency 2 --source-option max_rps=1.0
```

`--concurrency 2 --source-option max_rps=1.0` is the setting that the
[Reddit archive section](docs/USAGE.md#reddit-archive-no-api-key) recommends for a faster
scan. Without it, the scan sends at most 0.5 requests per second.

The scan prints a summary table, writes a CSV file named
`bot-farm-silence-tracker-run-<id>.csv` into the folder you ran it from, and stores the run
in the cache database of the event. Note the run id that it prints.

### Other platforms: Bluesky and X

The tool can also scan Bluesky and X (formerly Twitter) for the same event. The figures
in the README and in data-analysis.md so far come from Reddit only. Each platform's records are
kept apart in the registry, under `registry/events/<event-id>/<platform>/`, so a Bluesky
or X scan adds records for its own platform and does not change the Reddit figures.

| Platform | `--platform` | What it needs |
|---|---|---|
| Reddit through the Arctic Shift archive (the default) | `reddit-archive` | Nothing: no account and no API key. |
| Bluesky | `bluesky` | No account in the default mode. A login with an app password is needed for post search, and it changes which accounts are sampled. |
| X (formerly Twitter) | `x` | Your own X developer account with paid, pay-per-use API access, its app bearer token, and an explicit spending cap. |

**Bluesky.** Without a login, a scan samples the accounts that replied to the members of
the bundled public Bluesky lists:

```bash
bot-farm-silence-tracker scan --platform bluesky --n 200 --max-requests 2500
```

A login changes the sample. Post search needs a login: create an app password in the
Bluesky app (Settings, Privacy and security, App passwords; never use your main
password) and set both variables in the terminal that runs the scan. On macOS and Linux:

```bash
export BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE="yourname.bsky.social"
export BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
```

On Windows, in PowerShell:

```powershell
$env:BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE = 'yourname.bsky.social'
$env:BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD = 'xxxx-xxxx-xxxx-xxxx'
```

With a login and the bundled seeds, the same command uses search only: it samples the
accounts with a post in the baseline window that matches one of the seed hashtags and
keywords, and it does not use the bundled lists. To use both, add
`--source-option list_mode=repliers` (the other accepted values are `members` and
`feed`); the sample then mixes two sampling frames, and the scan prints a warning that
says how. The three ways (no login, a login, and a login with `list_mode`) sample
different populations, so say in your pull request which one you used.
`--concurrency` accepts 1 to 3 on Bluesky as well.

**X.** X bills every post and user that the API returns, and a large scan can cost
hundreds to thousands of USD. Create a developer account and an app at
<https://developer.x.com>, buy pay-per-use credits, and set the app's bearer token in
the terminal that runs the scan, with `export BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN="..."`
on macOS and Linux or `$env:BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN = '...'` in
PowerShell. Every X scan needs `--max-usd` (a hard cap in USD), and a scan that searches
for accounts needs `--yes` to confirm the billed search. X's recent search reaches back
only 7 days, and the cutoff of `2026-10-takedown` is more than 7 days ago, so a new
sample for this event needs full-archive search access on your X plan
(`--source-option full_archive=true`).

Choose a cap that covers the whole scan before you start. Discovery costs about $0.0175
for each account in the pool (by default the pool is the sample size on X), and the
timelines cost $0.005 for each post. For 200 accounts with about 25 posts each in the
window, that is about $3.50 for discovery and $25 for the timelines, about $28.50 in all,
so the example below uses a cap of $50. The first command runs the billed discovery,
stores the sample and prints the projected cost of its timelines without fetching them.
The second command fetches them. Replace `RUN_ID` with the run number that the first
command printed:

```bash
bot-farm-silence-tracker scan --platform x --n 200 --max-usd 50 --source-option full_archive=true --dry-run-cost --yes
bot-farm-silence-tracker scan --platform x --from-run RUN_ID --event 2026-10-takedown
```

The second command inherits the cap of the stored run. A different `--max-usd` on that
command conflicts with the stored cap and is refused unless you add `--force`. Some of
X's billing rules have not been checked against the live API yet, so check the
projection before you fetch. The full details for both platforms are in the
[Bluesky](docs/USAGE.md#bluesky) and [X](docs/USAGE.md#x) sections of docs/USAGE.md.

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
  `bot-farm-silence-tracker scan --platform reddit-archive --from-run RUN_ID --event 2026-10-takedown`,
  with the run's number in place of `RUN_ID`.
  Accounts already fetched are reused, so a resume does not pay for them again. See
  [Resuming a run](docs/USAGE.md#resuming-a-run).
- **Your computer.** While a scan runs, the tool asks the system not to sleep. Keep a
  laptop's lid open and its charger connected for a long scan. Keep the cache database on
  a local disk, not on a network file system.

## 4. Share the results in a pull request

Results are shared as records in the [shared registry](docs/USAGE.md#shared-registry), the
`registry/` folder of this repository. Fork the repository, clone your fork, create a
branch, and run these commands from the clone's folder. Replace `RUN_ID` with the run
number that your scan printed:

```bash
bot-farm-silence-tracker registry add --run-id RUN_ID --event 2026-10-takedown --registry registry
bot-farm-silence-tracker registry merge --registry registry
bot-farm-silence-tracker registry validate --registry registry
```

`registry add` reads the accounts from your cache and writes a record for each fully
scanned account with public activity into `registry/events/2026-10-takedown/<platform>/`.
It makes no requests except the live profile check of accounts that would become
candidates. `--flagged-only` leaves out the accounts that are not flagged. `registry merge`
rewrites the registry in its canonical form, and `registry validate` checks it. Commit the
changed registry files, then, with your fork's `main` up to date with this repository, run
the gate's checks locally before you push:

```bash
git add registry
git commit -m "Add scan results for 2026-10-takedown"
bot-farm-silence-tracker registry verify --base origin/main --head HEAD
```

The `git` commands are on separate lines because Windows PowerShell 5.1 does not accept
`&&` between commands.

Then open a pull request into `main` that changes only files under `registry/`. What to
expect from the checks:

- The `Registry gate` check validates the records and re-fetches at least `min(n, 5)` of
  the changed records from the live sources. A record that claims activity that does not
  exist, or that hides later activity, can fail the pull request, and so can too many
  inconclusive checks. The gate cannot re-fetch records on `x` or `reddit`, because those
  need credentials, so it reports them as unverified.
- A pull request that changes registry data and any other file fails the gate. Data and
  code are reviewed separately.
- A pull request that adds or changes up to 15 records, all `scanned` or `candidate` on
  `reddit-archive` or `bluesky`, needs no review.
- A larger pull request, which most scans produce unless `--flagged-only` leaves most
  accounts out, fails the gate
  until the repository owner reviews it, approves its head commit and adds the
  `registry-override` label. The label is removed after every new push, so avoid pushing
  again during the review. An added or changed record on `x` or `reddit`, whatever the
  size of the pull request, a status change to `verified_inauthentic` or `cleared`, a
  removed record and any evidence file change need the same approval.

The full rules are in [Checks on registry pull requests](docs/USAGE.md#checks-on-registry-pull-requests)
and [Repository rules](docs/USAGE.md#repository-rules).

### Add your run to the main graph

The chart at the top of the README is the one main graph for every platform: a run on
the Reddit archive, Reddit, Bluesky or X goes on the same chart, in its platform's own
group. Rates are never pooled across platforms. To add your run, use a second pull
request, separate from the registry one, because the gate fails a pull request that
mixes registry data with other files. In your clone, after `uv sync`, run the commands
below from the clone's folder. The report holds account names, so write it outside the
clone, for example in your home folder, as shown; the script refuses a report inside a
git work tree, and `run-*.json` files are ignored by git as a second guard.

Replace `RUN_ID` with the run number and `YYYY-MM-DD` with today's date (UTC). On macOS
or Linux:

```bash
uv run bot-farm-silence-tracker report --run-id RUN_ID --out ~/run-RUN_ID.json
uv run python scripts/add_run_to_chart.py ~/run-RUN_ID.json --label "Run 1" --as-of YYYY-MM-DD
```

On Windows (PowerShell):

```powershell
uv run bot-farm-silence-tracker report --run-id RUN_ID --out "$HOME\run-RUN_ID.json"
uv run python scripts/add_run_to_chart.py "$HOME\run-RUN_ID.json" --label "Run 1" --as-of YYYY-MM-DD
```

`--as-of` is the date you add the run, which becomes the chart's "as of" date. The script
adds one row of counts for your run's platform to `docs/charts/runs-provisional.json` and
redraws the two chart files next to it. Commit only those three files, never the report.
The script refuses a run it cannot chart honestly and says why, for example a two-phase
run, a run that is still running, a run scored with other parameters or markers than the
published runs (unless you pass `--allow-nondefault-method`, which marks the row and
drops its matched base), or the `fake` demo platform.

In the pull request description, give what a reviewer needs to check the row: the
platform, the run id, the tool version, the seed and seed list digest (`seed` and
`seeds_sha256` in the report), the cutoff, the time the data was scored to, the numbers
of eligible and silent accounts, and a link to your registry pull request for the same
run. Describe the figures as accounts with no activity seen up to their last scan, never
as bots: silence alone is not proof. The details, including how to add rows by hand, are
in [The main graph: every platform](data-analysis.md#the-main-graph-every-platform).

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
