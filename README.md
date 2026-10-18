# Bot Farm Silence Tracker

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/charts/runs-provisional-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/charts/runs-provisional-light.svg">
  <img alt="Chart: the share of eligible accounts that went silent in three Reddit runs, in all distinct accounts across them, and in the part of run 3 that is not in runs 1 or 2, with a 95% interval for each, every row scored as of its run's data horizon. Run 1, 3.3 days after the cutoff: 4.50% (4.02% to 5.04%). Run 2, complete, 3.3 to 4.0 days: 3.38% (2.97% to 3.86%). Run 3, partial and not a random draw, 4.0 days: 3.06% (2.65% to 3.53%). All distinct accounts: 3.67% (3.38% to 3.98%). Run 3 accounts not in runs 1 or 2: 3.17% (2.66% to 3.77%). Ordinary weeks: 4.20% at 3 days of silence and 0.86% at 7 days. Hollow markers show a rough matched base rate for each window, from about 3.4% to 4.0%, lower than the 3-day rate. Run 1 is slightly above its matched base and the other rows are consistent with theirs. No row is clearly above an ordinary week. The runs overlap, so there is no verdict yet." src="docs/charts/runs-provisional-light.svg">
</picture>

Three runs of the 2026-10-takedown Reddit scan, all provisional, scored from the cache as of
2026-10-17 14:42 UTC. The cutoff was 2026-10-11 00:30 UTC, but each run is scored as of
the archive's data horizon stored with it, so no run has reached 7 days of silence: run 1
is scored over 3.28 days, run 2 over 3.28 to 4.0 days, run 3 over 4.0 days. The 7-day
test is still pending.

- Run 1 (main sample complete): 288 of 6,399 eligible accounts silent, 4.50%.
- Run 2 (complete, 10,000 fetched): 214 of 6,324, 3.38%. This 214 counts accounts that
  end early only up to 2026-10-14 07:10 UTC (their stored horizon). Re-scoring from the
  cache to the 4.0-day window ending 2026-10-15 00:30 UTC gives 206.
- Run 3 (partial, 9,119 of 20,000 fetched, not a random draw): 182 of 5,943, 3.06%.
- All distinct accounts across the runs (23,631, counted once each): 550 of 15,002, 3.67%.
- Run 3 accounts not in runs 1 or 2 (5,924 fetched): 123 of 3,879, 3.17%.

Ordinary weeks give 4.20% for 3 days of silence. The windows are longer than 3 days, so
a matched base rate is lower. A straight-line interpolation (a rough estimate, not a
measurement) gives about 3.97% at 3.28 days and 3.37% at 4.0 days. The unmatched 4.20%
comparison is therefore conservative. Against the matched values, run 1 is slightly higher
(its interval sits just above 3.97%) and every other row has an interval that contains its
matched value: run 2 and run 3 are a little lower, and the distinct-account row is a little
higher. No row is clearly above an ordinary week. Run 1 and run 3 differ partly because of
their windows and sample makeup, and the rest is not explained. The runs share accounts,
so they are not independent checks and their rows are never summed. There is no verdict
until the 7-day re-score, about 2026-10-18 or later. Silence alone is not proof that an
account is a bot. See [Runs 1 to 3 (provisional)](#runs-1-to-3-provisional) for the
numbers and caveats.

> Candidates for manual review. Output is not a determination that any account is a bot.

Bot Farm Silence Tracker is a research tool for disinformation analysts. It samples
Reddit, Bluesky or X accounts from seed communities and flags accounts that **posted
steadily for 30 days and then went silent at a cutoff**: the moment of a named outage
event, such as the reported outage that the default event in
[`events.toml`](events.toml) describes (see [Outage events](#outage-events)). The
motivating case: data centers reported to host coordinated accounts were reportedly
taken offline, so bots running there would stop together. But the same outage also
cut off ordinary people who used the affected services, so a shared stop means first
of all "affected by the outage". The tool therefore compares every run with placebo
cutoffs in ordinary earlier weeks, tests stop-time clusters against the accounts' own
posting hours, and asks for evidence independent of the outage's timing before an
account becomes a candidate for review (see [Method](#method) and
[Limitations](#limitations)).

## Shared registry chart

The registry is empty until flagged accounts pass review, so 0 is expected here.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/charts/default-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/charts/default-light.svg">
  <img alt="Chart: potential bot accounts in the shared registry that have not posted since the default outage event, cumulative over time by platform, with a histogram of their stop times around the cutoff and its markers. Silence alone is not proof that an account is a bot." src="docs/charts/default-light.svg">
</picture>

Potential bot accounts in the [shared registry](#shared-registry) that have posted
nothing since the reported outage of the default event, redrawn each time registry
data that passed the [checks on registry pull requests](#checks-on-registry-pull-requests)
is merged (see [Method](#method) and [Registry chart](#registry-chart)); silence alone
is not proof that an account is a bot, and the outage also silenced ordinary users of
the services it cut off.

## Publishing your copy

This copy is published by the GitHub account `obviousthrowaway`. Its links, install
commands, `.github/CODEOWNERS` and the User-Agent string already name that account, so
`install.sh` and `install.ps1` run as they are and the User-Agent names this project page.

The project's source names the hosting account with a placeholder, the token that the
command below matches (written there with `[_]` in place of its second underscore). The
placeholder cannot be a GitHub account name (account names have no underscores), so no
one else can register it and serve their own files at these links. Until it is replaced,
`install.sh` and `install.ps1` refuse to run, and the program's User-Agent names no
project page.

If you start from a copy that still holds the placeholder, replace it with your account
name by running this once from the project folder, for example right after unpacking the
.zip (it works with the `grep`, `xargs` and `perl` that ship with macOS and Linux, and it
does not need git; change `your-name` first):

```bash
grep -rlI --null --exclude-dir=.git '_[_]OWNER__' . | xargs -0 perl -pi -e 's/_[_]OWNER__/your-name/g'
```

The `[_]` keeps the command from matching, and so from rewriting, its own text. Afterwards
`grep -rI --exclude-dir=.git '_[_]OWNER__' .` prints nothing. If the folder is already a
git repository, review the result with `git diff` before you commit it. If you fork this
copy instead, replace `obviousthrowaway` with your account name in the same places.

## Install

> **Install in one minute**
>
> The double-click installers are not signed, so macOS and Windows warn before they open
> one. Before you get past that warning, check the file: its SHA256 checksum must match
> its line in the release's `SHA256SUMS.txt`, and with the GitHub CLI,
> `gh attestation verify <file> --repo obviousthrowaway/bot-farm-silence-tracker` must succeed
> ([how](INSTALL.md#check-the-download-first)). The one-line installer runs both checks
> itself.
>
> - **macOS:** download the `.pkg` for your Mac (`macos-arm64` for Apple silicon,
>   `macos-x64` for Intel) from the
>   [latest release](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases/latest)
>   and double-click it. It is not signed: on macOS 15 and later, open System Settings >
>   Privacy & Security and click **Open Anyway**; on macOS 14 and earlier, Control-click
>   the file and choose **Open**.
> - **Windows:** download `...-windows-x64-setup.exe` from the same page and double-click
>   it. If SmartScreen says "Windows protected your PC", click **More info**, then
>   **Run anyway**.
> - **Linux or the command line** (macOS too), paste into a terminal:
>
>   ```bash
>   curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
>   ```
>
>   On Windows, in PowerShell: `irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex`

None of these needs administrator rights. Each asks which platforms to scan; the default
is Reddit through the Arctic Shift archive, which needs no account (see
[Choosing platforms](#choosing-platforms)). Then open a new terminal and check it with
`bot-farm-silence-tracker --version`.

[INSTALL.md](INSTALL.md) has everything else: what the installers put where and how to
uninstall, installing a specific version, downloading the program by hand, Python tools
and Docker, the uv fallback, BusyBox, checking where a download came from, the
environment variables, and installing while the repository is private.

## Quick start

1. **Install** with one of the options above (or see [INSTALL.md](INSTALL.md)), then check `bot-farm-silence-tracker --version`.
2. **Run a small first scan.** This needs no account or API key. It samples 30 Reddit
   accounts from the seed communities through the public research archive. Without
   `--platform`, `scan` uses the default platform from your
   [config](#choosing-platforms), which is `reddit-archive` unless you changed it:

   ```bash
   bot-farm-silence-tracker scan --n 30
   ```

   To try the tool with made-up data first, use `--platform fake`.
3. **Find the results.** The scan prints a summary table and writes a CSV into the folder
   you ran it from, named `bot-farm-silence-tracker-run-<id>.csv` (for example
   `bot-farm-silence-tracker-run-1.csv`). Use `--out results.json` for JSON. The run, with every
   account fetched, is stored in the cache database of the scan's
   [outage event](#outage-events), for example
   `~/.cache/bot-farm-silence-tracker/events/<event-id>/bot-farm-silence-tracker.db`
   (`--db` changes it).
4. **Resume a run** that stopped early (network trouble, a budget, Ctrl-C). The scan prints
   the exact command at the end; it looks like this, with your run id:

   ```bash
   bot-farm-silence-tracker scan --platform reddit-archive --from-run 1 --event <event-id>
   ```

5. **Re-score without fetching again**, for example with a different threshold, or later
   when more days have passed since the cutoff:

   ```bash
   bot-farm-silence-tracker report --run-id 1 --min-silence-days 5 --out rescored.csv
   ```

Two things to keep in mind. Results are **provisional** until at least
`--min-silence-days` (default 3) have passed since the cutoff, because silence may only mean
an account has not posted yet; the tool says so in its output. And every result is a
**candidate for manual review**, not a finding that an account is a bot. See
[Ethics and terms of service](#ethics-and-terms-of-service) before you use it on real accounts.

## Desktop app

If you prefer windows and buttons to a terminal, the desktop app runs the same scans.
Open it with:

```bash
bot-farm-silence-tracker gui
```

The standalone programs above always include it. The Releases page also has a
double-click version: `bot-farm-silence-tracker-macos-arm64.app.zip` or
`bot-farm-silence-tracker-macos-x64.app.zip` (unzip it and open `Bot Farm Silence
Tracker.app`; the checks and the Gatekeeper advice above apply) and
`bot-farm-silence-tracker-gui-windows-x64.exe`. With the Python tools, install the `gui`
extra, for example `uv tool install 'bot-farm-silence-tracker[gui] @
git+https://github.com/obviousthrowaway/bot-farm-silence-tracker'`. The app needs a desktop; on a
server or over SSH it says so and you use the commands instead.

The app has five screens:

- **Platforms**: the platforms you chose and the default one (the same setting as
  `config set-platforms`).
- **New scan**: platform, sample size (30 to start), the
  [outage event](#outage-events) (the events of `events.toml`, your default event
  first) and its cutoff in a date and time picker (in UTC; a line below it shows the
  same moment in your computer's time zone). Pick "Custom date..." or change the
  picker for a local-only custom event, the same as `scan --cutoff`. Then optional seed
  communities and, under "Advanced", the concurrency and request limits. It shows a
  rough time estimate. The chosen event also decides which event's results the Results
  and Run screens show.
- **Run**: progress (with a hint and the time of the last request when nothing has
  completed for two minutes), the scan's log, Cancel and Resume. Cancel stops cleanly, like Ctrl-C:
  the run is saved as `cancelled` and Resume continues it, exactly like `scan --from-run`.
  A run that another window is still scanning is marked "still running in another
  window", and Resume refuses it until that scan ends.
- **Results**: the runs of the chosen event's own store, computed with the cutoff of
  the run you choose, which the screen shows with the event. The flagged accounts (potential bots for manual review, never a verdict)
  and the groups that stopped at the same time, with filters by platform, community,
  stop group, confidence and nearest marker; a map of which communities each flagged
  account was seen in (click a community to filter); a timeline of daily posts with the
  cutoff and the event's [markers](#markers-inside-an-event) drawn as lines (dashed when a
  marker's time is approximate) and a legend; an "Outage event" tab with the event's
  windows and marker notes; "Open
  profile" and "Open community page" links in your browser; and "Export CSV", which
  writes the same file as `export-ids`. "Hide account names" works like `--hide-handles`
  and turns the profile links off. Confidence is high with two or more automation
  signals (a synchronized stop, very regular timing, posting around the clock), medium
  with one, and low with none or for a provisional result. The "Also in event" column
  names the other events whose scans on this computer also sampled the account; each
  event keeps its own results. Times are shown in UTC, with your computer's local time
  after them as a hint (the zone and daylight saving time of that date).
- **Network and privacy**: the same report as `bot-farm-silence-tracker network`.

Scans run inside the app through the same code as `scan`, so they contact the same hosts
and nothing else. Links opened in the browser are requests your browser makes, outside the
program. Credentials still come from the environment variables; the app also lets you
type them for the current session only. Typed credentials stay in memory, are never
written to a file, the config or the database, and are forgotten when the app closes.

## Choosing platforms

`bot-farm-silence-tracker` can scan four platforms. The installer asks which ones you want, and
records the answer in a small config file.

| Platform | `--platform` | Needs |
|---|---|---|
| Reddit through the Arctic Shift archive (the default) | `reddit-archive` | Nothing. No account. |
| Bluesky | `bluesky` | Nothing for the default mode. A login is optional. |
| X | `x` | Your own paid API bearer token (`BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN`). |
| Reddit official API | `reddit` | Approved API credentials (`BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID`, `..._CLIENT_SECRET`, `..._USERNAME`). |

**At install time.** Run in a terminal, the installer shows a short menu; Enter picks
`reddit-archive`. Without a terminal it never prompts: it uses `--platforms` /
`-Platforms` (or `BOT_FARM_SILENCE_TRACKER_PLATFORMS`), or the default. The value is a comma
list, for example `reddit-archive,bluesky`, and an unknown name stops the installer before
it downloads anything. When you do not choose (the default, or Enter at the menu), a
choice you saved earlier is kept. For `x` and `reddit` the installer prints which
environment variables to set and where to get them. It never asks for, reads or stores
credentials: set them in your own shell or environment.

**The config file.** The installer runs `bot-farm-silence-tracker config set-platforms` on the
installed program, so the program is the only thing that writes the file. The file is
TOML:

```toml
platforms = ["reddit-archive", "bluesky"]
default_platform = "reddit-archive"
```

It lives at `~/.config/bot-farm-silence-tracker/config.toml` on macOS and Linux and at
`%APPDATA%\bot-farm-silence-tracker\config.toml` on Windows. Set `BOT_FARM_SILENCE_TRACKER_CONFIG` to
use a different file.

**Changing it later.**

```bash
bot-farm-silence-tracker config show
bot-farm-silence-tracker config set-platforms reddit-archive,bluesky
bot-farm-silence-tracker config set-default bluesky
```

Names are checked against the platforms the program knows. `set-platforms` keeps the
default platform when it is still listed and otherwise makes the first listed platform the
default; `set-default` also adds its platform to the list.

**Which platform `scan` uses.** `--platform` wins; without it, the `default_platform` in the
config; without a config, `reddit-archive`. The list of platforms only records your
choice: you can still scan one that is not on it, and `scan` prints a one-line note to
standard error when your config lists platforms and the one you scan is not among them.

## Outage events

Every scan is measured against an **outage event**: the moment a group of accounts is
expected to have gone silent, such as a reported takedown of the data centers that
ran them. An event gives the scan its cutoff and the default lengths of its windows. The
shared events live in [`events.toml`](events.toml) at the top of the repository, and
the installed program carries a copy:

```bash
bot-farm-silence-tracker events list          # id, cutoff and name; * marks the default
bot-farm-silence-tracker events show <id>     # windows, notes, registry folder, cache database
```

Each event has:

| Key | Meaning |
| --- | --- |
| `id` | A lower-case slug of 3 to 64 letters, digits and `-`, such as `2026-12-takedown`. An id that starts with a year and month must match the cutoff's. Ids starting with `custom-` are reserved. |
| `name` | A plain display name (letters, digits, spaces and `,.()'/:-`). |
| `cutoff` | The moment of the outage, a UTC ISO-8601 date and time (`"2026-12-03T06:00:00Z"`). It may not lie in the future, and events are listed in cutoff order. |
| `baseline_days` | The steady-posting window before the cutoff (the algorithm's default is 30). |
| `min_silence_days` | The silence a result needs before it stops being provisional, and that a cliff record in the registry needs (default 3, at least 1). |
| `cliff_window_hours` | How close to the cutoff the last activity must be (default 48). With markers the silence window can start earlier; see below. |
| `marker_margin_hours` | For an event with markers: how long before the earliest marker the silence window starts (the shared file must write it out; default 12, at most 72). |
| `verified_min_silence_days` | Optional: the silence a record needs before a pull request may set it to `verified_inauthentic` (default 21, and at least `min_silence_days`; 1 to 365). Three days of silence is enough to review an account, not to call it inauthentic. Changing it in the shared file needs the same override as the windows. |
| `notes` | Optional free text. |
| `[[event.markers]]` | Optional earlier moments inside the outage; see [Markers inside an event](#markers-inside-an-event). |

The three windows default to the algorithm's values in a local events file
(`BOT_FARM_SILENCE_TRACKER_EVENTS` names one), but the shared `events.toml` must write
all three out for every event, and the registry gate refuses a file that does not: a
window left to a default in code could otherwise be moved by a code-only pull request.

`default` at the top of the file names the event used when a command gets no
`--event`. The default event is `2026-10-takedown`: its cutoff is the reported start of
the near-total network collapse, and the two earlier strikes reported on data centers
are its markers. Its first definition (the earlier midnight cutoff, without markers) is still
available as the custom event `custom-20261010T0000Z` (`--event custom-20261010T0000Z`),
which reproduces the flagged accounts and the earlier columns of the versions before
markers. The tests pin its output byte for byte against golden files that were
regenerated on purpose when the method gained its controls (the placebo comparison,
the cluster p-values and the steady test's columns), so those files are not the old
versions' bytes.
`legacy_event` names the event that the cache database of older versions belongs to
(see below). The shared file must set it, so a later change of `default` never moves
users' old databases; moving it needs the same override as changing an event.

**Each event keeps its own data.** Its records live in `registry/events/<id>/` of the
shared registry, and its scans in their own cache database,
`<cache>/events/<id>/bot-farm-silence-tracker.db` (`<cache>` is
`~/.cache/bot-farm-silence-tracker` on macOS and Linux and
`%LOCALAPPDATA%\bot-farm-silence-tracker` on Windows; `BOT_FARM_SILENCE_TRACKER_CACHE_DIR`
moves it). The same account can have a separate record in each event; report drafts
say when an account also appears in another event.

### Markers inside an event

An outage can come in steps: earlier strikes that knock out part of the infrastructure
before the main collapse at the cutoff. Such steps are **markers** of one event, not
events of their own. Each `[[event.markers]]` table, after its `[[event]]`, has:

| Key | Meaning |
| --- | --- |
| `id` | A slug like an event id, unique in its event. `cutoff` is reserved for the main outage. |
| `name` | A plain display name. |
| `time` | A UTC ISO-8601 time to the minute, between the baseline start (`cutoff - baseline_days`) and `cutoff + min_silence_days`. Markers are listed in time order. |
| `approximate` | `true` when the time is a guess; labels then say "approx." and the app draws the marker dashed. |
| `time_note` | Where the time comes from (for example "Reported time of first statements; strike was earlier overnight."). |
| `notes` | Free text, shown by `events show` and the app's "Outage event" tab. |
| `sources` | Optional list of plain-text source names (no links needed). |

**Times are as reported.** The default event's cutoff and marker times come from news
reports and have not been independently verified; the notes say so, and every output of
a run with markers repeats it.

**The window rule.** Let `onset` be the earliest marker's time minus
`marker_margin_hours`. Then:

- the baseline window ends at `min(cutoff, onset)`, so a decline caused by an earlier
  strike does not pollute the baseline, and it is `baseline_days` long;
- the silence window starts at `min(cutoff - cliff_window_hours, onset)` and ends at the
  cutoff, so an account that went silent at any marker is a cliff, not only one that
  stopped in the `cliff_window_hours` before the cutoff.

The margin covers a marker whose reported time is later than the moment it stands for
(the first statements after an overnight strike). For the default event the earliest
marker is about 65.5 hours before the cutoff; with a 12 hour margin the silence window
starts at 2026-10-07T18:56Z and the baseline is 2026-09-07T18:56Z to 2026-10-07T18:56Z.
An event without markers keeps its classic windows exactly.

**Nearest-marker labels.** Each flagged account is labelled with the moment its silence
began nearest to: one of the markers, or `cutoff` (the main outage), with the hours from
it to the last activity (negative: before it). At the same distance the earlier moment
wins, because a silence follows its cause. Each stop-time cluster is labelled by its
first stop. The labels appear in the `report` and `scan` output (the columns
`nearest_marker`, `nearest_marker_offset_hours`, `nearest_marker_approximate` and
`cluster_nearest_marker`, appended after every other column, so runs without markers
keep their columns), in `export-ids`, in the summary table, in report drafts for
moderators, and in the app (a column, a filter and the timeline). A run stores the
markers it was scored with, so `report` and `scan --from-run` keep them like its cutoff.

**Adding a marker.** Add a `[[event.markers]]` table under its event, in time order,
in a pull request that changes only `events.toml`; `bot-farm-silence-tracker events show
<id>` prints the markers and the resulting windows. See below for which marker changes
need the override label.

### Picking an event with `--event`

`scan`, `report` (both `--run-id` and `--registry`), `export-ids`, `registry add` and
`registry evidence` take `--event <id>`:

```bash
bot-farm-silence-tracker scan --platform reddit-archive --event 2026-12-takedown --n 500
bot-farm-silence-tracker report --run-id 3 --event 2026-12-takedown
bot-farm-silence-tracker registry add --run-id 3 --event 2026-12-takedown
```

Without `--event`, commands use the `default_event` of your
[config file](#choosing-platforms), else the default of `events.toml`. Change yours with
`bot-farm-silence-tracker config set-default-event 2026-12-takedown`. A scan prints the
event it uses. Window flags such as `--baseline-days` still override the event's
windows for one scan.

A run belongs to the event it started with. `scan --from-run` continues it only under
that event, even with `--force`, and the printed resume command names it
(`--event <id>`); `report`, `export-ids` and the registry commands refuse a run of
another event too. `registry validate`, `registry merge`, `registry verify` and
`registry gate` always cover every event.

### Custom dates

For a cutoff that is not a shared event, give `--cutoff`:

```bash
bot-farm-silence-tracker scan --platform reddit-archive --cutoff 2026-12-03T06:00Z --n 200
```

That makes a local-only custom event, `custom-20261203T0600Z` (the id holds the cutoff,
to the minute), with the default windows and its own cache database. The same
`--cutoff`, or `--event custom-20261203T0600Z`, finds it again. Custom events are never
written to the shared registry: `registry add`, `registry evidence`, report drafts and
`--skip-registered-within-days` refuse them. To share results for a new date, add the
event first.

### Adding an event (for example, a December takedown)

Open one pull request that changes only `events.toml`, adding a table at the end (the
file is in cutoff order):

```toml
[[event]]
id = "2026-12-takedown"
name = "December 2026 bot data center takedown"
cutoff = "2026-12-03T06:00:00Z"
baseline_days = 30
min_silence_days = 3.0
cliff_window_hours = 48.0
notes = "Where the cutoff comes from, with sources."
```

The registry gate validates the file (syntax, id rules, unique ids, cutoff order and
range, size limits) and passes a new event **without any label**; its summary lists the
added event for reviewers. The event's first records can come in the same pull request
or a later one, in `registry/events/2026-12-takedown/`.

**Changing an existing event is guarded.** Changing an event's name, cutoff or windows,
renaming it (a new id) or deleting it fails the gate unless the pull request has the
`registry-override` label together with an approving review of its head commit (the
same two proofs as every other override; see [Checks on registry pull
requests](#checks-on-registry-pull-requests)). Otherwise anyone could move a cutoff to
re-mask accounts. The same holds for moving `legacy_event`, which decides where users'
old cache databases go. The summary lists each such change as high-impact. Notes may
change freely, and a change of the `default` event is flagged for reviewers but needs
no label.

Markers follow the same idea. Moving a marker could re-label accounts, so changing a
marker's `time` or `approximate` flag, or removing a marker, needs the override label
and an approving review. So does any marker change that moves the event's windows,
such as a new earliest marker or a new `marker_margin_hours`: that is a window change.
Adding a marker that leaves the windows as they are needs no label, and neither does a
change of a marker's name, time note, notes or sources; the summary lists both.

### The cache database before events

Versions before outage events kept every scan in one database,
`<cache>/bot-farm-silence-tracker.db`. On the first command that uses the default event
(more exactly, the `legacy_event`), that database (with its `-wal`, `-shm` and spend
fallback files) is moved into that event's folder by an atomic rename, under a lock, so
two commands started at once never split it. Every open of a database holds a shared
lock on it (in the `locks` folder next to it) and the move holds that lock
exclusively, so the database is never moved while a scan or a window of this version
has it open: the move is then skipped with a note and tried again on a later run, and
an open that starts during the move waits for it and then uses the new path. If the
event folder is on another device, the files are copied and the originals kept as
`<file>.pre-events-backup`. The move is ordered so that a crash cannot lose spend
records: the spend fallback files are copied first and deleted only after the main
file has moved, and `<cache>/migration-state.json` records a move in progress, so the
next open finishes it. Each move is noted in the log and in `<cache>/migration.log`. A
database that is busy or cannot be moved is used in place and moved on a later run.
Opening the old path after the move opens the moved database. If a database appears at
the old path again after the move (an older version, or a process that had the old
database open, created it), the tool warns, because its runs are not in the event's
database; open it with `--db`. The database itself is not changed: its runs read as before, and runs from
before events belong to the shared event with the same cutoff, else to the custom event
of their cutoff (continue those with `--event custom-... --db <path>`).

**Runs of an earlier definition.** When a shared event's cutoff changes, runs scanned
with the old cutoff do not move to the new definition. They belong to the clearly
named local event of their own cutoff ("Earlier definition of `<id>`", id
`custom-YYYYMMDDTHHMMZ`); for the default event's old cutoff that is
`custom-20261010T0000Z`. They stay readable in the event's database, `report` shows them
with their own cutoff, and `scan --from-run` (even with an old resume command that names
the shared event) continues them with their original cutoff and says so. The registry
never takes them. `registry add`
refuses a run whose cutoff is not its event's, and points out windows that differ.
A resume command printed by an older version (`scan --from-run N --cutoff <date>`, with
no `--event`) still works: when the date is a shared event's cutoff and the run is
that event's, the scan says so and continues it under that event.
`--db` always names a database directly.

## Security and privacy

Bot Farm Silence Tracker sends no telemetry. Scans contact only the platform API hosts on a
per-platform allowlist, over https, and the HTTP client refuses any other host before
sending anything. Credentials come only from `BOT_FARM_SILENCE_TRACKER_*` environment
variables and are sent only to the hosts listed for them. To see exactly which hosts
each platform contacts and which credential goes where (by variable name, never the
value):

```bash
bot-farm-silence-tracker network
bot-farm-silence-tracker network --platform bluesky --json
```

Scans ignore proxy and CA settings from the environment (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, netrc) and trust only the bundled CA
certificates. Behind a proxy that you must use, pass `--trust-env` (or set
`BOT_FARM_SILENCE_TRACKER_TRUST_ENV=1`); the scan then warns that the proxy can see the
hosts it contacts, and that a CA from those variables could let the proxy read the
requests, credentials included.

See [SECURITY.md](SECURITY.md) for what is stored and where, credential handling per
platform, verifying downloads, the installers' network use, and how to report a
vulnerability privately.

## Runs 1 to 3 (provisional)

The chart is at the top of this page.

These are provisional results of three Reddit archive scans of event `2026-10-takedown`
(different seeds). They show counts only. All were scored from the cache as of
2026-10-17 14:42 UTC, against a cutoff of 2026-10-11 00:30 UTC. Run 1 fetched its whole
main sample and is now drawing placebo samples. Run 2 is complete. Run 3 is partial (9,119
of 20,000 fetched), so its numbers will change as it completes.

One rule applies to every row: each run is scored as of the archive data horizon stored
with it, and no later activity counts. That is 2026-10-14 07:10 UTC for run 1 and
2026-10-15 00:30 UTC for runs 2 and 3. No run has reached 7 days of silence, so the 7-day
comparison is still pending. A longer window needs the archive to cover it and the accounts
to be fetched again.

The tool's own counts do not all follow that rule, so they are not used here:

- The scan's console count for run 2 is 205 silent. It includes posts made after the horizon
  up to the time the accounts were fetched, so it is a longer window and not comparable
  with the other rows.
- `report` on a run that is still running (runs 1 and 3) scores up to the run's reference
  time, which is later than its horizon (run 3: 2026-10-16 17:31 UTC, giving 126 silent over
  a longer window than the stated 4.0 days, and run 1: 287). `report` on a completed run
  (run 2) clips at the stored window end and gives 214. For this table, runs 1 and 3 were
  re-scored on a backup copy with the reference time set to the stored horizon.
- A re-score reads the cache as it is now, which can include rows merged in by later runs
  (the tool's documentation says so for runs without a stored window end), so a count can drift between re-scores. A follow-up
  to make the report consistent is needed and is not part of this change.

| Measure | Run 1 | Run 2 | Run 3 (partial) |
| --- | --- | --- | --- |
| Accounts fetched and scored | 9,999 of 10,000 | 10,000 of 10,000 | 9,119 of 20,000 |
| Scored through (data horizon) | 2026-10-14 07:10 UTC | 2026-10-15 00:30 UTC | 2026-10-15 00:30 UTC |
| Silence window | 3.28 days | 3.28 to 4.0 days | 4.0 days |
| Eligible (steady posters in the baseline) | 6,399 | 6,324 | 5,943 |
| Went silent | 288, or 4.50% (95% Wilson interval 4.02% to 5.04%) | 214, or 3.38% (2.97% to 3.86%) | 182, or 3.06% (2.65% to 3.53%) |
| Of those, with a signal independent of the outage's timing | 9 | 6 | 7 |
| Synchronized clusters tested | 57 | 38 | 30 |
| Of those, significant (adjusted permutation p-value of 0.01 or less) | 0 (smallest 0.0115) | 0 (smallest 0.44) | 0 (smallest 0.99) |
| Rough base rate matched to the window | about 3.97% | 3.37% to 3.97%, mix-weighted 3.51% | about 3.37% |

Across runs, counted once per distinct account:

| Measure | All distinct accounts | Run 3, accounts not in runs 1 or 2 |
| --- | --- | --- |
| Distinct accounts fetched and scored | 23,631 | 5,924 |
| Eligible | 15,002 | 3,879 |
| Went silent | 550, or 3.67% (3.38% to 3.98%) | 123, or 3.17% (2.66% to 3.77%) |
| Of those, with a signal independent of the outage's timing | 18 | 6 |
| Rough base rate matched to the windows | mix-weighted 3.57% (3.37% to 3.97%) | about 3.37% |

Ordinary weeks, from placebo cutoffs on Sep 6, 13 and 20 2026 (692,252 eligible
accounts): silent 3 days, 4.20% (weekly values 3.87% to 4.61%); silent 7 days, 0.86%
(weekly values 0.82% to 0.89%).

How the distinct-account rows are built:

- The key is platform and account ID. An account scored in more than one run counts once,
  using its record from the run with the longest scored window (ties go to the lowest run
  number). Rows of different runs are never added together. This rule lowers the rate a
  little: eligible accounts scored over 3.28 days are silent at 4.67% (4.13% to 5.29%) and
  over 4.0 days at 3.16% (2.83% to 3.52%), and the rule moves accounts from the first to the
  second group. Keeping each account's first run instead gives 3.75% (3.46% to 4.07%)
  against 3.67%, so the distinct-account row is biased low by about 0.08 points.
- The runs overlap heavily. Runs 1 and 2 share 2,292 accounts. Of the 9,119 accounts
  fetched in run 3, 3,195 (35%) are also in run 1 or run 2 (2,055 in run 1, 2,025 in run 2,
  885 in all three), and 5,924 are new. The new-only row, run 3 accounts not in runs 1
  or 2, is the part of run 3 that does not repeat an earlier sample.
- The 23,631 distinct accounts are the union of the three fetched samples. Accounts shared
  by runs 1 and 2 got the same eligibility and silence result in both.
- Cluster tests are run level: they are not recomputed for the distinct-account rows.

How to read it:

- No row is clearly above an ordinary week. Run 1 (4.50%) is 0.30 percentage points above
  the pooled 3-day ordinary-week value (4.20%), below the highest of the three ordinary
  weeks (4.61%), and its interval contains the pooled value. Every other row is below it.
  The windows differ (see the next bullets), so this is not a verdict.
- The windows are longer than 3 days, so a matched base rate is lower than 4.20%, and the
  unmatched comparison is conservative: it is biased toward finding no excess.
  Interpolating in a straight line between the 3-day rate (4.20%) and the 7-day rate
  (0.86%) gives about 3.97% at 3.28 days and 3.37% at 4.0 days. That is a rough
  estimate, not a measurement, and the true curve is unknown.
- Against those rough matched values, run 1 is slightly higher: its interval (4.02% to
  5.04%) sits just above 3.97%, by 0.05 points at its lower end. For every other row the
  interval contains the matched value: run 2 (3.38%) against a mix-weighted 3.51%, run 3
  (3.06%, 2.65% to 3.53%) and its new-only part (3.17%, 2.66% to 3.77%) against 3.37%, and
  the distinct-account row (3.67%, 3.38% to 3.98%) against 3.57%. So nothing here shows a
  clear excess, and nothing is clearly below the matched base either.
- Run 1 and run 3 differ (4.50% against 3.06%, with intervals that do not overlap), and
  only part of that is explained. On the 1,356 accounts that are eligible in both, run 1
  has 52 silent and run 3 has 39, and no account is silent in run 3 only; the 13 that
  differ posted again in run 3's longer window. Across the whole runs, the difference
  between their matched base rates is 0.6 points, and reweighting run 3's communities to
  run 1's eligible mix moves its rate from 3.06% to 3.34%. About 0.9 of the 1.44 point gap
  is accounted for by window and sample makeup, and the rest is not explained by these
  checks. The window effect on the shared accounts (0.96 points) is larger than the 0.6
  point matched-base interpolation adjustment, and the two adjustments may not simply
  add. The runs share accounts, so they are not independent checks.
- Run 3 is a partial and not a random draw. The scan fetches in sample order, grouped by
  community, so the first accounts fetched over-represent some communities (for example one
  community is 9.2% of run 1's first quarter and a different one leads its last). Run 2's
  silent rate among eligible accounts varies along that order (2.38%, 3.30%, 4.14% and
  3.87% across its four quarters). Its count will change as more accounts are fetched.
- Run 2 mixes windows. For 2,232 of its 10,000 scored accounts (1,550 of its 6,324
  eligible ones) the archive data ends earlier (2026-10-14 07:10 UTC), so they are scored
  over at most 3.28 days, and the rest over 4.0 days. Silent shares by window: 3.94% (61
  of 1,550) over 3.28 days and 3.20% (153 of 4,774) over 4.0 days. The distinct-account row
  mixes windows too: 5,027 of its 15,002 eligible accounts are scored over 3.28 days.
- No cluster of stops is statistically significant in any run. The largest cluster in run
  1 (11 accounts) has an adjusted p-value of 0.0115, just above the 0.01 cut-off, so it is
  not counted. Only 9 of the 288 silent accounts in run 1, 6 of the 214 in run 2 and 7 of
  the 182 in run 3 have a signal that does not depend on the outage's timing.
- Silence alone is not proof that an account is a bot. The outage also silenced
  ordinary users, and a share of accounts goes quiet in any week.
- Nothing here is a final verdict. A verdict needs at least 7 days of silence after the
  cutoff (once the archive covers 2026-10-18 00:30 UTC), when the runs are
  re-scored and compared with the 7-day ordinary-week rate of 0.86%, and "verified" needs
  21 days of silence.
- The ordinary-week rates come from placebo cutoffs on three Sundays (00:30 UTC), counted
  in the Reddit dumps of August and September 2026 by a separate script. Its eligibility
  rules are meant to match this tool's but have not been checked to be identical, and the
  placebo samples of the runs have not been drawn yet. Treat the comparison as rough.

The chart is rendered from [`docs/charts/runs-provisional.json`](docs/charts/runs-provisional.json)
by `scripts/render_run_chart.py`:

```sh
uv run python scripts/render_run_chart.py docs/charts/runs-provisional.json docs/charts
```

## Method

1. **Discover.** The platform adapter lists accounts seen active in the seed
   communities (`config/seeds.yaml`, subreddits for Reddit and search queries for X).
   Discovery oversamples: pool size is `--pool-factor` (default 3, or the platform's
   own default: 1 on X, 1.2 on reddit-archive) times `--n`.
   Discovery is cutoff-aware. An account seen only after the cutoff can never be a
   cliff, so adapters prefer accounts with activity before the cutoff (ideally in the
   baseline window). When a platform cannot reach back that far, or a seed is skipped
   or invalid, the adapter emits a warning that is printed after the scan and stored
   in the run summary.
2. **Sample.** Uniform reservoir sampling over the pool: 3000 accounts by default,
   `--n N` for a different size, `--seed S` for reproducibility. Without `--seed` a
   random seed is drawn, printed and recorded with the run, so every run can be
   repeated. The run also records the tool version, the SHA-256 of its seed list and
   its reference time (and whether `--now` gave it); every output file's metadata
   repeats them (`seed`, `scan_tool_version`, `seeds_sha256`, `now`, `now_given`). With
   `--new-accounts-days D`, accounts are hydrated first and only those created in the
   last D days survive to the draw.
3. **Fetch.** For each sampled account, fetch activity from the baseline start
   (`cutoff - baseline_days`) to now. The [placebo cutoffs](#placebo-cutoffs) draw and
   fetch samples of their own after that. Results are
   cached in SQLite, one database per
   [outage event](#outage-events)
   (`~/.cache/bot-farm-silence-tracker/events/<event-id>/bot-farm-silence-tracker.db`; on
   Windows under `%LOCALAPPDATA%\bot-farm-silence-tracker\events\<event-id>\`; override
   with `--db`), so reruns do not refetch. On Windows, when `LOCALAPPDATA` is unset, the POSIX-style path
   under the home directory is used instead.
   Only content hashes are stored, never raw text. The sample itself is stored with
   the run before fetching starts (see [Resuming a run](#resuming-a-run)). If one
   account fails with an ordinary API error, it is recorded with status `error` and
   the scan moves on; after `--max-consecutive-errors` (default 10) failures in a
   row the scan stops as partial. Authentication failures, the request or spend
   budget and rate limits beyond the retry cap also stop the scan, and the run is
   still saved with every account fetched so far, so `report --run-id` works on it.
4. **Score.** Per account:
   - *Baseline window* is `[cutoff - 30d, cutoff)`.
   - *Eligible (steady):* active on at least 12 of the 30 days and at least 20
     posts (`--min-active-days`, `--min-posts`), at least one post in the last 7 days
     before the baseline ends (`--recent-days`; for an event with markers the
     baseline ends before the earliest marker), and weekly counts whose coefficient
     of variation is at most 1.0 (`--max-weekly-cv`; the standard deviation of the
     whole weeks' counts over their mean). The last two rules keep out an account
     that posted a burst early in the baseline and had gone quiet before the outage.
     The output records them in the `weekly_cv` and `recent_posts` columns, and
     report drafts state the numbers instead of "posted steadily".
   - *Cliff:* eligible, no activity at or after the cutoff, last activity within 48
     hours before the cutoff (`--cliff-window-hours`).
   - *Provisional:* a cliff where less than `--min-silence-days` (default 3) have
     passed since the cutoff. The CLI warns loudly, and output is marked.
   - `p_silence = exp(-rate * silence_days)`: the Poisson chance of this much
     silence given the baseline rate. Lower is more anomalous.
   - Other metrics: active-day ratio, posts per day, inter-arrival coefficient of
     variation, hour-of-day entropy, account age at the cutoff and status
     (active, suspended, deleted, private, error). Bluesky output adds two columns
     right after `data_end`: `reposts_after_cutoff` (reposts after the cutoff
     that were not counted as activity) and `deactivated` (a deleted account the
     platform reports as deactivated; it may come back). Its summary adds the
     matching counts and the count of accounts that opted out of logged-out views
     (`opted_out`, `dropped_opted_out`). Other platforms' output has none of these.
   - *Truncated:* the history stops before the baseline start. The account is scored
     on what came back, flagged in the `truncated` column and counted in the summary.
     The `partial_reason` column says why: `platform_cap` (the platform stopped
     returning older history) or `budget` / `error` (the fetch was interrupted). A
     truncated history may be a false negative.
   - *Composite score* in 0..1 for cliff accounts (weights live in
     `SCORE_WEIGHTS` in `src/bot_farm_silence_tracker/scoring.py`).
5. **Cluster.** Cliff accounts whose last activity falls within the same
   `--sync-minutes` (default 30) window share a cluster id. A cluster of at least 3
   is tested against a **permutation null**: each account's stop time is drawn again
   in the silence window from its own baseline posting by UTC hour of day, and
   accounts discovered through the same thread (Reddit archive, Bluesky repliers)
   move together, keeping their offsets. The cluster's p-value is the share of 9999
   permutations whose fullest `--sync-minutes` window anywhere in the run holds at
   least as many stops, so the window's position is part of the test, and the smallest
   possible p-value is 0.0001. **Multiple clusters:** every cluster is compared with
   the null distribution of the run's largest cluster (a single-step maximum
   statistic), which already controls the chance of any false cluster among all of the
   run's clusters. No further correction is applied on top: Holm's method on top of
   the maximum would be doubly conservative, and with the 999 permutations used before
   it made 0.01 unreachable in any run with more than ten clusters. The
   `synchronized_stop` signal fires at an adjusted p-value of at most 0.01. The
   summary lists every tested cluster (`cluster_tests`: `p_permutation` and
   `p_adjusted`, which are equal), and each row has `cluster_p_adjusted`. The observed
   clusters are found greedily (a window anchored on the earliest unassigned stop),
   and a greedy window never holds more stops than the best sliding window the null
   measures, so the test is valid but conservative. The draws are seeded by the run,
   so the same run always gives the same p-values. A significant cluster
   shows that the stops are more synchronized than the accounts' posting habits
   explain, not why: a shared stop at the outage is what everyone the outage
   affected shows, so `synchronized_stop` is **not evidence of automation on its
   own** (see [Limitations](#limitations)).
6. **Signals.** Each cliff account's `signals` column lists the signals that fired:
   `synchronized_stop` (timing), `regular_timing` (inter-arrival coefficient of
   variation at most 0.5) and `round_the_clock` (hour-of-day entropy with the
   Miller-Madow correction of at least 4.3 bits over at least 50 posts). The last two
   are measured on the baseline and do not depend on the outage's timing;
   `independent_signals` counts them, and the app shows that count as "signals: N".
   None of them is calibrated, and no count makes an account a bot.
7. **Placebo.** See [Placebo cutoffs](#placebo-cutoffs).
8. **Report.** Ranked CSV or JSON, plus a summary table on stdout. Every output file
   starts with the review-only notice and the placebo comparison.

### Placebo cutoffs

A cliff rate means little on its own: steady posters take breaks and stop for good
every week. So every scan is compared with **placebo cutoffs** one, two and three
weeks (`--placebo-weeks`, 3 to 8) before the real one, at the same weekday and hour.

**Every placebo cutoff gets a sample of its own.** Discovery only finds accounts with
activity in its window, and that window ends at the real cutoff. An account that
stopped for good at a placebo cutoff three weeks earlier has no activity there, so it
can never be in the run's sample: re-scoring the run's own sample at the placebo
cutoffs would count only accounts that paused and came back, push the placebo rate
toward zero and call almost any real rate "clearly above". (A simulation with no
outage at all, with discovery over the last 2 or 7 days, called every one of 20
replicates clearly above.) So for each placebo week `k` the scan runs the same
discovery again, with the same seeds, sample size and pool factor, a seed derived from
the run's seed and `k`, and the discovery window, the reference time, the markers and
the data horizon all moved back `k` weeks; it then fetches those accounts over the
moved baseline and silence window and scores them at the moved cutoff. Every pass,
real and placebo, selects its accounts the same way relative to its own cutoff.
Placebo data is not cached, so it never mixes with the run's own.

**Cost.** This repeats discovery and fetching once per placebo week, so a scan with
the default 3 placebo weeks makes about four times the requests of the run alone. The
scan says so when it starts, honors the same request budget and spend cap, and reports
the extra requests (`summary.placebo.requests` and a line under the placebo headline).
`--placebo-weeks 0` turns the comparison off. A placebo sample that stops early (the
budget, a rate limit) leaves the run without a comparison: a partial sample is not
selected like the run's own. Placebo samples are drawn only after the run's own sample
was fetched in full; `scan --from-run` draws them when it completes a partial run.

**Which platforms have it.** The comparison needs discovery that can search earlier
weeks by time:

| Platform | Placebo comparison |
|---|---|
| `reddit-archive` | Available: the archive searches by time (`after` / `before`). |
| `bluesky` | Available with search and list seeds. Not with feed seeds (feed generators keep about a day of posts). |
| `reddit` | Not available: the API lists only about the newest 1000 items of a subreddit, with no time bounds. |
| `x` | Not available with recent search (7 days). With `full_archive=true` only when asked for with `--source-option placebo_discovery=true`, because it repeats billed discovery and fetches. |
| `fake` | Available (synthetic). |

Where it is not available, no comparison is computed from the run's own sample, and
every output says `PLACEBO: no placebo comparison: not available on ...` with the
reason.

**The interval.** Each pass compares the accounts of its own sample that are active,
whose history the platform did not cap and whose data reaches back to that pass's
baseline. The comparison gives, for the cliff rate (cliff accounts per eligible
account) and for each signal's firing rate, the rate `r` at the real cutoff, the
placebo mean `m` and standard deviation `s` over the `K` placebo samples, and the
excess `r - m` with a 95% interval: the prediction interval of one more independent
sample, `excess ± t(K - 1) * sqrt(max(s², v_p) / K + max(s², v_r))`. `s²` estimates
the variance of one sample's rate (its sampling noise and the change from week to
week); it is never taken below the binomial variance of a placebo rate (`v_p`, at the
pooled placebo rate) for the placebo mean, nor below the binomial variance of the
real rate (`v_r`) for the real rate. Both binomial rates are taken as
`(x + 0.5) / (n + 1)`, so a rate of zero still has noise. With only 3 to 8 placebo
samples the interval is wide, and Student's `t` with `K - 1` degrees of freedom keeps
it honest, and conservative. In a simulated null with no outage (the fake source's
churn world, 250-account samples, discovery over the last 2, 7 or 30 days, scored 3
or 21 days after the cutoff) the comparison called the real rate clearly above in
none of 204 replicates, against the 2.5% a one-sided 95% interval allows (see
`tests/test_placebo_null.py`; `RUN_SLOW_TESTS=1` runs the long version). The price is
power: on those sample sizes a synchronized stop of 15% of the accounts at the real
cutoff was found in 20 of 20 replicates, of 10% in 9 of 20 and of 5% in none. Larger
samples and more placebo weeks narrow the interval.

It appears in the `report` and `scan` output and summary table (for example
`cliff rate 14% vs placebo 11% ± 3% (excess +3 points, 95% interval -5 to +11)`), in
every output file's notices and its `summary.placebo`, in the app's Results header,
in evidence drafts and in report drafts. When the interval of the excess reaches zero
or below, every one of them says prominently **NOT CLEARLY ABOVE THE PLACEBO**: the
cliff rate is not clearly higher than in ordinary weeks (the difference was not
resolved), so these cliffs alone are not evidence of the outage, let alone of
automation. That is not a finding that the rates are equal.

`report --run-id` reuses the placebo samples' counts stored with the run; with changed
scoring options it has no comparison, because the placebo samples are not cached.
Two-phase runs (`--baseline-sample-rate`) fetch only some accounts in full, so they
have no placebo comparison. Runs scanned before placebo samples existed have none either;
`report` says so, and a new scan gives one.

Placebo samples also leave out accounts that are suspended or deleted when they are
fetched. A placebo sample is fetched weeks after its cutoff, so more of its accounts
can be gone by then than in the run's own sample; when removals follow stops, that
lowers the placebo rate a little. Read a small excess with that in mind.

## Setup

```bash
uv sync
uv run bot-farm-silence-tracker scan --platform fake --n 500 --seed 1 --out results.csv
uv run bot-farm-silence-tracker report --run-id 1 --out results.json
uv run pytest -q
uv run ruff check
```

Run on real data (once adapters exist):

```bash
uv run bot-farm-silence-tracker scan --platform reddit --max-requests 5000
uv run bot-farm-silence-tracker scan --platform x --query "#Ukraine" --query "#NATO" --max-requests 500
```

Useful flags: `--seeds path.yaml`, repeatable `--subreddit` / `--query`,
`--new-accounts-days D`, `--baseline-days`, `--format csv|json`,
`--max-requests N` (hard request budget), `--max-usd D` (X only, required there),
`--yes` (confirm billed discovery; required on X unless `--from-run`),
`--dry-run-cost` (store the sample and project the cost without fetching activity),
`--pool-factor F` (discovery pool = F x n; default 3, or 1 on X and 1.2 on reddit-archive),
`--active-within-days D` (reddit-archive only: discover authors active in the last D days,
clipped to the baseline window before the cutoff, see the archive section),
`--concurrency K` (reddit-archive only, 1 to 3: fetch K accounts at once behind the one
shared pacer, see "Concurrency" in the archive section),
`--baseline-sample-rate P` (reddit-archive only, opt in: pre-check every account with
existence queries, then fetch the full baseline only for cliff candidates and a
fraction P (0.1 to 0.2 recommended) of the rest, see
[Two-phase baseline sampling](#two-phase-baseline-sampling---baseline-sample-rate)),
`--include-unavailable` (list suspended, deleted and private accounts too; accounts
that opted out of logged-out views are never listed, only counted),
`--source-option key=value` (repeatable adapter option, see below),
`--from-run RUN_ID` (resume a stored sample, see below),
`--max-consecutive-errors N` (default 10; requests that fail without any answer, as when
the network drops, are retried after a pause and do not count toward it),
`--allow-sleep` (let the computer sleep during the scan, see below),
`--allow-stale-source` (run on archive data that ends too soon, see below),
`--cache-ttl-hours H` (default 24; how long cached activity counts as fresh).

**Keeping the computer awake.** A long scan can take hours, and a sleeping computer loses
its network, so the scan stalls until the next brief wake-up. While a scan runs, the
program therefore asks the system not to start an idle sleep (the screen may still turn
off): an IOKit power assertion on macOS (or `caffeinate` if that fails),
`SetThreadExecutionState` on Windows, and `systemd-inhibit` on Linux when it is installed.
Pass `--allow-sleep` to opt out; the desktop app has a "Keep this computer awake while
scanning" checkbox, on by default and saved in the config file. This cannot stop a laptop
from sleeping when you close its lid on battery power, when you choose Sleep yourself, or
when the battery runs out; keep the lid open and the charger connected for a long scan.
If a scan does stall, the desktop app shows how long it has been without a completed
request, and `--from-run` continues it.

When an adapter reports a spend estimate, the CLI prints it on every exit,
including errors and Ctrl-C. Warnings collected before a fatal exit (an
authentication failure, an adapter error, a bug or Ctrl-C) are printed at WARNING
level before the scan exits.

Keep the cache database on a local disk. It uses SQLite's WAL journaling, which is
not safe on network file systems such as NFS or SMB.

### Lagging sources

Some sources, such as a research archive that ingests the live platform with a
delay, do not reach the present. An adapter reports how far its data goes, and the
scan checks this once, before discovery (or, with `--from-run`, before fetching),
and prints the lag on stderr right away. Scoring then uses the earlier of now and
the data horizon, so the silence length, `p_silence` and the provisional flag are
measured against the data that exists. The run summary and the output record both
`horizon` and `effective_now`. If the data ends before the cutoff plus
`--min-silence-days`, silence cannot be judged yet, and the scan stops before it
samples or spends anything. Pass `--allow-stale-source` to run anyway; every result
is then marked provisional. If an adapter that reports a horizon cannot tell it
this time (the archive does not answer), the scan treats the source as stale the
same way: it stops before sampling unless `--allow-stale-source` is given.
Authentication, budget and rate-limit errors from that check end the scan as they
do anywhere else. Activity seen after the cutoff always rules out a cliff, even
when it is past the reported horizon. Accounts cached by a run whose data stopped
at its horizon are refetched on `--from-run` once the source has caught up by more
than `--cache-ttl-hours`.

### Credentials and adapter options

Every environment variable Bot Farm Silence Tracker reads starts with `BOT_FARM_SILENCE_TRACKER_`, followed by
the platform and the name, for example `BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID` or
`BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN`. Each adapter lists the variables it requires and the
optional ones it uses when they are set. Credentials are never printed: they are
kept out of the config's `repr`, and tracebacks do not show local variables.

Non-secret adapter settings are passed with `--source-option key=value`, repeated
as needed. Each platform section lists the keys it understands. For example:

```bash
uv run bot-farm-silence-tracker scan --platform fake --source-option population=2000
```

Do not pass passwords or tokens as `--source-option`; they would end up in your
shell history. Use the platform's `BOT_FARM_SILENCE_TRACKER_*` variables instead.

### Resuming a run

Every scan stores its sample (the accounts it drew, in order) in the cache database
before it fetches anything, with a per-account status. A run that stopped early, for
any reason, can be continued without new discovery:

```bash
uv run bot-farm-silence-tracker scan --platform x --from-run 7
```

`--from-run` reuses run 7's sample and skips discovery. It creates a new run with
its own id, which `report --run-id` can re-score. A scan holds a lock on its run chain
for as long as it runs (an operating-system file lock in the `locks` folder next to the
database, plus a heartbeat with its process id, host and time in the runs table), so a
run that is still being scanned, by another process or another window of the app,
cannot be continued at the same time; the resume is refused before any request. A run
that still says `running` because its scan was killed has no lock and can be continued
at once. A scan stopped with Ctrl-C or Cancel is saved with the status `cancelled`. The platform must match, and so must
the [outage event](#outage-events): a resume under another `--event` or `--cutoff` is
refused, even with `--force`. A run
that ends partial prints the exact command that continues it, including `--db` when
you used a database other than the default. On Windows the command is quoted for
PowerShell; `cmd.exe` does not understand its single quotes, so paste it into
PowerShell.

**Options come from the stored run.** The resumed run takes the event and its cutoff, the baseline
length, every scoring threshold, `--max-requests`, the sample settings (`--n`,
`--seed`, `--pool-factor`, `--new-accounts-days`) and the `--source-option` values
from run 7, so you do not have to repeat them. On X that includes the budget:
`--max-usd`, `max_discovery_usd` and `budget_scope` are stored with the run (they are
not secrets), so a resume inherits the cap. `--max-requests` is a fresh budget of
the same size for the new run. You may repeat an option with the same value. A
different value is a conflict, and the scan refuses to start and lists every
conflict. A `--source-option` the stored run did not have counts as a conflict
too, so adding a new option on resume needs `--force`. Pass `--force` to let your
values win; each override is printed as a warning. Values are compared
normalized: `true`, `yes` and `on` match a stored `True`, and `2000.0` matches
`2000`.

Options that look like secrets are never stored. A key counts as one when one of
its name segments (split on `_`, `-` and camelCase) is `token`, `secret`,
`password`, `passwd`, `pass`, `key`, `apikey`, `api_key`, `bearer`, `auth`,
`credential`, `credentials`, `cookie` or `session`, or when the adapter lists it in
`Source.secret_options`. So `api_key`, `access_token` and `authToken` are not
stored, while `keywords` and `max_keys` are. A partial run's output lists the
options that were not stored, so you can pass them again when you continue; they
are not checked for conflicts. Keep secrets in `BOT_FARM_SILENCE_TRACKER_*` variables anyway.

**Fetched accounts are reused.** Accounts that run 7 already fetched are taken from
the cache whatever their age, as long as the cached history reaches back to the
baseline start, so a resume never pays for them twice. Other accounts are fetched
unless their cached activity is younger than `--cache-ttl-hours` and ends within
that long of the run's reference time. Accounts that failed with status `error` are
retried. Before fetching, the scan logs how many accounts it will fetch and how many
it reuses. Pass `--refresh` to refetch every account instead.

Reused histories stop where the earlier fetch stopped. If that is more than
`--cache-ttl-hours` before the new run's reference time, the run warns: activity
after the earlier fetch is not seen, so a reused account can look silent when it is
not. Rerun with `--refresh` before relying on such results.

A resume never moves the cutoff: the cutoff belongs to the run's event.

Discovery on X is billed and recent search only covers the last 7 days, so discover
once (or store a sample with `--dry-run-cost`, which ends with status `sampled`),
then resume as often as needed. A resumed X scan pays only for the timelines it
fetches, and its spend counts against the same cap as the run it continues (see
[Spend ledger](#spend-ledger)).

### Two-phase baseline sampling (`--baseline-sample-rate`)

Off by default: without the flag every scan is a full census, exactly as before.
With `--baseline-sample-rate P` (reddit-archive only, `0 < P <= 1`) the scan fetches
in two phases, because most sampled accounts can never be cliffs. In the n=300
archive validation run, 174 of 300 accounts (58%) had activity after the cutoff, yet
each one got a full baseline fetch.

1. **Pre-check.** Every sampled account gets existence queries only: the newest
   comment in `[cutoff - cliff_window_hours, now)` (`sort=desc, limit=1`), and, unless
   that comment is at or after the cutoff, the newest post in the same window. The
   account goes into a stratum: **A** (activity at or after the cutoff: cannot be a
   cliff), **B** (its newest activity is in the window before the cutoff: a cliff
   candidate), **C** (nothing in the window: cannot be a cliff either) or **U**
   (suspended, deleted or private). The queries run through the archive's usual
   slow-query ladder; a query the archive keeps timing out on is narrowed once to
   start at the cutoff, and a query that is narrowed or fails makes the account B, so
   no account is ruled out on incomplete data. The slow-query guard
   (`max_slow_queries`) counts accounts here as in the fetch: an account whose
   pre-check had any query fail the whole ladder counts once, so 3 such accounts in a
   row stop the scan as rate-limited. When more than 50% of at least 20 fresh
   pre-checks were incomplete, the scan logs a warning at once and repeats it in the
   summary; it does not abort, because an incomplete pre-check only costs (the
   account is fetched in full) and never biases the estimates.
2. **Baseline.** The full baseline `[baseline_start, now)` is fetched, exactly as a
   census fetches it, for **every** B account and for a random fraction P of A and C.
   B comes first; then the selected A and C accounts in ascending order of their
   draw. Each account's draw is a hash of the run's salt (the `--seed` when given)
   and its user id, so the subsample is reproducible.

**Choosing P.** Use 0.1 to 0.2. The pre-check costs one or two requests per account
whatever P is, so a large P pays for the pre-check and most of a census on top: at
P = 0.5 or more the scan can cost as much as a census, or more. The model below
applies the design to the cached n=300 run (pages of 100 items per kind, as the
archive adapter requests them; 422 retries and window splits are left out, and they
fall mostly on the wide baseline queries that phase 2 skips):

| P | phase 1 requests | phase 2 requests | total | vs census (1,033) |
|---|---|---|---|---|
| 0.1 | 438 | 221 | 659 | -36% |
| 0.2 | 438 | 311 | 749 | -27% |
| 0.25 | 438 | 356 | 794 | -23% |
| 0.5 | 438 | 582 | 1,020 | -1% |
| 1.0 | 438 | 1,033 | 1,471 | +42% |

(Strata in that run: A 174, B 57, C 69; 162 accounts were settled by the comments
query alone.)

What stays exact and what becomes an estimate:

- **Cliff accounts, their scores and the synchronized-stop clusters are exact for
  the pre-check snapshot** once every B account is fully fetched. If the pre-check
  is right, only B can hold cliffs; B is fetched in full, and clustering only looks
  at cliffs. The pre-check is a snapshot, though: an A or C account that gained
  activity just before the cutoff after it was pre-checked (late ingestion) is a
  cliff the unsampled part of A and C can hide. The summary therefore says "Cliff
  accounts (exact for the pre-check snapshot)", and prints the drift line below.
  While some B account is still pending or its fetch failed, it says "Cliff
  accounts (lower bound)" with the number of candidates not yet fetched, and the
  cliff rate is marked incomplete.
- **Data that changed after the pre-check.** A sampled A or C account whose full
  baseline shows a cliff-candidate pattern the pre-check did not see (a mover) keeps
  its stratum and its weight `1 / P_eff`: it was fetched only because its draw
  selected it, and unsampled accounts may have changed the same way. Counting it as
  a certain B (`pi = 1`) would bias the estimates. Any mover, in the sample or
  fetched past the stop point, and whether or not it is a cliff, makes the cliff
  count an HT estimate ("Cliff accounts (estimate)", `cliffs_exact` false), even
  when no sampled account is a cliff; the lower bound also includes the cliffs
  fetched past the stop point (`cliffs_after_stop`). The scan warns, `report`
  repeats the warning (it recomputes the movers from the cache), and `--from-run
  --refresh` pre-checks everyone again. A resume without `--refresh` warns when the
  pre-checks it reuses are older than `max(cliff_window_hours, 24h)`.
- **The drift line.** Every two-phase run with an unsampled A or C account prints
  "k of n sampled A/C accounts disagreed with their pre-check; up to U unsampled
  accounts may also have changed (95%), so the cliff count could be low by at most
  U." Here k movers were seen among the n fully fetched sampled A and C accounts,
  and U is the upper end of the exact 95% binomial interval for the number of movers
  minus k (at most the number of unsampled accounts). With k = 0 at P = 0.15, U is
  22: a run that saw no mover cannot rule out 22 hidden ones. The JSON has it as
  `summary.sampling.precheck_drift` (`moved_sampled`, `moved_after_stop`,
  `responded`, `unseen_upper95`, and `unseen`, the number of unsampled or failed A
  and C accounts).
- **The eligible total, the cliff rate (cliffs / eligible), eligible accounts silent
  since the cutoff but not cliffs, and eligible accounts active after it are
  Horvitz-Thompson estimates**: each fully fetched account counts `1 / pi`, with
  inclusion probability `pi = 1` for B and U and `P_eff` for A and C.
- **`P_eff`, the rate actually reached.** It equals P when phase 2 finished. When a
  run stops part-way (budget, rate limit, Ctrl-C), `P_eff` is the draw of the first
  selected account, in draw order, whose fetch is not done; every selected account
  with a lower draw is done, so the accounts below `P_eff` are an exact
  Bernoulli(`P_eff`) sample. Accounts at or above it are treated as not sampled, even
  when a `--concurrency` worker already finished them (`fetch_status` `after_stop`):
  `P_eff` follows the draw order, never the order in which fetches completed. A fetch
  that was interrupted part-way counts as not done. `--from-run` continues and raises
  `P_eff` back to P.
- **95% intervals.** Every A and C account has the same `pi = P_eff` and an
  independent draw, so the number of sampled accounts with a property is exactly
  Binomial(M, `P_eff`), where M is the unknown count among A and C. The interval for
  M inverts the two one-sided binomial tests (the Clopper-Pearson construction for
  the binomial n with known p) and is clipped to what is possible; the exactly known
  B and U part is added on. When the cliff count has no sampling error, the
  cliff-rate interval is the eligible-total interval mapped through `cliffs /
  eligible` (Fieller's interval when the numerator variance is zero); when it is
  estimated too, both totals' intervals are taken at 97.5% and combined (Bonferroni).
  The intervals are conservative: their coverage is at least 95% (the binomial
  intervals are exact tests, and the Bonferroni combination only adds to it).
- **What the intervals cover.** The phase-2 subsampling only. They are conditional
  on the phase-1 classification (the pre-check is taken as correct) and on the
  discovery sample: they bound what a census of the **same** sample would have
  reported **if the data had not changed since the pre-check**, not the error of
  drawing the sample from the seed communities. When the data did change and no
  mover landed in the sample, the cliff interval can miss the truth; the drift line
  bounds by how much. The
  summary also gives the unbiased HT standard error, `sqrt(m (1 - P_eff)) / P_eff`,
  and its delta-method counterpart for the rate.
- **Nonresponse.** A sampled A or C account whose baseline fetch failed is not
  dropped: the lower end of each interval counts it as not having the property, the
  upper end as having it, so the interval contains the one a full response would
  have given (the point estimate assumes failed fetches resemble the responders:
  missing at random, MAR). A B account not fully fetched widens every upper bound by one and makes
  the cliff count a lower bound. The lines say "widened for incomplete fetches" when
  this applies, and `--from-run` retries the failures.

Output: the summary table shows the unweighted eligible count as "Eligible, fully
fetched (unweighted)" and prints the strata, `P_eff` when it is below P, and the
estimates with their intervals; the CSV gets them as `#` comment lines and the
`stratum`, `inclusion_prob`, `weight` and `fetch_status` columns; the JSON gets an
`estimates` list and the full `summary.sampling` block. In the JSON `summary`,
`eligible` is `null` for a two-phase run (in a census it is the exact eligible
count, and no single number means that here); use `eligible_observed` (unweighted,
fully fetched accounts only), `eligible_estimate` and `eligible_ci95`, and
`cliff_rate_estimate`, `cliff_rate_ci95` and `cliff_exact`. An A or C account outside
the subsample has `fetch_status` `precheck_only`: it is not scored, it is not counted
as ineligible, and it is not in any observed count. A selected account whose baseline
fetch has not finished is `phase2_pending`, a failed one `phase2_failed`.

The stratum, inclusion probability and selection of every account are stored with
the run. `--from-run` keeps them and the salt (so the subsample never changes), never
repeats a finished pre-check unless `--refresh` is given, and inherits the rate; a
different rate is refused, even with `--force`. Fresh data always wins over a stored
stratum: an account a refreshed pre-check or its full baseline fetch now finds
unavailable becomes U, and one they now show as a cliff candidate becomes B with
`pi = 1`. `bot-farm-silence-tracker report` re-scores from the stored design and recomputes the
estimates; it refuses a larger `--cliff-window-hours` than the run's, because the
pre-check only ruled accounts out for the window it ran with (a smaller one is fine).
The mode composes with `--concurrency`, `--active-within-days`, `--max-requests`,
truncation handling, `--allow-stale-source` and provisional results.

**When not to use it.**

- Small samples. With few eligible A and C accounts in the subsample the intervals
  are wide; below a few hundred accounts a census costs little anyway.
- A sample rate of 0.5 or more: it costs about as much as a census (see above).
- When the analysis compares cliffs with the full histories of active accounts (for
  example a comparison group of non-cliff eligible accounts). Only a fraction P of them
  has a baseline, so such an analysis must weight each one by `weight` (1 / P_eff),
  and per-account comparisons simply have fewer accounts.
- When the cliff window may be widened later in `report`.
- On other platforms. The flag is refused for them: the pre-check needs a cheap
  existence query over a time window, and Reddit's own API lists only about the
  newest 1000 items with no time bounds, while every extra X request is billed.

### Spend ledger

Paid adapters record their estimated spend in a `spend_events` table, keyed by
platform, UTC calendar day (wall-clock time) and run, in integer micro-dollars. The
databases in the cache folder (every outage event's, custom cutoffs included, and the
old single database) share one spend ledger, `<cache>/spend.db`; a database you pass
with `--db` keeps its spend in its own table. A USD cap can then hold across reruns
and across events. With a daily scope, the default, the adapter compares the cap with
everything spent on that platform today, including earlier runs and runs of other
events. The first time a newer version opens an event's database, the spend that an
older version recorded in that database is copied into the shared ledger, once (the
copy is kept up to date if the older version is still running). With a run scope, it counts the current run and every run it
was resumed from: `--from-run` chains share a root run id, so resuming does not reset
the cap. The adapter's section of this README names the option that picks the scope.
The figures are the adapter's estimates, not the platform's invoice. The run summary
reports the run's own spend as `spend_usd` and the whole chain's as
`chain_spend_usd`, and the summary table prints the chain total for a resumed run.

The ledger is safe to share between scans running at the same time. The database
uses WAL journaling and waits up to 30 seconds for a lock. Adapters reserve a
request's worst-case cost before sending it, and the cap check and the reservation
are a single `BEGIN IMMEDIATE` transaction, so two scans cannot both slip under the
cap. After the response, the reservation is settled to the actual cost. If writing
billed spend to the database fails, the ledger rolls the failed write back (so it
can never be committed later and counted twice), retries, and as a last resort
appends it to `<db>.spend-fallback.jsonl` next to the database and logs an error.
Amounts in that file still count toward the cap and toward the run's `spend_usd`.
A settle records the absolute amount for its reservation, in the database and in
the file, so settling twice, or settling after a fallback, never counts an amount
twice. If the database cannot record a reservation at all, the request is refused
with `BudgetExceeded` rather than sent without one.

Each cache database has a random id, and every fallback record carries it, so a
fallback file left behind by a deleted or replaced database is ignored (with a
warning) instead of being matched against unrelated spend rows. A record that
matches no spend row never lowers a total. Records written by older versions,
which carry no id, only ever raise a total, and are ignored once they are more
than 7 days old. Every record also has a random nonce, so two identical charges stay
two records. At the start of every scan the file is first renamed to
`<file>.merged-<timestamp>-<nonce>`, under a lock file that every append also takes,
so spend written while the merge runs goes to a fresh file and is never lost. The
current database's records in that archive (and in any archive an interrupted scan
left unmerged) are then merged into `spend_events`; until they are, they still
count. A fully handled archive gets a `.done` suffix and is kept for audit.
Reservations older than an hour whose run has finished are closed at their
reserved (worst-case) amount. A request reserved just before
midnight UTC and settled after it counts toward both days' caps.

### Reddit

Official OAuth API only (`bot_farm_silence_tracker.sources.reddit:RedditSource`). It uses the
app-only `client_credentials` grant, or the `password` grant when a password is set.

**Getting access.** Reddit ended self-service API access on 2025-11-11. New access
needs pre-approval under the Responsible Builder Policy: file a Developer Support
ticket describing this research use (read-only, no raw content stored). Once
approved, create a "script" or "web" app at <https://www.reddit.com/prefs/apps>. The
client id is the string under the app name, and the client secret is shown on the app
page. Credentials issued before the change keep working.

**Environment variables:**

| Variable | Meaning |
|---|---|
| `BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID` | Required. App client id. |
| `BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET` | Required. App client secret. |
| `BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME` | Required. Your Reddit username, sent in the User-Agent: `python:bot-farm-silence-tracker:<version> (by /u/<name>)`. |
| `BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD` | Optional. Switches to the `password` grant (script apps). Under that grant the username is also sent to the token endpoint, together with the password. Use a dedicated research account. |

**Options** (`--source-option key=value`, repeatable):

| Key | Default | Meaning |
|---|---|---|
| `qpm` | 100 | Queries per minute. Resizes the shared pacer. |
| `discovery` | `both` | `listing`, `threads` or `both`; see Discovery below. |
| `thread_window` | `auto` | The `t` of `/r/{sub}/top`: `week`, `month`, `year`, or `auto`. `auto` takes the shortest window that reaches half-way into the baseline. A window must reach back to the cutoff, or the adapter warns and reads no threads. |
| `threads_per_sub` | 5 | Threads whose comment trees are read per subreddit. |
| `thread_comment_limit` | 200 | Comments requested per thread. |
| `thread_depth` | 3 | Reply depth requested per thread. |
| `discovery_budget` | 0.3 | Share of `--max-requests` that discovery may use. |

**Rate limits.** The free tier allows about 100 queries per minute per OAuth client,
averaged over 10 minutes (older docs say 60). The shared HTTP client is the only
pacer: it is sized from the adapter's `default_rps` (100/60 per second) and burst of
10, and `qpm` resizes it. A 429 with a `Retry-After` header waits that long. A 429
without one waits for `X-Ratelimit-Reset` (seconds until the window resets), up to 900
seconds; a longer wait stops the scan as partial and it can be resumed with
`--from-run`.

**Discovery.** `/r/{sub}/new` and `/r/{sub}/comments` list newest first and Reddit
serves about 1000 items per listing. Authors seen only after the cutoff cannot be
cliffs, so discovery yields only authors with an item dated before the cutoff
(inside the baseline window when it is known) and keeps paging past newer items to
find them. With `discovery=threads` or `both` it also reads `/r/{sub}/top?t=...`,
keeps threads created inside the baseline window (`[baseline_start, cutoff)`), and
takes authors from each thread's comment tree whose comment is dated within
`[baseline_start, cutoff)` too. That reaches pre-cutoff
authors on subreddits too busy for the listings to go back far enough. Comments on
threads created before `baseline_start` are not read in thread discovery, even when
the comments themselves are dated inside the window. A listing
that hits the cap before reaching the cutoff is reported as a warning.

**Name filter.** Authors named like bots are skipped: `[deleted]`, AutoModerator, and
any name ending in `bot` (case-insensitive). The suffix rule also drops a few real
people, for example a user called `abbot`; that loss is accepted.

```bash
export BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID=... BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET=... \
  BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME=your_name
uv run bot-farm-silence-tracker scan --platform reddit --subreddit ukraine --subreddit europe \
  --n 500 --max-requests 5000 --source-option discovery=both
```

**Request budget.** Every attempt counts against `--max-requests`: the token
request, each token refresh (about hourly, and again after a 401), every retry after
a 429 or a 5xx, and each page.

- Per sampled account: 1 profile lookup plus 1 to 10 history pages of 100 items. A
  30-day baseline is usually 2 to 3 pages, so about 4 requests typically, 11 at most.
  A 403 on the history costs no extra call when the profile lookup came first; it is
  one extra call only for accounts whose profile was already cached.
- Discovery: each listing page is one request. Per subreddit that is up to 10 pages
  for `/new` and 10 for `/comments`, plus for threads 1 `top` request and up to
  `threads_per_sub` comment-tree requests, so up to 26 per subreddit with the
  defaults. Lazy: it stops once the pool (`pool_factor` times `--n`) is full, and it
  stops at about `discovery_budget` of `--max-requests`; the last call may overshoot by its retries.
- `--new-accounts-days D`: one extra `/about` request for every pooled account
  (up to `pool_factor` times `--n`), because accounts are hydrated before the draw.
  Sampled accounts then reuse the cached profile, so their own lookup is free. On the
  example below that adds up to about 9,000 requests, so raise `--max-requests`.
- Retries: budget 5% on top of everything above.

Example, `--n 3000` with the 23 default subreddits (pool 9000): discovery at most
about 600 requests, accounts about 12,000 (worst case 33,000), plus a handful of
token requests. With the 5% retry allowance that is about 13,300 requests typically
(12,600 times 1.05, plus a few token calls), which at 100 per minute is roughly 2
hours 15 minutes of wall-clock time. The worst case is about 35,300 requests
(33,600 times 1.05), roughly 5 hours 55 minutes. Set `--max-requests` near 15000 for
the typical case, and use `--from-run` to continue a run that stopped early.

### X

> **Cost warning.** X bills per post and per user returned. A default scan
> (3000 accounts) can cost **hundreds to thousands of USD**, almost all of it in
> timelines. Every X scan needs an explicit `--max-usd`, and any scan that runs
> discovery needs `--yes`. Run `--dry-run-cost --yes` first with a small cap.

**Getting access**

1. Create a developer account at <https://developer.x.com> and create a project and app.
2. Choose pay-per-use and buy credits. Pay-per-use is the default plan since
   2026-02-06.
3. Generate the app's **bearer token** (app-only auth is all this tool needs).
4. Export it, never commit it:

   ```bash
   export BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN="..."
   ```

**Required flags.**

- `--max-usd D` (or `--source-option max_usd=D`): the hard spend cap in USD. There is
  no default. Without it the scan refuses to start, prints a rough projection and
  suggests a value.
- `--yes`: confirms that billed discovery may run. Before any request, the scan prints
  the projected discovery cost and stops unless `--yes` is given. A `--from-run` scan
  skips discovery and does not need it.

**Pricing and the budget guard.** Pay-per-use is `$0.005` per post returned and
`$0.010` per user returned. Users returned by the author expansion in search count too.
The adapter tracks money in integer micro-USD and works like this:

- Before every attempt, retries included, it refuses to send a request whose worst
  case (`max_results` posts, plus one user per post for searches) would push the
  estimate past the cap. With the cache database attached (always, from the CLI),
  that worst case is reserved in the spend ledger in the same transaction as the cap
  check, so two scans sharing a `--db` cannot both pass the cap.
- Every reservation is settled exactly once: to the actual charge when the response
  arrives, to the worst case when the request may have reached X without a usable
  response, and to $0 when it was never sent or the response bills nothing (402,
  429, 5xx). An error at any point cannot leave a reservation behind.
- After every response it charges what came back. Each post and user id is charged
  once per UTC day, matching X's same-day de-duplication. The checks before sending
  stay worst case.
- A request that failed in transit after it may have reached X is charged at its worst
  case. A 402, or a 429 whose body reports an exhausted usage cap, stops the scan as a
  budget stop.
- Before an account's first timeline page, the account's projected cost (from its
  lifetime `tweet_count` and age) must fit in what is left, so the scan stops before
  paying for a timeline it cannot finish.
- Timelines that cannot matter are skipped at no cost: accounts whose lifetime
  `tweet_count` is below `--min-posts`, and accounts created after the cutoff. They are
  stored with `meta.skipped`, counted in the spend line and in the summary's
  "Skipped" row (not in "Scored"). No activity is cached for a skip, so
  `report --run-id` with a lower `--min-posts` cannot score them; it prints the
  `scan --from-run` command that fetches them. A skip never deletes activity an
  earlier run paid for: it is recorded in the run's sample only.
- A timeline that stops part-way (the cap, or an error) is cached with its real
  coverage: X returns newest posts first, so the cached window starts at the oldest
  post fetched, and the account carries `meta.partial_fetch` with the reason. The
  next run (`--from-run`, or any run within `--cache-ttl-hours`) fetches only the
  older part, with `end_time` set to where the cached data starts, and merges it, so
  pages already paid for are not bought again. A history X itself cut at about 3,200
  posts is not refetched, since X returns nothing older.

When the cap trips, the scan stops, keeps what it has and is marked partial. Continue
with `--from-run RUN_ID` and a cap that has room. Prices live in `POST_PRICE_USD` and
`USER_PRICE_USD` in `src/bot_farm_silence_tracker/sources/x.py`.

**The cap is cumulative per UTC day.** Every charge is recorded in the spend ledger (see
[Spend ledger](#spend-ledger)). By default (`budget_scope=day`) the cap covers everything
recorded for X on the current UTC day, across all runs of every outage event and custom
cutoff in the cache folder (or, with `--db`, all runs on that database), so rerunning
a scan, or starting one for another event, after a scan that stopped at `--max-usd 50`
does not get another $50 the same day. Use
`--source-option budget_scope=run` to count only the current run and the runs it
continues with `--from-run` (its resume chain), so a resume never resets the cap.

**Discovery has its own cap.** Search spend in a run may use at most 30% of the budget
left in the scope when discovery starts, or `--source-option max_discovery_usd=D` when
that is lower. When it is reached, discovery stops with a warning, the sample is drawn
from the pool found so far, and timelines are still fetched. A scan whose discovery cap
is below one worst-case search page ($1.50: 100 posts plus 100 authors) is refused
before any request; raise `--max-usd` or continue a stored sample with `--from-run`.
The discovery share is tracked per process: two scans that start discovery at the same
moment on the same `--db` each take 30% of what is left, so together they can spend up
to 60% on discovery. The overall `--max-usd` cap still holds across them (it is
reserved atomically in the ledger); only the split between discovery and timelines
can shift. Run discovery scans one at a time if that split matters. On X the discovery pool defaults to `--pool-factor 1`
(the pool is the sample size); pass `--pool-factor` to oversample.

**Estimated cost for `--n 3000`.** Timelines cover the 40-day window (30 baseline days
plus the days since the cutoff). Discovery is billed too: the adapter projects about
`$0.0175` per account in the pool (one user plus one or two posts).

| Avg. posts per account in the window | Timeline cost (3000 accounts) |
|---:|---:|
| 10 | $150 |
| 25 | $375 |
| 50 | $750 |
| 100 | $1,500 |
| 250 | $3,750 |

| Discovery pool | Discovery cost |
|---|---:|
| `--pool-factor 1` (3000 accounts, X default) | about $53 |
| `--pool-factor 3` (9000 accounts) | about $158 |

Regular posters (the ones that matter for this study) post often, so expect the
bottom rows. Ways to spend less: lower `--n`, keep `--pool-factor 1`, raise
`--min-posts` (more zero-cost skips), and resume with `--from-run` instead of
rediscovering.

**Estimate before you spend.** `--dry-run-cost` runs discovery and sampling (billed,
counted against the caps, and it needs `--yes`), stores the sample as a run and prints
a projection of the timeline cost: the expected cost, a high estimate (twice the
expected posts per account), what is already spent in the budget scope, and whether the
total fits the cap. It fetches no timelines. Fetch the stored sample later without
paying for discovery again:

```bash
uv run bot-farm-silence-tracker scan --platform x --n 200 --max-usd 20 --dry-run-cost --yes
# prints "Stored the 200-account sample as run 7 ..."
uv run bot-farm-silence-tracker scan --platform x --from-run 7  # inherits --max-usd 20 from run 7
```

`--dry-run-cost --from-run RUN_ID` prints the projection for a stored sample without
any request.

**Adapter options** (`--source-option key=value`):

| Key | Default | Meaning |
|---|---|---|
| `max_usd` | none (required) | Hard spend cap in USD; same as `--max-usd`. |
| `max_discovery_usd` | 30% of the budget left | Cap on search spend in this run. |
| `budget_scope` | `day` | `day`: cap covers today's ledger across runs. `run`: this run and the runs it resumes. |
| `post_price`, `user_price` | `0.005`, `0.010` | Override the prices (must be `>= 0`). |
| `lang` | none | Appends `lang:<code>` to every search query. |
| `exclude_retweets` | `false` | Appends `-is:retweet` to every search query. |
| `full_archive` | `false` | Uses `/2/tweets/search/all` (needs full-archive access). |
| `field_param` | `tweet.fields` | Or `post.fields`; the docs disagree on the name. |

Boolean options accept `true`/`false`, `yes`/`no`, `on`/`off` and `1`/`0`.

**Example.**

```bash
export BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN="..."
uv run bot-farm-silence-tracker scan --platform x --n 500 --max-usd 100 --yes \
  --out x-run.csv
```

**Timing: discovery must run early.** Discovery searches with `end_time` set to the
cutoff, so every discovered author posted before it, and with `start_time` set to the
baseline start when the search window reaches that far. Recent search
(`/2/tweets/search/recent`) covers only the **last 7 days**, so run discovery within
about **6 days after the event's cutoff** (`events show <id>` prints it). After that,
recent search cannot reach the cutoff: the adapter warns, skips discovery at no cost, and you need either
`full_archive=true` (needs access on your plan; rate limited to 1 request per second)
or a stored sample (`--from-run`). The cache keeps the sampled accounts, so you can
rescore later.

**Rate limits.** The shared HTTP client paces X at 1 request per second. On a 429
without `Retry-After`, it waits until `x-rate-limit-reset` (up to 900 seconds; longer
waits stop the scan, which can be resumed). The adapter also keeps per-endpoint counters
(search 450 per 15 minutes, timelines 10,000, user lookup 300, full-archive 1 per
second).

**Errors.** A 403 on one user's timeline without an app-level reason is recorded as a
private account. A 403 that names an app-level reason (`client-not-enrolled`,
`client-forbidden`, `unsupported-authentication`), or three unexplained 403s in a row,
stops the scan as an authentication error. The bearer token never appears in log lines
or error messages.

**Not yet verified against the live API:** whether expanded users are billed (the guard
assumes yes), whether X de-duplicates charges per UTC day as assumed, the pagination
parameter name for search (`next_token`), whether the fields parameter is
`tweet.fields` or `post.fields` (the adapter has an option for the second), and how far
back a user timeline reaches (possibly the latest 3,200 posts; histories that hit it
are flagged as truncated).

### Reddit (archive, no API key)

`--platform reddit-archive` reads public Reddit posts and comments from the
[Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) archive
(`https://arctic-shift.photon-reddit.com`), a free community archive with a keyless
search API. It needs no credentials.

This source exists because Reddit's unauthenticated endpoints are blocked (the `.json`
URLs answer 403 or a login redirect) and API access is not always available. Bot Farm Silence Tracker
does not work around that block: it does not scrape reddit.com, spoof a browser, or use
proxies. It only talks to the Arctic Shift API, and it identifies itself honestly with
`User-Agent: bot-farm-silence-tracker/<version> (research; +https://github.com/obviousthrowaway/bot-farm-silence-tracker)`
(in a copy that has not been published yet, `bot-farm-silence-tracker/<version> (research
tool; unpublished copy)`, which names no project page).

**Attribution.** The data comes from Arctic Shift, a project by Arthur Heitmann
([source](https://github.com/ArthurHeitmann/arctic_shift), [API
docs](https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md)).
Credit it in anything you publish from these results. Its docs publish no formal terms
beyond asking users to be considerate of a free service and to use the monthly dumps for
massive jobs, and they name a removal-request form for people who want their data
removed, which this tool therefore cannot see. Please follow those requests.

```bash
uv run bot-farm-silence-tracker scan --platform reddit-archive --n 500 \
    --subreddit worldnews --subreddit europe --max-requests 2000 --out results.csv
```

The seed list is the same `reddit.subreddits` list the `reddit` platform uses.

**How it works.**

- *Discover.* Each round visits the seed subreddits in a fresh random order. Each visit
  reads one page (100 items) of posts or comments whose end time is drawn uniformly at
  random from the baseline window `[cutoff - baseline_days, cutoff)`, so the pool spreads
  over the whole window and every hour of the day instead of piling into the newest
  hours. No seed may supply more than `ceil(pool / number of seeds)` accounts while other
  seeds still turn up new authors; once the others run dry the cap is lifted. Posts or
  comments of a seed are done after 5 pages in a row with no new author
  (`--source-option dry_pages=N`), and a seed whose queries fail is dropped with a
  warning. Only items dated before the cutoff count, because an account seen only
  afterwards can never be a cliff. All randomness is seeded by `--seed`. Each page is a
  slice of the real activity, so the pool is a sample, not a census. Deleted authors,
  AutoModerator and names matching `(?i)[-_]?bot$` are skipped. Names that end in an
  ordinary word or surname such as `talbot`, `abbot`, `cabot` or `robot` are kept unless
  a `-` or `_` sits right before the "bot". It is a heuristic and still drops the rare
  person whose name just ends that way.
- *Discovery lookback (`--active-within-days D`).* By default discovery draws from the
  baseline window before the cutoff. With `--active-within-days D` it draws from the
  last D days before the scan's effective now instead (`now`, or the archive horizon
  when that is earlier, because nothing after it is archived), clipped to the baseline
  window: `[max(now - D, cutoff - baseline_days), min(now, cutoff))`. The end stops at
  the cutoff because an author seen only after the cutoff can never be a cliff. Pooling
  such authors would only add accounts that cannot be flagged, which pulls the share of
  cliffs down and biases the estimate low; the default baseline window drops rows dated
  at or after the cutoff for the same reason. The start stops at the baseline start
  because older activity is never scored. When the clip changes the window, a warning
  states the window actually used. The window must be at least 1 hour long. When it
  cannot be, whatever the archive horizon turns out to be, the scan is refused before
  any request, the horizon probe and coverage check included. Without
  `--allow-stale-source` the scan only runs when the horizon is at least the cutoff
  plus `--min-silence-days`, so any D up to `--min-silence-days` (3 by default) is
  refused that way: for example `--active-within-days 2` four days after the cutoff.
  A D that only an earlier horizon would rescue is not refused up front, since the
  horizon is known only after the probe; if the probed horizon still leaves an empty
  or shorter-than-1-hour window, the scan stops with the same error before any
  discovery request. With a cutoff in the recent past
  and D at least the baseline length, the clipped window is the whole baseline window,
  the same as the default. The mechanics are the same as the default: each page is the
  one-day window ending at a time drawn uniformly from the window, and a page that
  times out is retried for a six-hour window, so a long window is never one large
  query. A resumed run (`--from-run`) skips discovery, so the flag is refused there,
  but the value the run used is inherited and stored with the resumed run, and both
  `scan` and `report --run-id` print it. For example, 600 fresh random authors who
  posted or commented in the seed subreddits in the last 60 days before the cutoff:

  ```bash
  uv run bot-farm-silence-tracker scan --platform reddit-archive --n 600 --active-within-days 60 \
      --max-requests 3000 --out fresh.csv
  ```

  How random is that sample? Discovery collects a pool of `ceil(1.2 x 600) = 720`
  distinct authors, then the sampler draws 600 of them uniformly at random (reservoir
  sampling, every pooled author has the same chance). The pool itself is
  activity-weighted: an author with more posts and comments in the seed subreddits is
  more likely to land on a random page, so "random" means uniformly random among the
  discovered pool, not among all Reddit users or even all authors of those
  subreddits. Without `--seed` every run draws a fresh random pool and sample; pass
  `--seed N` to repeat one. Every sampled author was active in the seed subreddits
  before the cutoff, so every one of them could in principle be a cliff.
- *Fetch.* For each sampled account it queries posts and comments by author, newest
  first, and pages backward until the baseline start. Queries start at `limit=100`
  (`--source-option history_limit=auto` restores the old `auto`, which asks for up to
  1000 rows and times out far more often; an explicit `history_limit` must be 10 to
  100, because a page whose new ids all share its oldest second steps on with
  `before = oldest`, and a tiny page would silently skip the rest of a busy second).
  Older versions accepted a smaller `history_limit`. Resuming such a run with
  `--from-run` no longer refuses it: the resumed run uses `history_limit=10` and
  prints a warning. A value below 10 given on the command line is still refused.
  The archive's `after` and
  `before` are both exclusive, so the first request asks for
  `after = floor(baseline start) - 1`. Many items share one second, so each next page
  asks for `before = oldest + 1` and drops ids it has already seen; a page with no new
  id moves on to `before = oldest`. A window that is split after timeouts (see "Rate
  behavior") is split into halves that overlap by one second, so no second at the split
  point is skipped. The only items that can be lost are those beyond the first 100 by
  one author in one second, of one kind. Each kind is capped at exactly `max_items_per_kind` items
  (default 5000); hitting the cap sets the `truncated` flag, and the oldest days are the
  ones lost. Only the needed fields are requested (`id`, `created_utc`, `subreddit`
  and the text: `title` and `selftext` for posts, `body` for comments). The text is
  needed because its hash feeds the content-duplication signal of the analysis, and
  only that hash is kept (`[removed]` and `[deleted]` placeholders count as no text).

**Rate behavior.** The default pace is 0.5 requests per second with no bursts, and
every attempt, retries included, passes through that pacer (at most `max_rps`). The
waits follow the Arctic Shift API docs (`api/README.md`): its "Rate limiting" section
says a 429 means wait, and its "Query timeout" section says a timeout means the query
is too slow and should be retried or narrowed (a smaller `limit` or a narrower time
window). The API sends `X-RateLimit-Reset` (seconds until the window resets) on most
answers, including 422s, so the reset is waited for only where it means something:

- **429**: wait `Retry-After` if present, else `X-RateLimit-Reset + 1 + U(0, 5)`
  seconds (the random 0 to 5 s keeps clients throttled together from coming back in
  the same second), and halve the rate. Repeated 429s stop the scan as partial.
- **Timeout 422** (the `error` message contains "timeout" or "timed out", in any case,
  including the live service's ambiguous `"Timeout. Maybe slow down a bit"`): wait
  `Retry-After` if present (within the client's 900 s cap), else
  `min(slowdown_wait x 2^step, 120) x U(0.75, 1.25)` seconds, ignoring
  `X-RateLimit-Reset`. `slowdown_wait` defaults to 10 s, and `step` is 0 for the first
  retry and one more per later step of the same page. The rate is not lowered, but the
  success streak that drives recovery starts over. Then the ladder below runs.
- **Escalation guard**: when at least 3 timeout 422s arrived within 60 seconds, or at
  least 5 of the last 10 responses (within the last 120 seconds) were 422s (with
  `--concurrency K`: 3K timeouts, or 5K of the last 10K responses), the next
  timeout 422 is treated as load: the rate is halved once and the wait is
  `X-RateLimit-Reset + 1 + U(0, 5)` seconds. Both counts take at most one timeout per
  logical query (one probe, coverage sample, discovery page, or one kind of one
  account's history), so the retry ladder of one heavy author cannot escalate by
  itself. An escalated timeout skips the plain retry of the identical query and goes
  straight to the narrower one (`limit=100`, else a split window) when there is one.
  The counts then start over.
- **Load 422** (says "slow down" without a timeout): halve the rate (once per query)
  and wait `X-RateLimit-Reset + 1 + U(0, 5)` seconds.
- **502, 503, 504**: halve the rate, then wait `Retry-After` or the shared client's
  exponential backoff (1 s, doubling), not the reset.
- **Read timeout** (no answer within the client's 20 s read timeout, see
  Concurrency below): handled as a timeout 422 without headers. The shared client
  does not send the same query again on its own; the query goes to the ladder below
  (retry, then `limit=100`, then a split window), counts toward the escalation guard
  and `max_slow_queries` like a timeout 422 (in discovery and fetch), and its request row has `slow_kind`
  `read_timeout` (status empty). The totals count them as `read_timeouts`. This
  applies at every `--concurrency`, 1 included. A connect timeout is still an
  ordinary transport error, retried by the shared client.
- **`X-RateLimit-Remaining` at or below 0** on any answer: wait
  `X-RateLimit-Reset + 1 + U(0, 5)` seconds before the next request (a reset longer
  than the client's 900 s cap stops the scan as rate limited instead). The pacer then
  starts over, so the request after that one still waits its full interval.

A timeout 422 then runs this ladder:

1. Retry the same query once (`max_slowdown_retries`, default 1) after the wait above.
2. If that still times out and the query used `limit=auto` (only with
   `history_limit=auto`), wait again and retry with `limit=100`, and keep that limit
   for the rest of the account.
3. If that still times out, split the remaining time window in half and page each
   half on its own, newer half first, down to windows of about 6 hours. The halves
   overlap by one second (both bounds are exclusive), and duplicates are dropped by id.
4. If a 6-hour window still times out, the account is recorded with status `error`
   and the scan moves on. `--from-run` retries such accounts, and
   `--max-consecutive-errors` stops the scan if errors pile up during an outage.

All waits of one account's query share one budget of 900 s (the smaller of this
adapter's 900 s cap and the shared client's `max_total_wait`, which defaults to 1800 s
for ordinary retries); the wait that would pass it also ends in a per-account error. A
load 422 skips steps 2 and 3, because a smaller query would not help. When 3 different
queries in a row fail all the steps (`max_slow_queries`), the archive is overloaded
rather than one query being slow, and the scan stops with a rate-limit error and is
saved as partial. Continue it later with `--from-run RUN_ID`.

The scan starts at `max_rps`, which defaults to 0.5 requests per second. The rate is
halved from its current value only by load signals and never drops below 0.1 requests
per second. Recovery is additive: after every 10 successful requests in a row the rate
rises by 0.1 requests per second, back up to `max_rps`. Tune it with
`--source-option max_rps=R` (0.1 to 2; the archive's `api/README.md` "Rate limiting"
section says a couple of requests per second is fine, but the default stays at 0.5),
`recovery_step=S` and `recovery_streak=N`. A rate change keeps the pacer's earned
tokens, so it never grants an extra burst.

**Concurrency.** `--concurrency K` (1 to 3, default 1) fetches K accounts at once,
each in its own worker thread. It never multiplies the request rate: every worker
takes its requests from the one shared pacer, so the total stays at most `max_rps`
(0.5 by default) whatever K is. What it buys is overlap: while one account sits in a
422 backoff or a slow response, another account's requests use the pacer. To go
faster, raise the rate as well; the recommended setting is

```bash
bot-farm-silence-tracker scan --platform reddit-archive ... --concurrency 2 --source-option max_rps=1.0
```

which starts at 1.0 requests per second in total (not per worker), still halves on
every load signal, and stays inside the archive's own guidance: its `api/README.md`
"Rate limiting" section says a couple of requests per second is fine. `max_rps` still
tops out at 2. Behavior under concurrency:

- A load signal seen by any worker (a 429, a 502, 503 or 504, a load 422, an escalated
  timeout 422, or `X-RateLimit-Remaining` at or below 0) halves the shared rate and
  pauses **all** workers: the pacer gets a shared "not before" time that every worker
  waits for before its next request. A 429 the client gives up on (retries exhausted,
  or a wait over the 900 s cap) pauses them too, so none sends again before the stop
  reaches it. When a pause ends the pacer starts empty, so the first request after it
  still waits one full interval. Time a worker waits for a pause counts toward its
  query's 900 s wait budget, is added to the `wait_s` of that worker's last request
  row (or of the delayed request's own row when it is the account's first), and is
  totalled as `pause_wait_s`. A pause a worker set itself for an exhausted
  `X-RateLimit-Remaining` is counted once, as that worker's reset wait (`reset_wait_s`
  and the `wait_s` of the row that reported it); only the part another worker's later
  pause added counts as `pause_wait_s`. A plain timeout 422 still backs off only its own query.
- The escalation guard's thresholds scale with K (3K timeouts in 60 seconds, or 5K
  422s among the last 10K responses). K workers run K queries at once, so the same
  per-query timeout rate produces K times as many timeouts; unscaled thresholds would
  escalate about K times as often as a sequential scan.
- `max_slow_queries` counts each worker's accounts on its own: a worker stops the scan
  when its own last 3 accounts in a row failed every step of the slow-query ladder,
  the same rule a sequential scan applies to its one sequence of accounts.
- The rate, its recovery streak, the escalation guard and its counts, the request log
  and the totals are shared and guarded by one lock; each worker's logical query (its
  wait budget and whether it already counted toward the guard) is its own, so one
  query still counts once.
- `--max-requests` stays exact: a request is reserved against the budget before it is
  sent, so two workers cannot both take the last one.
- Only the source calls run in the workers. Every database write stays on the main
  thread, and accounts are recorded strictly in sample order, so the stored results,
  the `--max-consecutive-errors` count and the output match K = 1, with one
  exception: when the scan stops early, accounts after the stopping point that other
  workers had already finished are stored too. A sequential scan would not have
  fetched them yet; storing them only helps, because a `--from-run` resume then
  reuses them instead of fetching them again.
- When the scan stops (the budget, a rate-limit error, the error limit or Ctrl-C), the
  other workers are cancelled at their next wait or request, accounts that finished
  are recorded, and an account cut short stays pending, so `--from-run` fetches it.
  A worker whose account hits a fatal error (the budget, a rate-limit error, an auth
  error) or a bug cancels the others the moment it fails, even while an older account
  is still being fetched, and no further account is started; that error is what the
  scan reports. The request log is flushed in every case.
- A request already on the wire is never interrupted, so Ctrl-C first prints
  `stopping: waiting for N in-flight requests`. The archive client connects within
  10 s and waits at most 20 s for each read (instead of the usual 30 s), which bounds
  that wait. A read that times out is a slow query, not a transport error: see
  **Read timeout** above.

`--concurrency` is a performance setting and is not inherited by `--from-run`; it is
stored in the run's parameters when it is not 1. Only reddit-archive (and the test
`fake` platform) allow it. The official Reddit adapter keeps its own per-minute OAuth
budget and token state, and X bills every request against the spend ledger, which
writes through the scan's database connection on the main thread, so both fetch one
account at a time.

**Instrumentation.** Every HTTP attempt is recorded in the `request_log` table of the
cache database: run id, phase (probe, coverage, discovery, fetch), endpoint kind (posts
or comments), status, latency in ms, page `limit`, time window span in seconds, the
request rate when it was sent, for a 422 whether it was a timeout or a load refusal
(`slow_kind`, which is `read_timeout` for a request that got no answer in time),
and the waits: `wait_s` (the wait applied after that response, empty when none,
including a server-requested wait over the cap, which stops the scan instead),
`reset_s` (the `X-RateLimit-Reset` header) and `retry_after_s` (the `Retry-After`
header), and `send_seq`, the order the run's requests were sent in. Rows are stored
in the order they were drained (`seq`), which with `--concurrency` can differ slightly
from the send order, so sort by `send_seq` for the true order. Attempts that were never
sent (a refused connection) are not rows. No handles, subreddits, time bounds or
content are stored; the page `limit` is the only query value kept. Databases written
by an earlier version gain the wait columns and `send_seq` on open (`send_seq` is
empty in rows written before it existed). The scan ends with one line of totals (requests, 422s by kind and how many
timeouts the guard escalated, 429/5xx load responses, mean latency, observed requests
per second, time spent at the 0.1 floor, 422 backoff waits and waits for an exhausted
`X-RateLimit-Remaining`), which is
also stored as `summary.requests` on the run. Observed requests per second is based on
wall-clock time (`wall_elapsed_s`), so a laptop that sleeps during a scan shows the
rate a person waiting would see; `elapsed_s` is the monotonic time, which on macOS
stops while the host sleeps. For example:

```bash
sqlite3 ~/.cache/bot-farm-silence-tracker/events/<event-id>/bot-farm-silence-tracker.db \
  "SELECT phase, status, slow_kind, COUNT(*), ROUND(AVG(latency_ms)) FROM request_log
   WHERE run_id = 7 GROUP BY 1, 2, 3"
```

**Wall-clock estimates.** These are estimates, not guarantees: the archive is a free,
volunteer-run service whose query speed varies by the hour, and its query timeouts,
not the request rate, decide most of the wall clock. They assume the default pool
factor of 1.2, the 23 bundled seeds and the default 0.5 requests per second.

Fixed cost per scan: the horizon probe (up to 8 requests) and the coverage check (about
60 requests for a cutoff 3 days back, 135 for one 30 days back, see the ingestion gaps
note below). At 0.5 requests per second that alone is 2 to 5 minutes, and in the smoke
run below (cutoff 2 days back) timeouts stretched it to about 5.5 minutes.

Per sampled account: about 2 requests for a quiet author and 7 or more for an active
one (posts and comments, pages of 100). That is 4 to 15 seconds of requests. Each
timeout 422 adds its jittered backoff, about 7.5 to 12.5 seconds for the first retry
and doubling for later steps of the same page, or the server's reset (often 12 to 60
seconds) when the escalation guard treats it as load.

Discovery: one request per page, at least `ceil(1.2 x n / pages-worth of new authors)`
pages; expect about 1 to 2 requests per pooled author for small pools and more for
large ones (later pages turn up authors already pooled), plus the same timeout waits.

Measured once, estimates only (smoke run, 2026-10-12 UTC, `--n 10 --active-within-days 60
--allow-stale-source`, the default event's cutoff, nothing else using the API from this machine,
run under `caffeinate -i` so the host could not sleep): 122 requests in 517 seconds of
wall-clock time (517 s monotonic too, so no host sleep), 0.24 requests per second
observed. The probe and coverage check took about 5.4 minutes (76 requests, 13 timeout
422s), discovery about 1 minute (17 requests, 3 timeout 422s, a pool of 12), and
fetching about 130 seconds for 10 accounts (29 requests, 1 timeout 422), about 13
seconds per account. All 17 422s were timeouts; the guard escalated 2 of them to load
(the rate was halved twice and recovered to 0.45). The 12 plain timeout waits averaged
about 10 seconds (7.5 to 12.4); the one escalated wait that ran was 46.5 seconds (a
44-second reset). Ten accounts is a small and noisy sample.

An earlier smoke run under the old wait policy waited for `X-RateLimit-Reset` on every
timeout 422 (46 to 61 seconds each) and fetched at about 56 seconds per account. Its
wall-clock total also included host sleep (1,672 s wall against 1,209 s monotonic,
with one 526-second gap in its request log), which is why `wall_elapsed_s` and `elapsed_s` are now both
recorded.

| `--n` | Pool (x1.2) | Requests (about) | Wall clock, few timeouts | Wall clock, smoke-run timeout rate |
|---|---|---|---|---|
| 10 | 12 | 100 to 160 | 5 to 8 min | about 9 min |
| 300 | 360 | 1,200 to 2,500 | about 50 min | about 1.7 h |
| 600 | 720 | 2,300 to 5,000 | about 1.6 h | about 3.3 h |
| 3000 | 3600 | 11,000 to 25,000 | about 8 h | about 16 h |

All of these are estimates. The left column assumes pacing only (about 3 requests per
account and 1.4 per pooled author at 0.5 requests per second). The right one assumes
the smoke run's rates (about 13 seconds per fetched account and 5 seconds per pooled
author). A busier archive, a cutoff further back or larger pools can be several times
slower. The per-request `request_log` table (see "Instrumentation") shows which case a
scan is in, and the summary line at the end of the scan reports the time spent in 422
backoff waits.

Set `--max-requests`, and rely on `--from-run` to finish a large scan in pieces. Every
`--from-run` resume repeats the horizon probe and the coverage check, so each piece's
`--max-requests` must cover them again. The coverage check refuses to start unless the
remaining budget covers every sample with one retry each, 6 confirmations, 50
requests kept for discovery or fetching, and the HTTP layer's 4 retries. Counting the
8 probes, that is about 194 requests for a cutoff 3 days back and about 344 for one
30 days back. A piece with a smaller
budget stops as a stale source, before it fetches anything.

- *Archive lag.* Data arrives hours to days late. At the start of a scan the adapter
  reads the newest archived comment and the newest archived post of `worldnews` and of
  up to 3 seed subreddits (on a `--from-run` resume, the seeds stored with that run;
  override the seeds with `--source-option freshness_subreddit=NAME[,NAME...]`).
  `worldnews` is always probed. For each kind, a probe whose newest item is more than
  24 hours older than the newest probe is a quiet, banned or dead subreddit, so it is
  dropped with a warning; the oldest of the rest is that kind's horizon. The archive
  horizon is the older of the comment and post horizons, a conservative estimate of how
  far the archive reaches. The adapter reports it through `Source.data_horizon()`,
  which the core calls once before discovery (before fetching on a `--from-run`
  resume). The lag is enforced: the scan prints it at once, scores with
  `effective_now = min(now, horizon)` (so the lag is not counted as silence), and stops
  before sampling or spending anything if the horizon is earlier than the cutoff plus
  `--min-silence-days`. Pass `--allow-stale-source` to run anyway; every result is then
  provisional. If a kind's probes all fail, even after one retry, the horizon is
  unknown: the scan stops with the same stale-source error, and with
  `--allow-stale-source` it runs with every result provisional.
- *Ingestion gaps.* The archive can miss hours or days in the middle of its range, and
  such a gap looks like every account going silent at once. After the horizon probe,
  the adapter checks the days from two days before the cutoff up to the horizon. It
  reads `worldnews` comments at 4 evenly spaced sample times per day
  (`--source-option coverage_samples_per_day=4`, at most 24), counted from the
  cutoff's time of day: for a cutoff at 07:30 UTC the sample times are 07:30, 13:30,
  19:30 and 01:30. It reads `worldnews` posts once per day, at the first of those
  times. Each sample is one request of `limit=100`. It compares the rate with the
  median rate of the same kind at the same time of day over up to 7 reference days
  spread over the baseline (at least 3, reaching back before a shorter baseline). A
  sample below 25% of that median (`--source-option coverage_threshold=0.25`) is
  confirmed by one more sample an hour later in the same direction, which is counted in
  the request budget. Only when both are below 25% is it a gap: the scan warns, naming
  the kind and the day, and the horizon is pulled back to the start of that day. A
  single low sample followed by a normal one is treated as ordinary variation. If the
  horizon is then earlier than the cutoff plus `--min-silence-days`, the scan stops as
  stale, or with `--allow-stale-source` runs with every result provisional. The first
  14 days are checked every day, later days only every third day, so an old cutoff
  does not cost an unbounded number of requests, and a gap that lies wholly inside an
  unchecked day is not seen. A coverage sample is retried once, but one whose reads
  time out is retried up to `COVERAGE_READ_RETRIES` (3) times with jittered backoff.
  Coverage and probe failures do not count toward `max_slow_queries`. The check
  fails closed: if a sample stays unreadable after its retries, the reference days hold no `worldnews`
  comments or posts, or the remaining `--max-requests` budget does not cover the check
  plus 50 requests for discovery, the horizon is unknown and handled as above. With
  `--allow-stale-source` the scan then still scores as of the newest archived item
  found by the horizon probe, not as of now, and every result is provisional.
  `coverage_threshold=0` turns the check off.

  The check is coarse, and the two kinds are caught differently. A page of 100
  comments covers only about 4 to 22 minutes. A sample reads low only when the gap
  runs about 12 to 66 minutes (three page spans) past the sample time, and detection
  also needs the confirmation an hour later to read low. The chance of catching a
  total comment gap is about (gap - 2 hours) / 6 hours; a gap under about 1 to 2 hours
  is never caught (3 h about 17%, 6 h about 67%, 8 h or more always). A page of 100
  posts covers 13 to 17 hours, so each post sample sees most of a day, and only a
  nearly total gap of about 10 hours or more is caught.
  Partial gaps are invisible: losing up to about 75% of items, for example half of the
  comments missing for a whole day, is not detected, and only near-total gaps of
  several hours are caught.
- *No suspension status.* The archive cannot tell a suspended account from a silent
  one, so this source never reports `suspended`, `deleted` or `private`. Accounts just
  look silent, which makes the suspension confound in the main limitations list worse.
- *Coverage gaps.* The archive is community-run. Some periods, subreddits and users are
  missing or were removed on request, and a gap looks like silence. A short run of
  low counts for one subreddit across many accounts can be a gap, not behavior.
- *No account creation date.* The search API does not expose it, so `created_at` stays
  empty. `--new-accounts-days` therefore drops every account with this source (it
  warns), and the account-age metric is blank.
- *Slow author queries.* Very active authors can make the archive time out. The steps
  under "Rate behavior" usually get through; if not, that one account is recorded as an
  error and can be retried with `--from-run`.

### Bluesky

`--platform bluesky` reads public Bluesky profiles and posts through the documented
atproto XRPC API. Reads go to `https://public.api.bsky.app`, the cached host that
Bluesky asks developers to use for unauthenticated public reads. The adapter only
reads: it never posts, follows, likes or writes any record. It identifies itself with
`User-Agent: bot-farm-silence-tracker/<version> (research; +https://github.com/obviousthrowaway/bot-farm-silence-tracker)`
(in a copy that has not been published yet, `bot-farm-silence-tracker/<version> (research
tool; unpublished copy)`, which names no project page).

```bash
# With a login: search the seed hashtags and keywords.
export BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE=yourname.bsky.social
export BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
uv run bot-farm-silence-tracker scan --platform bluesky --n 500 \
    --max-requests 5000 --out results.csv

# Without a login: sample the accounts that replied to the bundled lists' members.
uv run bot-farm-silence-tracker scan --platform bluesky --n 200 \
    --max-requests 2500 --hide-handles --out results.csv

# Or name your own lists, starter packs and feeds.
uv run bot-farm-silence-tracker scan --platform bluesky --n 500 \
    --query "https://bsky.app/profile/<handle>/lists/<id>" \
    --source-option seed_lists=at://did:plc:.../app.bsky.graph.list/... \
    --out results.csv
```

`--hide-handles` (on `scan` and `report`) hides account names on the console, for
screen shares and logs: the top-candidates table shows row numbers (`#1`, `#2`, ...),
and every warning, error and log line replaces the handles and IDs of the run's
accounts with their row number or `[hidden]`. DIDs, the owner of an AT-URI and the
handle in a bsky.app profile or starter-pack URL (a seed list's curator, for example)
become `[hidden]` too. The output file still names the accounts.

**Login (optional, needed for search).** Post search (`app.bsky.feed.searchPosts`)
currently needs a login: the public host answers it with HTTP 403, which a Bluesky
docs contributor described as deliberate load shedding. Bot Farm Silence Tracker does not route
around that block. Logged-out requests always go to `https://public.api.bsky.app`, and
no option sends them to another AppView. Without a login, query seeds are skipped
before discovery starts, with a warning, and no search request is sent; a scan whose
seeds are all queries stops with an error that says how to log in or which feed and
list seeds to use instead.
To search, create an app password in the Bluesky app (Settings, Privacy and
security, App passwords) and set `BOT_FARM_SILENCE_TRACKER_BSKY_HANDLE` and
`BOT_FARM_SILENCE_TRACKER_BSKY_APP_PASSWORD`. Never use your main account password. Set both
variables or neither. The adapter opens one session with
`com.atproto.server.createSession` on `https://bsky.social` (change it with
`--source-option pds_url=https://your.pds.example` if your account lives on another PDS;
a failed login says so),
sends searches through it, and refreshes the session when it expires. Profile and feed
reads stay unauthenticated. The handle, password and tokens are never logged, printed
or stored in the database or the output, and `--source-option` refuses keys such as
`handle` or `app_password`, so secrets cannot end up in shell history or a stored run.
Use a dedicated account if you can: the searches are tied to it.

**Seeds.** The `bluesky.queries` list in `config/seeds.yaml` holds hashtags and keyword
phrases on the same topics as the other platforms, and `bluesky.lists` (and an optional
`bluesky.feeds`) holds public lists that are appended to them; `--query` replaces all of
them. An entry may
also be a public feed or list, given as an AT-URI (`at://did:plc:.../app.bsky.feed.generator/...`
or `at://did:plc:.../app.bsky.graph.list/...`) or a bsky.app URL
(`https://bsky.app/profile/<handle>/feed/<name>`, `https://bsky.app/profile/<handle>/lists/<id>`).
`--source-option seed_feeds=URI,URI` and `seed_lists=URI,URI` add feeds and lists to the
seed list. A feed or list named by its owner's handle is resolved to the owner's DID
first, so the same feed given twice (by handle and by DID) is read once. Feeds and lists
work without a login. A starter pack, given as
`https://bsky.app/starter-pack/<handle>/<id>` or
`at://did:plc:.../app.bsky.graph.starterpack/...`, is read as its list (one
`app.bsky.graph.getStarterPack` request finds the list). Without a login, search seeds are
skipped with a warning, and a scan whose seeds are all searches stops with an error that
says how to log in.

**Sampling frames.** Each kind of seed samples a different population, and the scan
says which before discovery ("Sampling frame: ...") and again in a warning after it:

| Seeds | Frame |
|---|---|
| Search queries (login needed) | *Window posters*: accounts with a post in the baseline window that matches a query. |
| Feeds, and lists with `list_mode=feed` | *Feed posters*: accounts with a post in the baseline window that the feed showed. |
| Lists, `list_mode=repliers` (the default) | *Repliers*: accounts that replied in the baseline window to posts by the lists' members. |
| Lists, `list_mode=members` (opt-in) | *List members*: the members themselves, whether or not they were active. |

With a login and the bundled seeds, discovery uses search only, and the bundled lists
are not read; `--source-option list_mode=repliers` (or `members` or `feed`) asks for
them as well. Seeds you choose yourself (`--query`, `--seeds`, `seed_feeds`,
`seed_lists`) are always used. Frames are never mixed silently: when the seeds draw from
more than one frame, a warning names each one and says how they are mixed. Discovery
visits the seeds in turn and lets each take at most an equal share of the pool while
others still have new accounts, and all list seeds together count as one seed, so the
mix follows the number of seeds, not the size of each frame.

**List seeds: repliers by default.** Feed generators keep only recent posts: in a
live check on 2026-10-12, most political feeds held about a day of history, so a feed
read more than a day after the cutoff finds almost no one who posted before it. The
scan warns up front when feed seeds are read more than a day after the cutoff. List
seeds avoid this. Each list's members are read with `app.bsky.graph.getList` (at most
`list_member_pages=50` pages of 100 per list; a warning says when a list is cut short,
and a list that fails partway is counted, with a warning that its unread members
cannot be drawn), and duplicates across lists are dropped: an account on several lists
counts once in the union, so the union is not weighted by how many lists name it.
Only curated lists (`app.bsky.graph.defs#curatelist`) and starter-pack lists are used:
a moderation (mute or block) list is refused with a warning, because its members were
picked for being reported or blocked and would bias the sample toward suspensions.

- *`list_mode=repliers` (default).* The members are **anchor accounts**, not the
  sample. `anchor_accounts=40` anchors are drawn at random from the union with
  `--seed`. For each, `getAuthorFeed` (`filter=posts_no_replies`, at most
  `anchor_feed_pages=10` pages) gives its own posts whose `sortAt` and `indexedAt` are
  both in the baseline window; posts with `replyCount` 0 are skipped, and
  `anchor_posts_per_account=3` of the rest are drawn at random. `getPostThread`
  (`depth=1`, `parentHeight=0`) gives each one's direct replies. The authors of replies
  indexed in the window, at most `thread_reply_limit=10` drawn at random per thread,
  form the frame: they are de-duplicated by DID, anchors (every list member) are left
  out, and so are accounts labelled `!no-unauthenticated`, whose label the thread view
  carries. Every drawn anchor is read before any replier is pooled, and the frame is
  shuffled with `--seed`, so when the pool limit (`--pool-factor` times `--n`) is
  smaller than the frame, the pool is a uniform random subset of all the repliers
  found, not the repliers of the first few anchors. The pool is then sampled uniformly
  at random with `--seed`, and each account's `community` is the name of the anchor's
  list. This frame, "accounts that replied in the window in political conversations",
  reaches ordinary users and amplification accounts. **Its bias:** an account's chance
  of entering the frame is roughly the sum, over the threads it replied to, of
  (posts drawn from that anchor / that anchor's window posts) ×
  min(1, `thread_reply_limit` / that thread's repliers). Busy threads contribute more
  accounts, but each of their repliers is less likely to be drawn, and repliers of
  prolific anchors (many window posts, of which only three are read) are
  under-represented. The frame also leans toward accounts that reply to prominent
  political accounts; an account that replied to several sampled posts is more likely
  to be found but counts once. **Survivorship:** threads are read after the cutoff, so
  repliers who were taken down, or who deleted their account or their reply, before
  the scan are never discovered. The suspended and deleted counts are therefore near 0
  by design, while cliffs (accounts that are still present but went silent) are
  measurable. Anchors that post a lot may not reach the window start within
  `anchor_feed_pages`, so their posts lean toward the cutoff; their repliers are then
  drawn from closer to the cutoff, which nudges the cliff share up (the summary
  warning counts these anchors). Discovery stops reading anchors after it has used
  half of the `--max-requests` budget left when it began, keeping the rest for
  scoring, and the warning then says how many anchors were read. Repliers tend to post
  a lot, so scoring costs more than discovery: in a live check, 30 days of history
  took about 9 author-feed pages per account, so allow about 10 requests per sampled
  account. A warning gives the tallies: anchors read, window posts, threads read, the
  direct replies found in each, how many threads contributed repliers and the largest
  thread's share of the frame.
- *`list_mode=members` (opt-in, the prominent-accounts frame).* The union is shuffled
  with `--seed` and the pool is drawn uniformly at random from the members, whenever
  they last posted, not from the accounts that posted in the window; members who were
  not regular posters before the cutoff are counted as not eligible. This frame cannot
  estimate how many bot accounts went dark; bots are rarely on curated lists of
  officials and outlets.
- *`list_mode=feed`.* A list's posts are read with `getListFeed`, like a feed. That
  reaches back further than a feed generator, but an active list holds only a day or
  two of posts in every seven pages, so reaching a 30-day window takes hundreds of
  requests.

The bundled `bluesky.lists` are seven public curated lists of news organisations,
journalists and elected officials in the UK, Canada and Europe, the Middle East and
Africa, plus US outlets and journalists (1,048 entries and about 900 distinct members on
2026-10-12; reading them all takes about 15 requests). No list of US elected officials
is bundled. The UK MPs list and the two news-organisation lists make up about 75% of
the entries (789 of 1,048), so the anchors are weighted toward UK politics and news
outlets. The set is not balanced across the political spectrum: a public curated list
of right-of-centre officials, parties or outlets appeared absent from Bluesky in a
2026-10-12 search, so the anchors lean toward mainstream and centre-left accounts. The
comments in `config/seeds.yaml` give each list's name, size and what was checked about
it. This frame cannot estimate how many bot accounts went dark; bots are rarely on
curated lists of officials and outlets. The default repliers frame reaches past the
lists to the accounts that reply to them.

**How it works.**

- *Discover.* Each round visits the seeds in a fresh random order. A search seed reads
  one page (`sort=latest`, 100 posts) of a one-day slice whose end is drawn from
  `[cutoff - baseline_days, cutoff + 1 day)` and clamped to the cutoff, so every moment
  of the baseline window is equally likely to fall in a slice, the final day included.
  The draw is uniform in time; heavier posters are more likely to be drawn. A feed or
  list seed (`list_mode=feed`) is read newest first, page by page. Pages whose items
  are still after the cutoff are paged through without counting as dry, up to 30 of
  them (`max_pages_ahead=N`; `0` ends the seed at the first such page); then at most
  20 more pages (`feed_pages=N`) are read, back to the window start. A page is placed
  in time by its bulk, not its oldest item: it is "after the cutoff" when its bulk is,
  and it has reached the window start when its bulk is older than the start. The bulk
  is the page minus a stray allowance of 20% of its items, rounded up, but never half
  the page or more: a page of 3 to 5 items tolerates one stray item, a page of 10 two,
  and a page of 1 or 2 items none. Feeds are not
  strictly ordered by time (pinned or boosted posts, odd `sortAt` values), so one stray
  old post does not end the seed. A seed that contributes nothing or does not reach the
  window start says why: the feed's history ends before (or inside) the window, a page
  limit was reached, it had no items in the window, or it stopped turning up new
  authors. Repliers (the default for list seeds) and list members are handed out as
  described above. The per-seed cap, the 5-page dry rule (`dry_pages=N`; not for the
  repliers seed, which reads one anchor per page and ends when its anchors run out)
  and the `--seed` randomness work as for the Reddit archive. A seed
  page that fails is retried twice, after 5 and then 10 seconds, before the seed is
  dropped with a warning; a page that works resets the count, so only failures in a
  row drop a seed. Only posts indexed before the cutoff count. Accounts are
  identified by their DID, which survives handle changes.
- *Activity.* `app.bsky.feed.getAuthorFeed` with `filter=posts_with_replies`, paged newest
  first back to the start of the baseline, up to 50 pages of 100 (`max_pages=N`; when the
  cap stops it, the account is marked truncated). An account's own posts are `post`,
  replies `reply` and quote posts `quote`. Reposts are left out by default: a repost
  carries someone else's words, so its text hash would make unrelated accounts that
  reposted the same popular post look like duplicates, and the method looks for accounts
  that stop *posting*. `--source-option include_reposts=true` counts them as `repost`
  (timed by when the repost was indexed, with no content hash), which matches what the X
  adapter does with retweets. Reposts that are left out are still tallied: the
  `reposts_after_cutoff` column shows how many an account made after the cutoff, and
  the output adds a `REPOSTS` note when cliff candidates reposted, because such an
  account stopped posting but did not go silent. Pins are not requested
  (`includePins` defaults to false).
- *Timestamps.* Activity is timed by `indexedAt`, when Bluesky's AppView first saw the
  record, not by `createdAt`. `createdAt` is written by the posting client and can be
  set to any date, so a bot could backdate or future-date its posts to fake activity or
  silence. One consequence: a post whose `createdAt` was backdated to before the window
  is listed at that earlier position in the author feed, past the point where paging
  stops, so it is missed.
- *Account status.* The profile endpoint reports `AccountTakedown` (Bluesky's takedowns
  and suspensions), recorded as `suspended`; `AccountDeactivated`, recorded as `deleted`
  with `deactivated = true` in the output and counted separately in the summary (a
  deactivated account can come back); and "Profile not found", recorded as `deleted`. The author feed answers "Profile not found" for all
  three, so the adapter asks the profile endpoint to tell them apart.
- *Creation date.* `created_at` is the profile's `createdAt`, so `--new-accounts-days`,
  the account-age metric and creation bursts work. Profiles without it stay unknown.
- *Data horizon.* None: the AppView is live, so results are scored as of now and no
  request is spent on a freshness probe. (An earlier version read a feed to measure
  freshness, and a failed read stopped the whole scan as stale.)
- *Two-phase sampling.* `--baseline-sample-rate` works: the pre-check reads the newest
  page (25 items) of the author feed, which normally answers "any activity since the
  cliff window?" in one request.
- *Concurrency.* `--concurrency` accepts 1 to 3. The request rate stays capped by the
  shared pacer.
- *Other options.* `lang=xx` restricts search to one language. `page_size=N` (1 to 100)
  sets the page size. There is no option to point reads at another AppView.

**Rate behavior.** Bluesky publishes no limit for its AppView ("generous") and limits
each IP to 3000 requests per 5 minutes on its PDS, and `createSession` to 30 per 5
minutes per account. The default is 1 request per second, one session per scan, and
`--source-option max_rps=R` changes it (0.1 to 5). Every 429, 502, 503 or 504 halves the
rate, down to 0.1 per second, and 20 successful requests in a row raise it again by 0.1.
A 429 waits for `Retry-After`, else for the `RateLimit-Reset` time (epoch seconds) plus
1 second, else backs off exponentially. A response with `RateLimit-Remaining: 0` pauses
every worker until its reset. A wait longer than the shared client's cap, or 429s that
outlast its retries, stop the scan as rate limited; resume it later with `--from-run`.
`--max-requests` is a hard cap. Every request is logged to the run's `request_log`
table with its method name, status and timing, and never with a handle, DID, query or
token.

**Limits and ethics.**

- Read-only, documented endpoints only. The Bluesky Developer Guidelines forbid spam
  and automated interactions; this tool makes none. Bluesky's Terms of Service and
  Privacy Policy say most activity is public by design and do not address read-only
  research access, so stay polite: keep the default rate and a `--max-requests` cap.
- Raw text is never stored, only a hash, and accounts are keyed by DID. Treat the
  output as personal data: a list of accounts flagged by a heuristic.
- Bluesky lets accounts ask not to be shown to logged-out viewers (the
  `!no-unauthenticated` label). The adapter honors it: such authors are never added to
  the pool, and an account that carries the label when its profile is read, or on its
  own posts when a cached account is fetched again, is recorded as `opted_out` without
  reading further. Opted-out accounts never appear in any output, not even with
  `--include-unavailable`: the summary only counts them. The database keeps the DID,
  the `opted_out` flag and timestamps (when the account was read, and each run sample's
  window end), but no handle or content: the handle, creation date, metadata, cached
  posts and activity window are removed from the account cache, every stored run
  sample, earlier runs included, keeps only the DID and the opt-out flag, and deleted
  rows are overwritten in the database file (SQLite `secure_delete`). While the cache
  entry is fresh, a run skips the account without a lookup. Discovery with
  `--new-accounts-days` reads the profile of an account cached as `opted_out` again,
  and a fetch reads it again once its cache entry expires (`--cache-ttl-hours`), so an
  account that removes the label is sampled again and its posts are fetched in full.
  Do not publish identifiable results about individuals.
- Search covers what Bluesky's search index returns: its cursor "may not allow
  scrolling through the entire result set", and ranking and indexing can change.
- A deactivated account that later comes back looks like a deleted one that returned.
- Accounts on third-party PDS hosts may answer differently from Bluesky's own.

## Shared registry

The `registry/` directory of this repository is a shared, crowd-sourced record of
scanned accounts, kept separately for each [outage event](#outage-events). Its purpose is to build, through review, a list of verified
inauthentic accounts that people then report to platform moderators (see
[Reporting](#reporting)). It also lets researchers avoid scanning the same accounts
again and again.

**The registry is not anonymous.** Usernames and IDs are stored base64-encoded
(URL-safe, without padding). That hides names from casual search, but it is
reversible by anyone with one line of code. It is meant for a trusted research group
working in a private repository, not for publication. Treat every registry file as
containing real account names.

### Format

Each outage event of `events.toml` has its own folder, `registry/events/<event-id>/`.
Records live in `registry/events/<event-id>/<platform>/<shard>.jsonl`, one JSON object
per line and one line per account and event. The shard is the first two hex digits of `sha256(id_b64)`, so
records spread evenly over at most 256 files per platform (every Bluesky DID encodes
to the same base64 prefix, so a prefix shard would put them all in one file), and
lower-case hex never gives two file names that differ only in letter case. Lines are
sorted by `id_b64` and written with sorted keys and no spaces, so a pull request diff
shows only the records that changed. Nothing else may live under `registry/` except
`schema.json` and `events/<event-id>/evidence/<platform>/<id_b64>.md` files, and only
in the folders of events that `events.toml` lists.
`registry/schema.json` is the JSON schema. Each record has:

| Field | Meaning |
| --- | --- |
| `event_id` | The outage event: an id of `events.toml`, equal to the name of the record's `events/<event-id>/` folder. |
| `platform` | The scan platform (`reddit-archive`, `reddit`, `bluesky`, `x`). |
| `id_b64` | The stable platform ID, base64-encoded: the Reddit author name (`reddit-archive`; it must equal the username) or the lower-cased name (`reddit`), the Bluesky DID, or the numeric X user ID. |
| `username_b64` | The handle at the last scan, base64-encoded. |
| `first_scanned_at`, `last_scanned_at` | The reference time of the first and newest scan that saw the account. |
| `last_activity_at` | The newest own activity the scan saw. |
| `scan_window_end` | Where the scanned data ended: the archive's horizon for `reddit-archive`, the scan time for live sources. |
| `baseline_posts`, `baseline_active_days` | Activity in the event's baseline window before its cutoff. |
| `cliff` | Whether the scan scored the account as a cliff that is not provisional and silent for at least the event's `min_silence_days` up to `scan_window_end`. |
| `tool_version` | The version that wrote the facts. |
| `status` | `scanned`, `candidate`, `verified_inauthentic` or `cleared`. |
| `evidence` | `evidence/<platform>/<id_b64>.md`, relative to the event's folder. Required for `verified_inauthentic`. |
| `status_updated_at` | When the status last changed. |

Records hold no scores. An evidence file may list the signals.

`registry validate` also enforces these rules:

- No timestamp may lie more than 1 hour in the future.
- `scan_window_end` may lie at most 3 days before `last_scanned_at`.
- A cliff or `candidate` record needs at least its event's `min_silence_days` between
  `last_activity_at` and `scan_window_end`, so a record cannot shrink its window to
  hide activity.
  (`cleared` and `verified_inauthentic` records are exempt: a rescan keeps a human
  status even when the account posted again.)
- The ID must match the username (`reddit-archive`, `reddit`), be a DID (`bluesky`)
  or be numeric (`x`). The `fake` platform is never accepted.
- `event_id` must be an event of `events.toml` (never a custom event) and the name of
  the record's folder.

### Statuses

- `scanned`: scanned, nothing to review.
- `candidate`: the record's `cliff` is true, at least one signal **independent of the
  outage's timing** fired, and the live profile check did not find the account
  removed:
  - the inter-arrival coefficient of variation is at most 0.5;
  - the hour-of-day entropy, with the Miller-Madow bias correction, is at least 4.3
    bits over at least 50 baseline posts.

  A synchronized stop (a cluster of at least 3 accounts whose adjusted permutation
  p-value is at most 0.01; see [Method](#method)) is listed with
  the signals but never makes a candidate on its own: a shared stop at the outage is
  what anyone the outage affected shows. A low `p_silence` is not a signal either:
  silence grows with the time since the cutoff, so every cliff account's `p_silence`
  eventually falls below any fixed threshold. It measures the cliff, which is already
  required.

  **The live profile check.** An archive cannot tell silence from removal, so before
  an account becomes a candidate its public profile is checked on the live platform,
  only through the adapter's own allowlisted hosts, logged out, with its own user
  agent (no other identity, user agent or proxy). Bluesky: one
  `app.bsky.actor.getProfile` on `public.api.bsky.app`; a suspended or deleted
  account, or one whose check failed, stays `scanned`. Reddit has no logged-out API
  without credentials, so its status is recorded as "unknown (archive only)", and X's
  as "unknown (needs credentials)": the human verifier resolves them on the live site
  (see the evidence file's reviewer findings). `--no-live-check` makes no request
  (Bluesky accounts then stay `scanned`). A candidate needs human review.
- `verified_inauthentic`: reviewers agreed the account is inauthentic.
- `cleared`: reviewers decided it is not.

### Workflow

1. Scan as usual, then merge the run into its event's folder of the registry.
   The accounts come from the cache; nothing is fetched except the live profile check
   of the accounts that would become candidates (see [Statuses](#statuses)).

   ```bash
   bot-farm-silence-tracker registry add --run-id 7 --event <event-id> --registry registry
   ```

   New accounts become `scanned` or `candidate` by the rule above; only the live
   profile check makes requests. An account already
   in the registry is updated when the new scan is newer: the facts are replaced,
   `last_activity_at` keeps the later of the two values, and the status can move
   between `scanned` and `candidate`. `verified_inauthentic` and `cleared` are never
   changed by `registry add` or `registry merge`. Accounts that opted out of logged-out
   views are left out entirely, as everywhere else in the tool. Suspended, deleted and
   private accounts are left out as well, because there is no public activity to
   confirm, and so are accounts that were skipped, failed, only pre-checked, or showed
   no activity in the window.
2. Skip work others already did. A new scan can leave out the accounts the registry
   records as scanned recently:

   ```bash
   bot-farm-silence-tracker scan --platform reddit-archive --skip-registered-within-days 14 --registry registry
   ```

   Only records of the scan's own event count: an account checked for one event is
   still unchecked for another.

3. Draft evidence for a candidate. The draft is built from the cache and keeps the
   identity encoded, so it can be committed:

   ```bash
   bot-farm-silence-tracker registry evidence --run-id 7 --account <handle-or-id> \
     --event <event-id>
   ```

   Without `--out` it is written to
   `registry/events/<event-id>/evidence/<platform>/<id_b64>.md`.

   It lists the scan facts, the steady test's numbers, the signals that fired with
   their values (each marked whether it is independent of the outage's timing), the
   stop-time cluster (its size and its permutation p-value, adjusted for the run's
   many clusters), the run's placebo comparison, the live profile check made when the
   draft was written, the hour-of-day profile, the communities, and public links. The cache keeps no post IDs or text, so the
   links are the profile and public archive queries scoped to the listed activity
   times, written with `{username}` and `{id}` placeholders. Reviewers check every item
   against the live platform and fill in the draft's **reviewer findings**: "Live
   profile checked: active / suspended / deleted / not checked", and the evidence
   fields that are not about the silence (content, network, profile, other). The last
   checklist box, "The behavior is inauthentic", needs the live profile resolved and
   at least one of those fields filled in; the gate refuses a verdict without them.
4. Open a pull request that sets the status and the `evidence` path. `registry merge`
   rewrites the registry in canonical form (it also merges in other registries with
   `--from DIR`), and `registry validate` checks it.

Statuses change only by pull request, and `main` is protected by a ruleset (see
[Contributing](#contributing)). Plain account records need no reviewer: the
`Registry gate` validates and spot-checks them. **Every record that moves into
`verified_inauthentic` or `cleared`, every change to a record that already has one of
those statuses, every evidence file change and every record on `reddit` or `x` needs
the repository owner's approval** through the `registry-override` label and an
approving review of the head commit (see below). The gate's summary lists every
clearing (`verified_inauthentic` to `cleared`) first, as a high-impact transition,
and the owner confirms the reason before approving.

### Checks on registry pull requests

Three workflows run on pull requests:

- `CI` (`.github/workflows/ci.yml`, on `pull_request`) tests the code.
- `Registry gate` (`.github/workflows/registry-gate.yml`, on `pull_request_target`)
  checks registry data. Its `gate` job is a required status check in the ruleset of
  `main`, next to the `CI` jobs. It always runs (it has no path filter) and passes a
  code-only pull request at once: its "Classify the change" step finds no path under
  `registry/` and no `events.toml`, writes "No registry data changed" to the summary,
  and skips every later step.
- `Registry override` (`.github/workflows/registry-override.yml`, on
  `pull_request_target`) ties the `registry-override` label to one head commit (see
  below). It has no concurrency group, so later events never cancel it.

The gate runs the base branch's code, never the pull request's. It fails a pull
request whose base is not the default branch (it reruns when the base is edited),
because another branch could carry an outdated or changed gate. It checks out the
base commit only, fetches the pull request's head as git objects, and reads the
registry files from them without checking anything out. It uses a read-only token, no
secrets, and no cache. It then:

1. Fails a pull request that changes `registry/` or `events.toml` together with any
   other file. Data and code are reviewed separately. The one exception is
   `registry/schema.json` with a code change; the test suite checks the schema byte for
   byte.
2. Validates the head's `events.toml` (keys, id syntax, unique ids, cutoff format,
   range and order, window ranges, at most 64 KB and 200 events) and compares it with
   the merge base: a new event passes without a label and is listed in the summary;
   changing an existing event's name, cutoff or windows, renaming it or deleting it
   needs the `registry-override` label with both proofs below, and is listed as a
   high-impact change (see [Outage events](#outage-events)). Then validates the head
   registry with the base code's rules and the head's events. Every file under
   `registry/` must be a regular, non-executable file (git mode `100644`) with an
   allowed name in the folder of an existing event, and every record's `event_id`
   must be its folder's.
3. Compares the head records with the merge base and fails, unless the repository
   owner approves it with the `registry-override` label and both proofs below, when
   the pull request:
   - moves any record into `verified_inauthentic` or `cleared` (a new record with
     either status, and a clearing from `verified_inauthentic` to `cleared`,
     included);
   - changes or removes any record that is `verified_inauthentic` or `cleared`
     (moving it back to `scanned` or `candidate`, or changing its username, among
     others; its ID and platform are its key, so changing them removes the record);
   - removes records;
   - adds or changes more than 15 records;
   - adds or changes any record on `reddit` or `x`, which the check cannot query
     without credentials;
   - adds, changes or removes any evidence file.

   A record is keyed by its event, platform and ID, so the same account in two events
   is two records. A record that becomes `verified_inauthentic` also needs its event's
   `verified_min_silence_days` of silence (21 by default, longer than the
   `min_silence_days` a candidate needs), an account that has not posted again (the
   record is still a cliff, and its last activity is before the cutoff), and an
   evidence file whose reviewer findings support the verdict (see
   [Workflow](#workflow)); no label lifts any of these. Every added or changed
   `verified_inauthentic` record whose account posted again is listed as "resumed":
   report drafts refuse it, and reviewers should consider clearing it. The job summary has a table of every status transition, and lists
   every clearing (`verified_inauthentic` to `cleared`), every evidence file change,
   every username change of such a record, and every added or changed `reddit` or
   `x` record as "unverified (platform not checkable)". Anything the pull request
   controls, such as file names, appears there only inside escaped code spans.

   The label requests an override for one head commit, and the gate honours it only
   with two proofs:
   - **The label was added while this commit was the head.** On the `labeled` event
     that added it, the event is the proof. When the label is added, the `Registry
     override` workflow writes a commit status with the context
     `registry-override/pr-<number>` on the head commit named in that event, and posts
     a comment naming that commit; on any later event the gate honours the label only
     if the current head has that status. After a new push the gate ignores the
     label, and the same workflow removes it, so a maintainer adds it again after
     reviewing the new commits. (That workflow runs no code from the pull request or
     the repository, and is the only one that may write to pull requests or
     statuses.) The pull request's timeline cannot prove when a commit arrived: its
     `committed` events carry only git dates, which the author chooses.
   - **The repository owner approved this very commit.** The head commit needs an
     approving review whose commit is that head, by someone with admin access (the
     repository owner) who is not the author (write or maintain access is not
     enough), and it must be that reviewer's latest approving,
     change-requesting or dismissed review. The label alone is not enough, because it
     names whatever the head is when it is added: an author could push a new commit
     during a long review of an earlier one. A review of an earlier commit, a
     dismissed review, a comment-only review and a review by the author approve
     nothing. If the reviews or permissions cannot be read, the label is ignored.

   So, to override: review the head commit, approve it, then add the label. If you
   added the label first, approve the head and then rerun the `Registry gate` (the
   gate does not run on reviews, because a `pull_request_review` run would use the
   pull request's own copy of the workflow). If the gate reports that nothing shows
   the label was added for the head although the comment names it (a newer event can
   cancel the labeled run, and the gate retries reading the status only for 20
   seconds), rerun the gate too.

   The owner cannot approve their own pull request on GitHub, so for a pull request
   the owner wrote, the owner merges it with the ruleset's administrator bypass (see
   [Contributing](#contributing)) after running the gate locally, with the label counted as trusted
   because there is no `--event`:
   `bot-farm-silence-tracker registry gate --base-sha origin/main --head-sha HEAD --label registry-override`.

   To withdraw an override, remove the `registry-override` label. Requesting changes on
   the pull request alone does not re-run the gate.

   Every record must also pass the syntax rules: a Bluesky handle is a lower-case
   atproto domain name of at most 253 characters, a Reddit name has 3 to 20 of
   `A-Z a-z 0-9 _ -`, and an X username 1 to 15 of `A-Z a-z 0-9 _`. Record files may
   be at most 5 MB, evidence files 200 KB, and record lines 8 KB.
4. Spot-checks the changed records against the live sources, by ID, through the same
   adapters a scan uses, each with its own event's cutoff: the Arctic Shift archive for `reddit-archive` and the public
   Bluesky API for `bluesky`. With 15 or fewer changed records it checks all of them.
   A larger (labelled) change checks every record whose status changed or is not
   `scanned`, plus a sample of the rest, seeded by
   `sha256(base_sha, head_sha, pr_number)`, so a rerun draws the same sample. An
   author can still try many head commits locally and push one whose sample leaves out
   a chosen record among the rest, so the sample is a spot-check, not a guarantee. For
   each record:
   - Check A: own activity exists within 5 minutes of `last_activity_at`. If it does
     not, the record fails, unless the account has earlier activity (its owner may
     have deleted the last post) or the activity is within the source's ingestion
     lag. Both are inconclusive.
   - Check B: no own activity exists from one second after `last_activity_at` up to
     `scan_window_end` minus the ingestion lag (6 hours for the archive, 1 hour for
     Bluesky). Activity there fails the record: it hides activity, which is how a
     real account could be framed as having gone silent. Activity inside the lag is
     inconclusive. Activity after the scanned data ended is fine.

   The gate fails on any failed record, when more than 20% of the checked records are
   inconclusive, and when any record whose status changed or is not `scanned` is
   inconclusive. It fails closed: when the live check cannot finish (the request
   budget or the time limit runs out) or checks fewer than the minimum below, the
   pull request fails. It sends at most 0.5 requests per second and 200 requests, and stops
   after 15 minutes.

   **Minimum spot-check.** Every pull request that adds or changes records gets at
   least `min(n, 5)` of them re-fetched and checked, where `n` counts its changed
   records on a platform the check can query (`reddit-archive` and `bluesky`): all of
   them when there are 15 or fewer changed records, and above that every record whose
   status changed or is not `scanned` plus a seeded sample of at least 5 of the rest.
   The sample is drawn only from records the check can query, so a large change padded
   with `reddit` or `x` records still gets live checks. Records on `reddit` and `x`
   cannot be queried without credentials and are listed as unverified. Because the
   gate runs from the base branch on `pull_request_target`, a change to these rules
   takes effect for pull requests only after it is merged into `main`.

CI does not check `baseline_posts`, `baseline_active_days` or `cliff`; reviewers check
them against the evidence file (see the checklist in
`.github/pull_request_template.md`). On Bluesky, a post whose `createdAt` is backdated
before the window is listed at its `createdAt` position, so the scan and the check can
both miss it; reviewers read the profile itself.

The gate takes effect from the first pull request opened after the workflow is on
`main`, because `pull_request_target` always runs the base branch's workflow. The
pull request that added it shipped no records, only the schema.

Run the same checks locally with
`bot-farm-silence-tracker registry verify --base origin/main --head HEAD` (either side may
also be a registry directory; `--label registry-override` stands in for the label),
or the whole gate with
`bot-farm-silence-tracker registry gate --base-sha origin/main --head-sha HEAD`.

A deleted post can make Check A inconclusive just as an invented one does, so an
inconclusive or failed record is a reason to look closely, not proof of tampering.

### Registry chart

The chart at the top of this README is drawn from the registry files by
`bot-farm-silence-tracker registry chart`, which validates the registry with the same
rules as the gate and then writes `docs/charts/<event>-light.svg` and
`<event>-dark.svg` for every event, plus `default-light.svg` and `default-dark.svg` for
the default event. It shows counts only, never a username, handle or ID. For the event
it shows:

- the cumulative number of accounts with no activity since they stopped, by stop
  time and stacked by platform, from the start of the baseline window to the day it
  was drawn, with a line at the cutoff and at each marker (dashed when the marker's
  time is approximate);
- a histogram of the stop times in hours relative to the cutoff, each bar split by
  the marker (or the main outage) the stop is nearest to, so accounts that stopped
  together show as a spike;
- the counts by status: "verified" (`verified_inauthentic`, a person checked the
  evidence), and the automatic labels "candidate" (`candidate`) and "silent since
  takedown" (`scanned` with `cliff`). `cleared` records and `scanned` records without a
  cliff are not charted.

The same registry, events file, date and commit always give the same bytes. Run it
locally with `bot-farm-silence-tracker registry chart --as-of 2026-10-14`.

The `Registry chart` workflow (`.github/workflows/registry-chart.yml`) redraws the
charts when a push to `main` changes `registry/` or `events.toml`, and proposes them for
`docs/charts/` as `github-actions[bot]`. It redraws only data that passed the gate:
the push must be the merge of a pull request, and the latest `Registry gate` run on
that pull request's head commit (from the base branch's workflow, on
`pull_request_target`) must have succeeded; with several pull requests, all of them.
A direct push, or a merge whose gate failed, is missing or was bypassed, leaves the
charts as they are, and the run's summary says why. A maintainer can also start the
workflow by hand on `main`; it then redraws from the current head of `main` after
validating the registry again. A manual run charts everything on `main`, including
data that reached it without passing the gate, so review such data first. The chart
commit touches only `docs/charts/`, which
the workflow's path filter leaves out, so it never starts another run.

The ruleset of `main` (see [Contributing](#contributing)) lets nothing push to `main`
directly, so the workflow commits the charts to the `registry-charts` branch, rebuilt
from the current head of `main` on every run, and opens a pull request from it into
`main`, or updates the one already open. Pull requests opened by GitHub Actions start
no workflow runs, so the required checks do not report on it: the owner merges it with
the administrator bypass, or closes and reopens it to run the checks first. If the
repository does not allow GitHub Actions to create pull requests (Settings, Actions,
General, "Allow GitHub Actions to create and approve pull requests"), the run's
summary links the branch so the owner can open the pull request by hand.

### False positives

Every registry entry starts from a statistical flag. People take breaks, get
banned, delete accounts, or simply stop near a cutoff, and regular posters who stop at
the wrong moment look exactly like bots that went dark. A record should become
`verified_inauthentic` only on evidence beyond the silence itself, checked by the
repository owner, and `cleared` is the right outcome whenever the evidence is thin.

### Exporting real IDs locally

`export-ids` writes the real platform IDs, handles and scan facts of one run
(`--run-id N`) or of every run (`--all-runs`) to a CSV or JSON file you name:

```bash
bot-farm-silence-tracker export-ids --run-id 7 --out ~/private/run-7-ids.csv
```

The file contains real accounts. It is created readable by you only (on macOS and
Linux), nothing is uploaded, and the command prints a warning to keep it private.
Never commit it: `--out` inside a git repository is refused unless you pass
`--allow-inside-repo`, and `.gitignore` covers `*-ids.csv` and `*-ids.json`.

## Reporting

`report --registry` turns registry records into local drafts that a person submits
to the platform's moderators by hand:

```bash
bot-farm-silence-tracker report --registry registry --status verified_inauthentic \
  --platform reddit-archive --out ~/private/report-drafts
```

It drafts the records of one [outage event](#outage-events) (`--event`, else the
default event) and writes one Markdown draft per account, under
`<out>/<event-id>/<platform>/`. Each draft names the event and its cutoff, and says
when the same account also appears in another event:

- Reddit (`reddit-archive` or `reddit`): text for the form at
  <https://www.reddit.com/report> (reason "Spam", or the option for inauthentic or
  manipulated activity when the form offers one), plus a modmail template for the
  moderators of the subreddits where the account was most active.
- Bluesky: text for the in-app "Report account" flow, with the reason "Spam" (or
  "Misleading" if the account impersonates or deceives).

Drafts are written only for `verified_inauthentic` records. `--include-unverified`
drafts another `--status` for review: it prints a warning, and every such draft is
marked "UNVERIFIED — do not submit".

No draft is written, and the command says why, for a record whose account posted
again (it is no longer a cliff, or its last activity is at or after the cutoff, newer
than the silence window), whose silence is shorter than its event's
`verified_min_silence_days`, or whose evidence file was drafted for an older last
activity than the record now holds.

A draft states what was observed, not a conclusion: when the account stopped posting
and with how many similar accounts, the steady test's numbers, the run's placebo
comparison (rebuilt from the evidence file's numbers, never its text) and the live
profile status. It calls the outage "reported" and cites the event's notes; it never
calls the account part of a network, and it shows no uncalibrated p-value.

Each draft names the account by its ID, which is what the spot-check probed: the
Reddit author name, or the Bluesky DID. A Bluesky handle can change and pass to
another account, so the draft shows the handle only as seen at the scan and asks you
to confirm the profile at the DID link.

Each draft carries the evidence summary and the public links. The drafts are the only
place usernames are decoded, so they contain real account names: `--out` must be
outside the registry and outside any git repository (unless you pass
`--allow-inside-repo`; `.gitignore` covers `report-drafts/`), and the drafts must
stay private.

**Nothing is ever submitted automatically.** A person reads each draft, checks it
against the live platform, and submits it through the platform's own form. Platforms
treat bulk automated reporting as abuse, and each report needs a human judgment
behind it.

## Limitations

- **Sampling is not population-random.** No API used here can sample all users
  uniformly. "Random" means random within the sampling frame, and the frame depends on
  the seeds: accounts that posted in the window in the seed communities (Reddit, X,
  Bluesky search), accounts in the posts a seed feed showed in the window (Bluesky
  feeds), accounts that replied in the window to posts by the seed lists' members
  (Bluesky lists, the default), or the seed lists' members whether active or not
  (Bluesky `list_mode=members`). Frequent posters, the threads and anchors drawn, and
  the chosen seeds shape the frame. Do not report prevalence for a platform. The
  Bluesky repliers frame is read after the cutoff, so it cannot find repliers who were
  taken down or deleted by then: it measures cliffs, not suspensions or deletions. The list-members frame cannot
  estimate how many bot accounts went dark; bots are rarely on curated lists of
  officials and outlets.
- **Silence is not proof of a bot.** Vacations, illness, bans, deleted accounts,
  people leaving a platform, and API gaps all look like a cliff. Real people who are
  regular posters and happened to stop near the cutoff will be flagged.
- **The outage itself silenced ordinary people.** The outage that took the bot
  infrastructure offline also cut off ordinary users of the affected services, and
  its main collapse came at 03:30 local time (00:30 UTC), when most people there were
  asleep. So when many accounts stop at a marker or at the cutoff, the plain reading
  is "these accounts were affected by the outage", not "these accounts are bots".
  Stop times are also not independent or spread evenly over the window: people stop
  at the end of their day, and people in one conversation stop together. The
  permutation null of the clusters accounts for each account's posting hours and for
  accounts found in the same thread, but no test of timing can tell a bot from a
  person who used an affected service. That is why a synchronized stop never makes a
  candidate on its own, and a candidate needs a signal measured on the baseline.
- **The placebo comparison is a check, not a proof.** Three placebo weeks give a
  rough spread, and a wide interval is the honest result. A cliff rate clearly above
  the placebo says the outage week was unusual for these accounts, not why.
- **Suspensions are a signal, and also a confound.** A suspended account has no
  readable activity, so it cannot be scored. It is counted and, with
  `--include-unavailable`, listed.
- **It needs a few days after the cutoff.** Before `--min-silence-days` have passed
  results are provisional. Rerun later; the cache keeps this cheap.
- **Each account is judged on its own data.** An account whose cached data ends
  before the run's reference time (a window reused by `--from-run`, a cache hit
  within `--cache-ttl-hours`, or a partial fetch) has its silence measured only to
  that end, so data that was never fetched is never read as silence. Such rows
  carry a `data_end` column, the summary counts them, and a cliff among them stays
  provisional until its own silence reaches `--min-silence-days`. Use `--refresh`
  to refetch them.
- **History limits.** Platforms cap how much history they return, which can hide
  baseline activity. Adapters flag such accounts as truncated. The output has a
  `truncated` column, the summary counts them, and CSV and JSON files carry a note.
  Truncated histories may be false negatives: missing baseline activity can make a
  real cliff look irregular and ineligible.
- **Discovery windows.** Discovery can only find accounts the platform still lists.
  If a listing or search window does not reach back before the cutoff, the pool
  leans toward accounts that kept posting, which are exactly the ones that are not
  cliffs. The adapter warns when this happens. Discover early and use `--from-run`
  afterwards.
- **Reddit discovery is biased.** Listings are newest first and capped at about 1000
  items, so on a busy subreddit the listings alone reach back only hours or days. The
  adapter skips authors seen only after the cutoff and adds a thread-based path
  (`discovery=both`), but both paths over-represent frequent posters, popular
  threads and subreddits that are quiet enough to page back to the cutoff. Authors
  who posted rarely, or only in threads outside the top of the chosen window, are
  under-sampled. The adapter warns when a listing hits the cap before the cutoff.
- **Fetch errors.** Accounts that failed with status `error` are not scored. Rerun
  with `--from-run` to retry them.
- The seed list is a starting point. Curate it, and cover the political spectrum:
  the signal is behavioral, not ideological.

## Writing an adapter

See `src/bot_farm_silence_tracker/sources/base.py` for the interface and rules, and
`src/bot_farm_silence_tracker/sources/fake.py` for a worked example. Register the class in
`src/bot_farm_silence_tracker/sources/registry.py` (already pointing at
`bot_farm_silence_tracker.sources.reddit:RedditSource` and `bot_farm_silence_tracker.sources.x:XSource`). Use only
`self.http` for network calls so rate limits and the request budget apply.

The core gives adapters these hooks, all documented in `base.py` and `http.py`:

- `default_rps` and `default_burst` class attributes size the shared client's token
  bucket, which is the only pacer. `self.http.set_rate(...)` exists for rates that
  come from options.
- `self.http.configure(reset_wait=..., classify_error=..., before_attempt=...,
  on_response=..., on_attempt_failed=..., on_attempt_not_sent=...)` handles platform
  reset headers on 429 (waits up to `max_retry_wait`, 900 seconds by default, plus
  the reset hook's margin, and logs any wait over 5 seconds), maps 429 or 402 bodies
  to `BudgetExceeded`, and runs per-attempt cost checks, retries included.
  `request()` accepts the last four per call too. Every attempt whose
  `before_attempt` ran gets exactly one of `on_response`, `on_attempt_failed` (sent,
  no usable response: charge the worst case) or `on_attempt_not_sent` (never sent:
  release the reservation). A `Retry-After` over `max_retry_wait` on a 429 or 5xx,
  or `Retry-After` and backoff waits adding up to more than `max_total_wait` (1800
  seconds) in one request, raise `RateLimited`; reset-header waits do not count
  toward that total but have their own per-request cap, `max_reset_wait_total`
  (1800 seconds).
- `self.http.get_json(url)` raises `SourceAuthError` on 401 or 403 and
  `SourceError` on other 4xx, redirects and bad JSON, never a raw httpx error.
  `request()` maps every other httpx error (undecodable bodies, redirect loops,
  invalid URLs) to `SourceError` too.
- `data_horizon()` returns how far the source's data reaches (`None` for a live
  source, the default). It may make one cheap request. A plain `SourceError` from
  it stops the scan as a stale source (unless `--allow-stale-source`).
- `secret_options` lists extra option keys that must never be stored with a run.
- `self.warn(message)` reports skipped seeds, discovery that could not reach the
  cutoff, and similar conditions.
- `Account.meta["truncated"] = True` marks a capped history. When the fetch stopped
  early, also set `Account.meta["partial_fetch"] = {"since": <ISO time of the oldest
  item returned>, "posts": <items returned>}` (and `"reason": "platform_cap"` when
  the platform stopped returning history), so the pipeline caches the real coverage
  and later fetches only the older part.
- `config.ledger` records spend persistently. Use
  `config.ledger.reserve(usd, cap, scope)` before a billed request, which raises
  `BudgetExceeded` over the cap, and `reservation.settle(actual_usd)` after it.
  `add()` and `spent()` still work for adapters that do not need atomicity.
  `cost_summary()` returns the line the CLI prints on every exit.
- `plan_spend(...)` returns a `SpendPlan` before any request: projection lines, whether
  discovery is billed (the CLI then requires `--yes`), and an optional refusal (for
  example a missing cap). `cost_projection(...)` prints the `--dry-run-cost` projection
  for the stored sample.
- `supports_baseline_sampling` (default False) lets `--baseline-sample-rate` run on the
  adapter. Set it only when the adapter can answer `newest_activity(account, kind,
  since, until)` (the pre-check's existence query, kinds from `precheck_kinds`) with
  about one request: a search by time range sorted newest first with a limit of 1.
  The default implementation fetches the whole window, which is correct but saves
  nothing. A query narrowed to a later start reports it in `covered_since`.
- `max_concurrency` (default 1) is the highest `--concurrency` the adapter allows. An
  adapter that raises it must make `hydrate`, `fetch_activity` and its HTTP hooks safe
  to call from several threads at once (`config.concurrency` says how many run). The
  shared client's pacer, request budget and `cancel()` are already thread safe, and
  `self.http.hold(seconds)` pauses every worker after a load signal.
- `default_pool_factor` sets the discovery pool factor used when `--pool-factor` is not
  given, and `config.min_posts` carries `--min-posts` so a paid adapter can skip
  accounts that cannot qualify.
- `network_hosts` declares every host the adapter contacts, as `HostUse` entries
  (exact host name, purpose, which credentials and session tokens it receives, and
  whether it is used only with a login). The base class installs them as the shared
  client's host allowlist, so any other host, plain `http://`, a redirect off the
  list, or credentials sent to a host not declared to receive them raise
  `DisallowedHostError` before anything is sent. The `network` command prints the
  same declarations. Override `host_uses(options)` when an option changes a host, and
  list such an option in `credential_destination_options` when it decides where
  credentials go, so `--from-run` never takes it from the database. Each credential's
  value is also bound to the hosts declared for it, so it cannot reach another host
  even one that receives other credentials.
- Every `self.http.request` / `get` / `get_json` call passes `sends_credentials=True`
  or `False` (leaving it out raises `TypeError`): `True` whenever the request carries
  a credential or session token in any form. The client also detects the common forms
  (`Authorization` and API-key headers, `auth=`, token-like query parameters, password
  fields) and treats a request as credentialed when either says so.
- `required_credentials` and `optional_credentials` name `BOT_FARM_SILENCE_TRACKER_*` variables.
  `option_str`, `option_float`, `option_int` and `option_bool` read
  `--source-option` values.

## Contributing

Every change reaches `main` through a pull request. Who must approve it depends on
what it changes:

| The pull request changes | Review needed | Enforced by |
| --- | --- | --- |
| Only account records, `registry/events/<event-id>/<platform>/<shard>.jsonl`, all `scanned` or `candidate` on `bluesky` or `reddit-archive` | None | The `Registry gate` check: it validates the records and re-fetches at least `min(n, 5)` of them from the live sources (seeded per head commit), and fails if it cannot |
| Records moving into `verified_inauthentic` or `cleared`, records that already have one of those statuses, record removals, more than 15 records, records on `reddit` or `x`, evidence files, or a change to an existing outage event | The repository owner | The `Registry gate`: the `registry-override` label and the owner's approving review of the head commit (see [Checks on registry pull requests](#checks-on-registry-pull-requests)), and CODEOWNERS for evidence and `events.toml` |
| Anything else: code, workflows, docs, `events.toml`, `registry/schema.json`, evidence, CODEOWNERS | The repository owner | CODEOWNERS and the ruleset |

Registry data and other files never share a pull request: the gate fails a mixed one.

`.github/CODEOWNERS` gives every file to the owner (`@obviousthrowaway`, the account that
hosts this copy; see [Publishing your copy](#publishing-your-copy)) and leaves only the
account record files without an owner. Evidence (`registry/events/<event-id>/evidence/`),
`events.toml`, the schema and `.github/` keep the owner.

The ruleset of `main` is [`.github/rulesets/main.json`](.github/rulesets/main.json).
It applies to the default branch and:

- requires a pull request, with 0 required approvals but "Require review from Code
  Owners", so a pull request with only ownerless record files needs no approval and
  any other file needs the owner's;
- dismisses stale approvals when new commits are pushed, and requires the most recent
  push to be approved by someone other than its author;
- requires the status checks `gate` (Registry gate) and the `CI` and `Windows` jobs
  (`lint`, `test (ubuntu-latest)`, `test (macos-latest)`, `test-windows`, `package`,
  `binary (ubuntu-22.04)`, `binary (macos-14)`, `gui-app (macos-14)`,
  `gui-app (windows-latest)`, `docker`, `shellcheck`, `powershell`,
  `installer-sh (ubuntu-latest)`, `installer-sh (macos-latest)`,
  `installer-sh-busybox`, `installer-ps1 (powershell)`, `installer-ps1 (pwsh)`), all
  from GitHub Actions; each runs on every pull request;
- blocks force pushes and deleting `main`;
- lets one actor bypass it: the repository admin role, only through a pull request,
  so the owner can merge their own pull requests (GitHub does not let an author
  approve them) and the `Registry chart` workflow's chart pull requests. Nobody may
  push to `main` directly. GitHub does not accept the GitHub Actions app as a bypass
  actor of a ruleset in a repository owned by a user, so the `Registry chart`
  workflow proposes its chart commits in a pull request instead of pushing them.

Apply or update it with the script, which creates the ruleset or updates the one with
the same name, so it is safe to run again (it needs the `gh` CLI, logged in as an
admin of the repository):

```bash
scripts/apply_repo_rules.sh --dry-run your-name/bot-farm-silence-tracker  # print the payload only
scripts/apply_repo_rules.sh your-name/bot-farm-silence-tracker
```

Make sure CODEOWNERS names your account first: an owner GitHub cannot resolve owns
nothing, and then no file needs a code owner's review.

## Ethics and terms of service

- Use official platform APIs or openly published research archives through their
  documented APIs, within their terms. Never scrape platform websites, spoof user
  agents, or circumvent access controls or rate limits.
- Store the minimum: handles, ids, timestamps, communities and content hashes. No
  raw post text.
- Output is a list of candidates for manual review. Do not publicly accuse any
  individual account holder of being a bot, a troll or a state actor based on it.
  Corroborate with independent evidence, and treat the people behind accounts fairly.
