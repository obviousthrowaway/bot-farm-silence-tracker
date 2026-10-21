# Security and privacy

This page describes what Bot Farm Silence Tracker collects, where it keeps it, what it sends
over the network and to whom, and how to report a security problem. Everything here
describes what the code does today. When the code changes, this page changes with it.

## What is collected and where it is stored

Bot Farm Silence Tracker reads **public** account data through official platform APIs (Reddit,
X, Bluesky) or the Arctic Shift research archive. It stores the minimum needed to score
accounts:

- accounts: platform, user id, handle, creation date (when the platform gives one),
  status (active, suspended, deleted, private and so on) and a small set of metadata
  such as a post count or a truncation flag;
- activity: the timestamp, kind (post, comment, reply, repost, quote), community
  (for example the subreddit) and a **hash** of the content. Raw post text is never
  stored;
- runs: the scan's parameters, its sample and summary. Adapter options whose names
  look like secrets, and URLs that contain a user name or password, are never stored
  with a run;
- per-request instrumentation for some platforms: endpoint, status, latency and the
  waits chosen;
- the spend ledger (X only): the estimated USD spent per day and run.

Everything is kept in local SQLite databases, one per outage event (see "Outage
events" in docs/USAGE.md), under the cache folder:

| System | Default database path |
|---|---|
| Linux and macOS | `~/.cache/bot-farm-silence-tracker/events/<event-id>/bot-farm-silence-tracker.db` |
| Windows | `%LOCALAPPDATA%\bot-farm-silence-tracker\events\<event-id>\bot-farm-silence-tracker.db` |
| Docker image | `/data/.cache/bot-farm-silence-tracker/events/<event-id>/bot-farm-silence-tracker.db` inside the container (the `/data` volume) |

A database left by a version before outage events, directly in the cache folder, is
moved into the default event's folder on first use (a copy, if the folder is on another
device, keeps the original as `bot-farm-silence-tracker.db.pre-events-backup`; delete
it once the moved copy works). `BOT_FARM_SILENCE_TRACKER_CACHE_DIR` moves the cache
folder. Use `--db PATH` to name a database directly. If writing spend to the database fails, the
ledger writes a small fallback file next to the database. Results go to the file
you name with `--out`, or to `bot-farm-silence-tracker-run-<id>.csv` (or `.json`) in the
current folder. Delete the database and the output files to remove everything the
tool collected.

## No telemetry

Bot Farm Silence Tracker sends **no telemetry, analytics, crash reports or update checks**
anywhere. The only network requests a scan makes are the platform API requests
listed below. `report` and `network` make no requests at all.

The one other network request in the program is the hidden `selftest-tls` command,
which the release smoke tests run to check that HTTPS works. It runs only when you
call it and sends exactly one HEAD request, with no credentials and no data, to
`https://pypi.org/simple/`, or to the `--url` you give it, which must be an
`https://` address without a user name or password. It does not follow redirects,
uses no proxy and trusts only the bundled CA certificates (it ignores the proxy and
CA variables described below).

### Installers

The installers (`install.sh`, `install.ps1`) download the program and
`SHA256SUMS.txt` from the GitHub release, check the checksum, and then:

- when the [GitHub CLI](https://cli.github.com/) `gh` is installed and logged in, run
  `gh attestation verify <file> --repo obviousthrowaway/bot-farm-silence-tracker`, which contacts
  GitHub's attestation API through `gh` (see [Verifying downloads](#verifying-downloads));
- only when they fall back to uv (no prebuilt program for your system, or you asked for
  it): download Astral's uv installer from `astral.sh` if uv is missing (after asking,
  or when `BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1` is set), and let uv install the package from
  the release wheel, PyPI or git. uv may also download a Python interpreter (from
  GitHub, as uv's managed Python builds) when no suitable Python is installed, and the
  package's dependencies from PyPI.

They send nothing else. The double-click installers (the macOS `.pkg` and the Windows
`...-setup.exe`) download nothing: they carry the program, copy it into your user
account, and run it only to check it starts (`--version`) and to save your platform
choice (`config set-platforms`), neither of which uses the network. They are not signed,
so check them against `SHA256SUMS.txt` or with `gh attestation verify` (see
[Verifying downloads](#verifying-downloads)).

A copy whose installers still hold the owner placeholder has not been published
(see "Publishing your copy" in docs/USAGE.md). The placeholder cannot be a GitHub account
name, so no one else can serve files under it, and the installers refuse to run with it:
they print "This copy has not been published yet" and exit with an error before they
download anything. Only `BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL` or
`BOT_FARM_SILENCE_TRACKER_GITHUB_URL` lets such a copy run, and it then skips the
`gh attestation verify` check with a warning.

These environment variables change what the one-line installers download and from where:

| Variable | Effect |
|---|---|
| `BOT_FARM_SILENCE_TRACKER_SOURCE` (or the first argument / `-Source`) | Skip the prebuilt program and `uv tool install` this instead: a git URL, a PyPI name, a local checkout or a wheel file. A local wheel or checkout is not checked against `SHA256SUMS.txt`. |
| `BOT_FARM_SILENCE_TRACKER_VERSION` | The release tag to install instead of the latest. |
| `BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL` | Download the release files from this folder URL instead of GitHub (https only). The SHA256 check still runs; the `gh attestation verify` check is skipped. |
| `BOT_FARM_SILENCE_TRACKER_GITHUB_URL` | For tests: use this server in place of `https://github.com` (https only). |
| `BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1` | For tests only: accept `http://` addresses in the two variables above. |
| `BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET=1` | `install.sh` only: accept BusyBox `wget` as the only downloader. It may not validate TLS certificates, so by default the installer refuses it and asks for curl. |
| `BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1`, `BOT_FARM_SILENCE_TRACKER_NO_UV=1` | Install uv without asking, or never use uv. |
| `BOT_FARM_SILENCE_TRACKER_INSTALL_DIR`, `BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1` | Where the program goes, and whether the PATH is changed. No network effect. |

Set these only to values you trust: anyone who can set them can point the installer at
another server, and the checksum file then comes from that same server.

## Enforced host allowlist

Every request a platform adapter makes goes through one shared HTTP client, and that
client checks every attempt (retries, token requests and Bluesky session refreshes
included) **before** it is sent, and checks the request again as built (its final URL,
headers and body) just before sending:

- the URL must use `https://` on the default port 443. Plain `http://` and any other
  port are always refused;
- the host must be on the platform's allowlist, compared as an exact host name (no
  wildcards, no subdomains). A client with no allowlist installed refuses every
  request; every adapter installs its own before it can send anything;
- a request that carries credentials may go only to the hosts that are declared to
  receive credentials. Every adapter request states whether it carries credentials
  (the adapter must say so on each call), and the client also detects them itself: an
  `auth=(user, password)` tuple, an `Authorization`, `Proxy-Authorization` or `Cookie` header, a header
  named like an API key or token (`X-Api-Key`, `X-Auth-Token` and so on), a user name
  or password in the URL, a query parameter named like a token or key (`access_token`,
  `api_key`, `token` and so on), and a form or JSON body with a `password` or secret
  field. Either one is enough;
- each secret credential from the environment (a password, an app password, a client
  secret, a bearer token) may go only to the hosts declared for **that credential**.
  The client knows each secret's value and refuses a request to any other host that
  contains it, whatever the adapter declared. So Reddit's client secret cannot reach
  `oauth.reddit.com`, even though that host receives the access token. Public
  identifiers are not bound this way: the Bluesky handle, the Reddit username and the
  Reddit client id may appear in requests to other hosts (a scanned account can have
  the same name), and they are still sent only as the table below says;
- redirects are never followed. A redirect to a host that is not allowed, or to
  `http://`, stops the scan with an error.

Per credential, not only per host: credentials from the environment (the table
below) are bound to their own hosts when the adapter starts, and Reddit's access token
is bound to `oauth.reddit.com` as soon as it is issued. Bluesky's session tokens are
held per host, to the PDS, which is the only Bluesky host that receives credentials.
The client recognizes a bound value in the URL, the headers and the body in these
forms, and in no others:

- as is;
- URL-encoded, with `%20` or with `+` for a space;
- JSON-escaped, with non-ASCII characters escaped (`\u00e9`) or kept as they are;
- as the user or password of an `auth=(user, password)` tuple (the only kind of
  `auth` the client accepts; anything else is refused with `TypeError`);
- inside a `Basic` `Authorization` header, which the client decodes.

Anything else is **not** recognized: base64 outside a `Basic` header, hex, a change
of case, double encoding, a value split across several parts, or a hashed or
encrypted value. No adapter sends a credential in any of those ways. A value that
starts or ends with a letter or digit is matched only on token boundaries (not
inside a longer word), and values shorter than 8 characters are not matched at all,
so a short password cannot block an unrelated request.

A refused request raises `DisallowedHostError`, which stops the scan. Nothing is sent
to a refused destination, not even a connection attempt.

The allowlist is built from the hosts each adapter declares in its code. The `network`
command prints the same declarations, so what it shows is what is enforced:

```bash
bot-farm-silence-tracker network                      # every platform
bot-farm-silence-tracker network --platform reddit    # one platform
bot-farm-silence-tracker network --json               # machine-readable
bot-farm-silence-tracker network --platform bluesky --source-option pds_url=https://pds.example
```

It lists, per platform, the hosts contacted, which credential (by environment variable
name, never its value) is sent to which host and how, whether a login is required,
optional or not used, and the default request rate. It reads no credentials and sends
no requests.

| Platform | Hosts (https only) | Credentials sent |
|---|---|---|
| `reddit-archive` | `arctic-shift.photon-reddit.com` | none |
| `reddit` | `www.reddit.com` (OAuth token endpoint only), `oauth.reddit.com` (API reads) | client id and secret (HTTP Basic auth), and the password when set, to `www.reddit.com` only; the access token to `oauth.reddit.com` only; the username in the User-Agent header to both, as Reddit's API rules ask |
| `x` | `api.x.com` | the bearer token, to `api.x.com` only |
| `bluesky` | `public.api.bsky.app`; with a login also `bsky.social`, or the host of `--source-option pds_url` when it is set | none to `public.api.bsky.app`; the handle and app password (login) and the session tokens to the PDS host only |
| `fake` | none | none |

## Proxies and certificate authorities

By default a scan **ignores** the proxy and certificate settings of the environment:
`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` (and their lowercase forms), `NO_PROXY`,
`SSL_CERT_FILE`, `SSL_CERT_DIR` and `~/.netrc`. It connects directly to the platform
hosts and checks their certificates against the CA bundle that ships with the program
(certifi). An environment variable set by something else on the computer therefore
cannot route the scan, or its credentials, through another server.

If your network only allows traffic through a proxy, opt in with `--trust-env` (or set
`BOT_FARM_SILENCE_TRACKER_TRUST_ENV=1`). The scan then uses those variables like most HTTP
tools do and prints a warning. Know what that means: the proxy sees every host the scan
contacts, and a CA certificate from `SSL_CERT_FILE` or `SSL_CERT_DIR` (as corporate
TLS-inspecting proxies install) lets that proxy read and change the requests,
credentials and session tokens included. Use it only with a proxy you trust.

## Credentials

- **Environment only.** Credentials are read from `BOT_FARM_SILENCE_TRACKER_*` environment
  variables and nowhere else. Adapter options (`--source-option`) whose names look like
  secrets (`token`, `password`, `handle` on Bluesky and so on) are refused or never
  stored, so secrets do not end up in a stored run.
- **Never logged, stored or written to outputs.** Credentials are kept out of the
  adapter configuration's `repr`, tracebacks do not print local variables, and error
  messages that name a URL leave out its query string. Credentials are not written to
  the database or to the CSV and JSON output.
- **Sent only to the listed host, over https.** See the table above. The HTTP client
  refuses a request that carries credentials to any host not declared to receive
  them, and a request that contains a particular credential to any host not declared
  for that credential (see "Enforced host allowlist").

### Reddit

The client id, the client secret and the optional password go only to Reddit's token
endpoint. If you use the password grant, use a dedicated research account. The
access token it returns is kept in memory for the scan and sent only to
`oauth.reddit.com`.

### X

The bearer token is app-only and read-only. X bills per use, so every X scan needs an
explicit spend cap (`--max-usd`), and a scan that runs billed discovery needs `--yes`.
The cap is cumulative per UTC day by default and is enforced before every request,
retries included. It is an estimate based on X's published prices, not X's own
billing, so keep an eye on usage in your X developer account as well.

### Bluesky

Logged-out reads go only to `public.api.bsky.app` and carry no credentials. A login is
needed only for search. For a login:

- create an **app password** (Settings, Privacy and security, App passwords). Never use
  your main account password;
- use a **dedicated account** if you can: the searches are tied to it;
- **revoke the app password** when you are done.

The handle and app password are sent only in the login request to the PDS
(`bsky.social`, or the `pds_url` you set). The session tokens it returns are kept in
memory and sent only to that host. `pds_url` must be a plain `https://` origin: an
ASCII host name, optionally `:443`, with no user name, password, other port, path, query or
fragment. When a login goes anywhere but `bsky.social`, the scan says so before it
starts.

Because `pds_url` decides where the login goes, `--from-run` never takes it from the
database: anyone who could write to the database could otherwise send your app
password to their own server. If the run you continue used a `pds_url`, pass
`--source-option pds_url=...` again. Without it, a resume with a login stops with an
error, and a resume without a login ignores the stored value (logged-out reads never
use the PDS). Adapters mark such options in `credential_destination_options`, so the
rule covers any future adapter option that decides where credentials go.

## Verifying downloads

Each release has a `SHA256SUMS.txt` that lists the checksum of every file. The
one-line installers check the download against it and stop on a mismatch. To check by
hand:

```bash
sha256sum bot-farm-silence-tracker-linux-x64          # Linux
shasum -a 256 bot-farm-silence-tracker-macos-arm64    # macOS
```

```powershell
Get-FileHash .\bot-farm-silence-tracker-windows-x64.exe -Algorithm SHA256   # Windows
```

and compare the result with the file's line in `SHA256SUMS.txt`.

When the repository is public, each release file also has a build provenance
attestation signed by GitHub Actions. Check it with the
[GitHub CLI](https://cli.github.com/):

```bash
gh attestation verify bot-farm-silence-tracker-macos-arm64 --repo obviousthrowaway/bot-farm-silence-tracker
```

The installers run this check themselves when `gh` is installed and logged in.
Releases made while the repository is private have no attestation (GitHub offers them
for private repositories only on GitHub Enterprise Cloud). For those, when `gh`
reports "no attestations found", the installers print a warning and continue after the
SHA256 check; any other verification failure stops them.

## Changes to the repository

Every change to `main` goes through a pull request, under a ruleset kept in the
repository as `.github/rulesets/main.json` and applied by
`scripts/apply_repo_rules.sh <owner/repo>` (see "Repository rules" in docs/USAGE.md):

- **Account records need no reviewer.** `.github/CODEOWNERS` leaves only
  `registry/events/<event-id>/<platform>/<shard>.jsonl` without an owner. The
  `Registry gate`, a required check that runs the base branch's code and never the
  pull request's, validates such a change and re-fetches at least `min(n, 5)` of its
  records from the live sources, with a sample seeded per head commit. It fails
  closed: if the live check cannot run or finish, the pull request fails.
- **High-impact data needs the repository owner.** A record moving into
  `verified_inauthentic` or `cleared`, any change to a record that already has one of
  those statuses, a removal, more than 15 records, a record on `reddit` or `x` (which
  CI cannot check live), any evidence file change and any change to an existing
  outage event fail the gate unless the `registry-override` label was added for the
  head commit and someone with admin access (the repository owner) who is not the
  author approved that same commit. Write or maintain access is not enough.
- **Everything else needs the repository owner's review.** Code, workflows, docs,
  `events.toml`, the schema, evidence and CODEOWNERS are owned by the owner, and the
  ruleset requires code owner review. Data and code never share a pull request.
- **The ruleset** requires a pull request (0 approvals plus code owner review),
  dismisses stale approvals on push, requires the most recent push to be approved by
  someone other than its author, requires the `gate` check and the CI jobs (from
  GitHub Actions only), and blocks force pushes and deleting `main`. One actor may
  bypass it: the repository admin role, only through a pull request, so the owner can
  merge their own pull requests (GitHub does not let an author approve them). Nothing
  pushes to `main` directly: the `Registry chart` workflow proposes its chart
  commits, which touch only `docs/charts/`, in a pull request from the
  `registry-charts` branch.

If the owner in CODEOWNERS is not an account GitHub can resolve, no file has an owner
GitHub can resolve, so the code owner review requires nothing. Keep it pointing at a real
account before relying on these rules.

## Reporting a vulnerability

Please report security problems privately through GitHub's private vulnerability
reporting: open the repository's **Security** tab and choose **Report a vulnerability**
(this creates a private security advisory). Do not open a public issue for a security
problem. Include the version (`bot-farm-silence-tracker --version`), your system, and the
steps to reproduce it.
