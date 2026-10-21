# Installing Bot Farm Silence Tracker

[docs/USAGE.md](docs/USAGE.md#install) has the short version. This page has every option and
the details. None of the options needs administrator rights.

- [The double-click installers (macOS and Windows)](#the-double-click-installers)
- [The one-line installer](#the-one-line-installer)
- [Download the program by hand](#download-the-program-by-hand)
- [With Python tools](#with-python-tools)
- [With Docker](#with-docker)
- [While the repository is private](#while-the-repository-is-private)

## The double-click installers

Each [release](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases/latest) has an
installer for macOS and one for Windows. They install the desktop app and the
`bot-farm-silence-tracker` command for the terminal, and ask which platforms you want to
scan. They are listed in `SHA256SUMS.txt` and have a build provenance attestation like
the other release files. The desktop app inside them is the one the same release
publishes as its own files (`...-macos-<arch>.app.zip` and `...-gui-windows-x64.exe`). The
release workflow builds the installers only from those files and stops when one is
missing, so an installer never contains a stand-in app. (Builds of a pull request use a
stand-in only to test the installers, and never publish it.)

### Check the download first

The installers are not signed, so macOS and Windows warn before they open one, and the
steps below get past that warning. Check the file before you do that. Download
`SHA256SUMS.txt` from the same release, then, in the folder you downloaded to:

```bash
shasum -a 256 bot-farm-silence-tracker-<version>-macos-arm64.pkg     # macOS
```

```powershell
Get-FileHash .\bot-farm-silence-tracker-<version>-windows-x64-setup.exe -Algorithm SHA256   # Windows
```

The result must match the file's line in `SHA256SUMS.txt`. With the
[GitHub CLI](https://cli.github.com/) installed, also check where the file was built:

```bash
gh attestation verify bot-farm-silence-tracker-<version>-macos-arm64.pkg --repo obviousthrowaway/bot-farm-silence-tracker
```

If either check fails, do not open the file. (The one-line installer below runs both
checks itself.)

### macOS

Download the file for your Mac:

| Mac | File |
|---|---|
| Apple silicon (M1 and later) | `bot-farm-silence-tracker-<version>-macos-arm64.pkg` |
| Intel | `bot-farm-silence-tracker-<version>-macos-x64.pkg` |

(Apple menu > About This Mac shows "Chip: Apple M..." on Apple silicon and "Processor:
... Intel" on an Intel Mac.)

The installer is not signed by Apple, so macOS stops it the first time. After you have
[checked the download](#check-the-download-first):

- **macOS 15 (Sequoia) and later.** Double-click the `.pkg`. When macOS says it was not
  opened, click **Done**. Open **System Settings > Privacy & Security**, scroll down to
  the message about the `.pkg`, click **Open Anyway**, and confirm with your password or
  Touch ID. Then click **Open Anyway** once more.
- **macOS 14 (Sonoma) and earlier.** Control-click (or right-click) the `.pkg` in Finder,
  choose **Open**, then click **Open** in the dialog.

The installer then walks you through these steps:

1. **Introduction** says what the program does and which services it contacts.
2. **Installation Type** shows "Install for me only": no administrator password is needed.
3. Click **Customize** to choose the platforms to scan. Reddit through the Arctic Shift
   archive is ticked for you; Bluesky, X (needs your own paid API token) and the Reddit
   official API (needs approved API credentials) are optional. The installer never asks
   for passwords or keys.
4. **Install**, then **Close**.

It puts:

- the app in the Applications folder of your home folder
  (`~/Applications/Bot Farm Silence Tracker.app`; in Finder use Go > Home, then
  Applications), and it appears in Launchpad and Spotlight;
- the command-line program in `~/.local/bin`, and adds that folder to your PATH in your
  shell's startup file (as the one-line installer does). Open a new terminal window,
  then run `bot-farm-silence-tracker --help`.

Your platform choice is saved by running `bot-farm-silence-tracker config set-platforms`.
If you keep only the default (the Reddit archive) ticked, a platform list you saved
earlier is kept, so updating never undoes your own settings. Installing a newer `.pkg`
updates in place.

**Uninstall.** macOS has no list of installed programs to remove from, so the installer
adds an uninstall command. In Terminal, run:

```bash
bot-farm-silence-tracker-uninstall
```

It removes the app and the command-line program and keeps your scans and settings. Add
`--purge` to delete those too (`~/.cache/bot-farm-silence-tracker` and
`~/.config/bot-farm-silence-tracker`). It removes the PATH line only when nothing else is
left in `~/.local/bin`, because other programs often use that folder.

### Windows

Download `bot-farm-silence-tracker-<version>-windows-x64-setup.exe`,
[check it](#check-the-download-first), and double-click it. It is not signed, so Windows
SmartScreen may say "Windows protected your PC": click **More info**, then **Run anyway**.

Setup installs for your user account only (no administrator rights):

- the program goes to `%LOCALAPPDATA%\Programs\bot-farm-silence-tracker`, the same folder
  the one-line installer uses, and that folder is added to your user PATH;
- a **Bot Farm Silence Tracker** shortcut in the Start menu opens the app; you can also
  tick a desktop shortcut, and a **Bot Farm Silence Tracker (command line)** shortcut
  opens a Command Prompt where the `bot-farm-silence-tracker` command is ready;
- a **Platforms to scan** page lets you tick the platforms, with the Reddit archive ticked
  for you. The choice is saved by running `bot-farm-silence-tracker config set-platforms`;
- the last page has an **Open Bot Farm Silence Tracker** checkbox.

Running a newer Setup updates in place. To uninstall, use **Settings > Apps > Installed
apps**, find **Bot Farm Silence Tracker**, and choose **Uninstall**. That removes the
program, its shortcuts and its PATH entry. Your scans and settings are kept unless you
tick **Also delete my scans and settings**.

For a silent install, for example on many computers:

```powershell
.\bot-farm-silence-tracker-<version>-windows-x64-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /PLATFORMS=reddit-archive,bluesky
```

`/PLATFORMS` is optional (without it, the Reddit archive is used and an earlier choice is
kept). A silent uninstall (`unins000.exe /VERYSILENT` in the program folder) always keeps
your scans and settings.

## While the repository is private

> The one-line installers fetch `install.sh`,
> `install.ps1` and the release files from public GitHub addresses, so they cannot reach
> a private repository, and the Releases page needs access to it. Until the repository is
> public, use the [GitHub CLI](https://cli.github.com/) with an account that has access:
>
> ```bash
> gh release download v0.1.0 -R obviousthrowaway/bot-farm-silence-tracker -D release
> gh repo clone obviousthrowaway/bot-farm-silence-tracker && cd bot-farm-silence-tracker
> # Install the Python package from the downloaded wheel (needs uv):
> sh install.sh ../release/bot_farm_silence_tracker-0.1.0-py3-none-any.whl
> # Or install the prebuilt program from the downloaded folder. The installer only
> # accepts https:// addresses, or http:// ones when BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1
> # is set (it does not accept file://), so serve the folder on this computer:
> python3 -m http.server 8000 --bind 127.0.0.1 -d ../release &
> BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL=http://127.0.0.1:8000 BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 sh install.sh
> ```
>
> Installing a local wheel skips the installer's checksum check; compare it with
> `SHA256SUMS.txt` yourself. With `BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL` the installer
> checks the SHA256 checksums but skips the build provenance check.
>
> Releases made while the repository is private have no build provenance attestation
> (GitHub offers attestations for private repositories only on GitHub Enterprise Cloud).
> When `gh` is logged in and reports "no attestations found" for a downloaded release
> file, the installers print a warning and continue after the SHA256 check.

## The one-line installer

> **Before the first release.** The prebuilt programs are published on the
> [Releases page](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases) when a
> version is tagged. Until the first release exists, the installer has no prebuilt program
> to download, so it installs from source with [uv](https://docs.astral.sh/uv/) instead
> (it asks before installing uv itself). That path uses Python, which uv downloads for
> you, and needs [git](https://git-scm.com/downloads); on macOS, the first use of `git`
> may ask you to install the Command Line Tools. Once a release is out, the installer
> downloads the prebuilt program and you need neither Python nor git.

When a release exists, the installer downloads the ready-made program for your computer
from the latest [GitHub Release](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases/latest),
checks its SHA256 checksum, runs it once to make sure it works, and puts it in a folder in
your user account.

macOS or Linux (open the Terminal app and paste):

```bash
curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh
```

Windows (open PowerShell and paste):

```powershell
irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
```

The installer also asks which social platforms you want to scan (see
[Choosing platforms](docs/USAGE.md#choosing-platforms)); press Enter for the default, Reddit through
the Arctic Shift archive. To choose without a prompt, pass `--platforms` (or set
`BOT_FARM_SILENCE_TRACKER_PLATFORMS`) to the `sh` that runs the installer, or `-Platforms` to the
PowerShell script block:

```bash
curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | sh -s -- --platforms reddit-archive,bluesky
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1))) -Platforms reddit-archive,bluesky
```

Then open a new terminal window and check it:

```bash
bot-farm-silence-tracker --version
```

The program goes to `~/.local/bin` on macOS and Linux and to
`%LOCALAPPDATA%\Programs\bot-farm-silence-tracker` on Windows, and that folder is added to your
PATH (for fish, through `~/.config/fish/conf.d/bot-farm-silence-tracker.fish`). To remove it,
delete the file (`rm ~/.local/bin/bot-farm-silence-tracker`, or delete the Windows folder).

To install a specific version, set the variable for the `sh` (or PowerShell) that runs
the installer, not for `curl`:

```bash
curl -LsSf https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.sh | BOT_FARM_SILENCE_TRACKER_VERSION=v0.1.0 sh
```

```powershell
$env:BOT_FARM_SILENCE_TRACKER_VERSION = 'v0.1.0'; irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
```

A version that does not exist is an error; the installer does not silently install
something else.

**When the installer uses uv.** It installs with uv only when there is no prebuilt program
for your system: musl-based Linux (such as Alpine), 32-bit Windows, or no release yet.
When a release exists, uv installs the Python package (wheel) attached to that release,
after checking it against `SHA256SUMS.txt`; only when there is no release yet does it
install the source code from GitHub, which needs `git`. If uv is not installed, it shows the address of Astral's installer and asks before running
it; without a terminal to ask on, set `BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1` to allow it. Set
`BOT_FARM_SILENCE_TRACKER_NO_UV=1` to never use uv. A failed download or a checksum mismatch
always stops the installer with an error; it never switches to uv in that case. On Windows
on ARM it installs the x64 program, which Windows runs through emulation.

**The uv paths are less locked down than the standalone program.** The standalone binary is
one file whose checksum the installer verifies. The uv fallbacks (the release wheel, and
the source at a release tag or on `main`) resolve their dependencies fresh from PyPI when
you install. The wheel itself is checked against `SHA256SUMS.txt`, but the packages it
depends on are not, so a later or compromised upload on PyPI can change what you get.
Prefer the standalone binary where one exists for your system.

**Downloaders.** The installer needs `curl` or `wget`. BusyBox `wget` (the default on
Alpine and some minimal images) may not validate TLS certificates, so when it is the only
downloader the installer refuses to run; install `curl` (for example `apk add curl`) and
rerun. To accept that risk, set `BOT_FARM_SILENCE_TRACKER_ALLOW_BUSYBOX_WGET=1`.

**Checking where a download came from.** Each release file has a build provenance
attestation signed by GitHub Actions. With the [GitHub CLI](https://cli.github.com/)
installed, check a downloaded file with:

```bash
gh attestation verify bot-farm-silence-tracker-macos-arm64 --repo obviousthrowaway/bot-farm-silence-tracker
```

The installers run this check themselves when `gh` is installed and logged in.

**All options.** The comments at the top of
[`install.sh`](install.sh) and [`install.ps1`](install.ps1) list every option and
environment variable, and [SECURITY.md](SECURITY.md) lists what each one changes.

## Download the program by hand

Go to the [Releases page](https://github.com/obviousthrowaway/bot-farm-silence-tracker/releases/latest)
and download the file for your system:

| System | File |
|---|---|
| Windows (64-bit) | `bot-farm-silence-tracker-windows-x64.exe` |
| macOS, Apple silicon (M1 and later) | `bot-farm-silence-tracker-macos-arm64` |
| macOS, Intel | `bot-farm-silence-tracker-macos-x64` |
| Linux (64-bit, x86) | `bot-farm-silence-tracker-linux-x64` |
| Linux (64-bit, ARM) | `bot-farm-silence-tracker-linux-arm64` |

`SHA256SUMS.txt` on the same page lists each file's checksum. Check the file before you
run it, and before you get past any Gatekeeper or SmartScreen warning below:
`shasum -a 256 <file>` (macOS), `sha256sum <file>` (Linux) or
`Get-FileHash <file> -Algorithm SHA256` (Windows) must print the checksum on the file's
line in `SHA256SUMS.txt`, and, with the [GitHub CLI](https://cli.github.com/),
`gh attestation verify <file> --repo obviousthrowaway/bot-farm-silence-tracker` must succeed
(see [Verifying downloads](SECURITY.md#verifying-downloads)).

- **Windows.** The program runs in a terminal, so double-clicking it only flashes a
  window. Put the `.exe` in a folder, open PowerShell in that folder (Shift + right-click
  in the folder, "Open PowerShell window here"), and run `.\bot-farm-silence-tracker-windows-x64.exe --help`.
  Windows SmartScreen may say "Windows protected your PC" because the file is not signed:
  choose "More info", then "Run anyway".
- **macOS.** The file is not signed or notarized, so Gatekeeper blocks a copy downloaded in
  a browser ("cannot be opened because the developer cannot be verified"). Either:
  - run `xattr -d com.apple.quarantine ~/Downloads/bot-farm-silence-tracker-macos-arm64`, or
  - try to open it once, then go to System Settings > Privacy & Security, scroll to the
    message about the blocked program, and choose "Open Anyway".

  Also run `chmod +x ~/Downloads/bot-farm-silence-tracker-macos-arm64` so it can start. The
  one-line installer avoids all of this, because files fetched with `curl` are not
  quarantined.
- **Linux.** Run `chmod +x bot-farm-silence-tracker-linux-x64` and move it to a folder on your PATH.
  The Linux programs need glibc 2.28 or newer (for example Ubuntu 20.04, Debian 11, RHEL 8
  or later). They do not run on musl-based systems such as Alpine; use the Python tools
  or Docker there.

## With Python tools

If you already use Python, install it as an isolated tool (needs Python 3.12 or newer;
uv fetches one for you if needed):

```bash
uv tool install git+https://github.com/obviousthrowaway/bot-farm-silence-tracker
# or
pipx install git+https://github.com/obviousthrowaway/bot-farm-silence-tracker
# or run it once without installing
uvx --from git+https://github.com/obviousthrowaway/bot-farm-silence-tracker bot-farm-silence-tracker --help
```

Remove it with `uv tool uninstall bot-farm-silence-tracker` (or `pipx uninstall bot-farm-silence-tracker`).

## With Docker

```bash
docker build -t bot-farm-silence-tracker https://github.com/obviousthrowaway/bot-farm-silence-tracker.git
docker run --rm -v bfst-data:/data bot-farm-silence-tracker scan --platform reddit-archive --n 30
```

The program runs as an unprivileged user, with `/data` as its home and working folder.
The `bfst-data` volume therefore holds both the output files and the cache databases
(`/data/.cache/bot-farm-silence-tracker/events/<event-id>/`, one per outage event), so `--from-run` and `report`
work across runs. To get the files straight into your current folder instead, mount it
and run as yourself so the files belong to you (the cache then goes to `.cache` in that
folder):

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/data bot-farm-silence-tracker scan --platform reddit-archive --n 30
```

