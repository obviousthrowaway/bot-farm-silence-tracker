# Bot Farm Silence Tracker installer for Windows (PowerShell 5.1 or newer).
#
#   irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
#
# What it does:
#   1. Downloads the prebuilt bot-farm-silence-tracker.exe from the latest GitHub Release.
#   2. Checks its SHA256 checksum (and, when the GitHub CLI "gh" is installed and logged
#      in, its build provenance attestation).
#   3. Runs it once ("--version") to confirm it works on this computer.
#   4. Puts it in %LOCALAPPDATA%\Programs\bot-farm-silence-tracker and adds that folder to your
#      user PATH.
#   5. Records which social platforms you want to scan, by running the installed program's
#      "config set-platforms" command (this script never writes the config file itself).
#
# No Python and no administrator rights are needed. Everything is written under your user
# profile. On Windows on ARM the x64 program is used; Windows runs it through emulation.
#
# When there is no prebuilt program for your system (32-bit Windows, or a release that
# does not have it yet), it can install the program with uv (https://docs.astral.sh/uv/)
# instead: from the Python package (wheel) attached to the same release, checked against
# SHA256SUMS.txt, or, when there is no release yet, from the source code on GitHub (that
# needs git). If uv is not installed, it asks before running Astral's uv installer. A
# failed download or a checksum mismatch never switches to uv: the installer stops instead.
#
# Options (parameters, or environment variables set before the command):
#   -Platforms / BOT_FARM_SILENCE_TRACKER_PLATFORMS
#       The platforms to scan, as a comma list: reddit-archive, bluesky, x, reddit.
#       Default: reddit-archive (Reddit through the Arctic Shift archive, no account
#       needed). Given a console and no list, the installer offers a short menu. For a
#       platform that needs credentials (x, reddit) it prints which environment variables
#       to set and where to get them; it never asks for or stores credentials.
#   -Source / BOT_FARM_SILENCE_TRACKER_SOURCE
#       Skip the prebuilt program and "uv tool install" this instead: a git URL
#       (git+https://...), a PyPI name, a local checkout or a wheel file.
#   -Version / BOT_FARM_SILENCE_TRACKER_VERSION       Release tag, e.g. v0.1.0 (default: latest).
#   -InstallDir / BOT_FARM_SILENCE_TRACKER_INSTALL_DIR
#       Where to put the program (default: %LOCALAPPDATA%\Programs\bot-farm-silence-tracker).
#   BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH=1   Do not change the user PATH.
#   BOT_FARM_SILENCE_TRACKER_NO_UV=1            Never use uv; fail if there is no prebuilt program.
#   BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1         Allow installing uv without asking.
#   BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL   Download the release files from this folder URL
#                                         instead of GitHub (https only).
#   BOT_FARM_SILENCE_TRACKER_GITHUB_URL         For tests: use this server in place of
#                                         https://github.com (https only).
#   BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1    For tests only: allow http:// URLs.
#
# A copy whose $Repo below still holds the owner placeholder has not been published (see
# "Publishing your copy" in the README). The installer then refuses to run, unless
# BOT_FARM_SILENCE_TRACKER_GITHUB_URL or BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL replaces
# GitHub as the download base, and even then it skips the attestation check with a warning.
#
# Examples:
#   $env:BOT_FARM_SILENCE_TRACKER_VERSION = 'v0.1.0'; irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/obviousthrowaway/bot-farm-silence-tracker/main/install.ps1))) -Source C:\path\to\checkout
#
# To remove it later: delete the folder %LOCALAPPDATA%\Programs\bot-farm-silence-tracker and
# remove it from your user PATH (Settings > System > About > Advanced system settings >
# Environment Variables). If it was installed through uv: uv tool uninstall bot-farm-silence-tracker
#
# Everything below runs inside one script block, so "irm | iex" leaves no variables or
# functions behind in your PowerShell session.

& {
    [CmdletBinding()]
    param(
        [string]$Source,
        [string]$Version,
        [string]$InstallDir,
        [string[]]$Platforms
    )

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    # Older PowerShell defaults to TLS 1.0 for web requests.
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    $Repo = 'obviousthrowaway/bot-farm-silence-tracker'
    # The owner placeholder in $Repo above, written in two parts so that the replace
    # command in "Publishing your copy" (README) rewrites $Repo but leaves this check intact.
    $OwnerPlaceholder = '_' + '_OWNER__'
    # While $Repo holds the placeholder, it names no real repository: it must not be used
    # for downloads or attestation.
    $Unpublished = $Repo.Contains($OwnerPlaceholder)
    $UnpublishedMessage = "This copy has not been published yet: replace $OwnerPlaceholder with the GitHub account that hosts it (see Publishing your copy in the README)."
    # Refuse before anything is downloaded, unless a download base override (a test
    # server or a release folder) replaces GitHub.
    if ($Unpublished -and -not $env:BOT_FARM_SILENCE_TRACKER_GITHUB_URL -and -not $env:BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL) {
        throw $UnpublishedMessage
    }
    $UvInstallerUrl = 'https://astral.sh/uv/install.ps1'
    $ToolName = 'bot-farm-silence-tracker'
    # $IsWindows does not exist in Windows PowerShell 5.1, which only runs on Windows.
    $OnWindows = ($null -eq $IsWindows) -or $IsWindows

    if (-not $Source) { $Source = $env:BOT_FARM_SILENCE_TRACKER_SOURCE }
    if (-not $Version) { $Version = $env:BOT_FARM_SILENCE_TRACKER_VERSION }
    if (-not $InstallDir) { $InstallDir = $env:BOT_FARM_SILENCE_TRACKER_INSTALL_DIR }
    # -Platforms a,b arrives as an array in PowerShell; the environment variable is a string.
    if ($Platforms) { $Platforms = $Platforms -join ',' } else { $Platforms = $env:BOT_FARM_SILENCE_TRACKER_PLATFORMS }
    if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\bot-farm-silence-tracker' }
    if ($InstallDir -match '[;\r\n"]') {
        throw "The install folder must not contain a semicolon, a quote or a line break: $InstallDir"
    }
    $InstallDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDir)

    function Test-Flag([string]$Value) { return $Value -eq '1' }

    function Test-Command([string]$Name) {
        return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
    }

    function Test-Url([string]$Url) {
        if (-not $Url.StartsWith('https://') -and -not (Test-Flag $env:BOT_FARM_SILENCE_TRACKER_INSECURE_TEST)) {
            throw "Refusing a non-https URL: $Url (set BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1 only for local tests)"
        }
    }

    # Run a native program and leave its exit code in $LASTEXITCODE. Windows PowerShell 5.1
    # turns a native program's stderr into error records when the stream is redirected,
    # and with $ErrorActionPreference = 'Stop' the first one would abort the installer.
    # 'Continue' here keeps them as plain output; callers judge the exit code instead.
    function Invoke-Native([string]$Exe, [string[]]$Arguments) {
        $ErrorActionPreference = 'Continue'
        & $Exe @Arguments
    }

    function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
        Invoke-Native $Exe $Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE."
        }
    }

    # ------------------------------------------------------------ platform choice
    $DefaultPlatform = 'reddit-archive'
    $KnownPlatforms = @('reddit-archive', 'bluesky', 'x', 'reddit')

    # Check a comma or space separated list against the platforms this installer knows and
    # return it as a lowercase, de-duplicated comma list. Runs before anything is
    # downloaded, so a typo installs nothing.
    function ConvertTo-PlatformList([string]$Text) {
        $names = New-Object System.Collections.Generic.List[string]
        foreach ($item in ($Text -split '[,\s]+')) {
            $name = $item.Trim().ToLowerInvariant()
            if (-not $name) { continue }
            if ($KnownPlatforms -notcontains $name) {
                throw "Unknown platform '$name'. Choose from: $($KnownPlatforms -join ', '). Nothing was installed."
            }
            if (-not $names.Contains($name)) { $names.Add($name) }
        }
        if ($names.Count -eq 0) {
            throw '-Platforms needs at least one platform (for example reddit-archive). Nothing was installed.'
        }
        return ($names -join ',')
    }

    # Offer the platform menu. Enter picks the default. Returns the comma list, or $null
    # when the console cannot be read (the caller then uses the default).
    function Read-PlatformChoice {
        Write-Host ''
        Write-Host 'Which social platforms do you want to scan?'
        Write-Host '  [1] Reddit (Arctic Shift archive, no account needed) - default'
        Write-Host '  [2] Bluesky (no account needed for the default mode)'
        Write-Host '  [3] X (needs your own paid API token)'
        Write-Host '  [4] Reddit official API (needs approved API credentials)'
        Write-Host 'Several are fine, for example 1,2. You can change this later with:'
        Write-Host "  $ToolName config set-platforms reddit-archive,bluesky"
        $map = @{ '1' = 'reddit-archive'; '2' = 'bluesky'; '3' = 'x'; '4' = 'reddit' }
        for ($tries = 0; $tries -lt 5; $tries++) {
            # Only "no console to read" ends the menu; a pipeline stop or Ctrl-C must propagate.
            try { $answer = Read-Host 'Choice [1]' }
            catch [System.Management.Automation.PSInvalidOperationException] { return $null }
            catch [System.IO.IOException] { return $null }
            $picked = New-Object System.Collections.Generic.List[string]
            $bad = $null
            foreach ($token in ($answer -split '[,\s]+')) {
                if (-not $token) { continue }
                if ($map.ContainsKey($token)) {
                    if (-not $picked.Contains($map[$token])) { $picked.Add($map[$token]) }
                } else { $bad = $token }
            }
            if (-not $bad) {
                if ($picked.Count -eq 0) { return $null }
                return ($picked -join ',')
            }
            Write-Host "'$bad' is not one of 1, 2, 3 or 4."
        }
        throw 'No valid platform choice. Rerun with -Platforms reddit-archive. Nothing was installed.'
    }

    # Print which environment variables the chosen platforms need and where to get them.
    # This only prints: credentials are never asked for, taken or stored here.
    function Write-CredentialHelp {
        $chosen = $PlatformList -split ','
        if (($chosen -notcontains 'x') -and ($chosen -notcontains 'reddit')) { return }
        Write-Host ''
        Write-Host 'Some of the platforms you chose need credentials. Set them in your environment'
        Write-Host 'yourself; this installer never asks for or stores them.'
        if ($chosen -contains 'x') {
            Write-Host ''
            Write-Host '  X (--platform x):'
            Write-Host '    BOT_FARM_SILENCE_TRACKER_X_BEARER_TOKEN'
            Write-Host '    Get an app bearer token at https://developer.x.com (pay-per-use credits).'
            Write-Host '    X bills per post, so every scan needs an explicit --max-usd.'
        }
        if ($chosen -contains 'reddit') {
            Write-Host ''
            Write-Host '  Reddit official API (--platform reddit):'
            Write-Host '    BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_ID'
            Write-Host '    BOT_FARM_SILENCE_TRACKER_REDDIT_CLIENT_SECRET'
            Write-Host '    BOT_FARM_SILENCE_TRACKER_REDDIT_USERNAME'
            Write-Host '    (optional) BOT_FARM_SILENCE_TRACKER_REDDIT_PASSWORD'
            Write-Host '    Reddit needs to approve API access first (Responsible Builder Policy),'
            Write-Host '    then create an app at https://www.reddit.com/prefs/apps.'
        }
    }

    # The command shown as the first run: the first chosen platform that needs no credentials.
    function Get-FirstRunCommand {
        $chosen = $PlatformList -split ','
        if ($chosen -contains 'reddit-archive') { return "$ToolName scan --platform reddit-archive --n 30" }
        if ($chosen -contains 'bluesky') { return "$ToolName scan --platform bluesky --n 30" }
        return "$ToolName scan --platform $DefaultPlatform --n 30   (after setting the credentials above, use your platform)"
    }

    # Have the installed program record the choice. The program owns the config file
    # format; this script only passes the list. A choice that was not made (the default)
    # does not overwrite platforms the user saved earlier.
    function Save-Platforms([string]$Exe) {
        # Builds pinned to a release from before the config command cannot save a choice.
        Invoke-Native $Exe @('config', '--help') *> $null
        if ($LASTEXITCODE -ne 0) { return }
        $arguments = @('config', 'set-platforms')
        if ($PlatformFrom -eq 'default') { $arguments += '--if-unset' }
        $arguments += $PlatformList
        Write-Host "==> Saving your platform choice: $PlatformList"
        Write-Host "    $Exe $($arguments -join ' ')"
        Invoke-Native $Exe $arguments
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not save the platform choice. Run: $ToolName config set-platforms $PlatformList"
        }
    }

    # Download Url to OutFile. Returns the HTTP status code when the server answered
    # (200 on success); any other failure (network, TLS, Ctrl-C) is thrown.
    function Get-Download([string]$Url, [string]$OutFile) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile
            return 200
        } catch {
            # WebException (5.1) and HttpResponseException (7+) both carry the response.
            $response = $null
            if ($_.Exception.PSObject.Properties['Response']) { $response = $_.Exception.Response }
            if ($null -ne $response -and $null -ne $response.StatusCode) { return [int]$response.StatusCode }
            throw
        }
    }

    # Return the tag of the latest GitHub Release, or $null when there is no release yet.
    # GitHub answers /releases/latest with a redirect (3xx) to /releases/tag/<tag>, or to
    # /releases when there is none. Any other answer (an error status such as 429 or 503,
    # or a page from a proxy) is not "no release" and is thrown, as are network and TLS
    # errors.
    function Get-LatestTag {
        $request = [Net.HttpWebRequest]::Create("$GitHub/$Repo/releases/latest")
        $request.AllowAutoRedirect = $false
        $request.Method = 'HEAD'
        $request.UserAgent = 'bot-farm-silence-tracker-installer'
        try {
            $response = $request.GetResponse()
        } catch {
            # An error status still carries the response; PowerShell may wrap the
            # WebException in a MethodInvocationException.
            $ex = $_.Exception
            while ($null -ne $ex -and -not ($ex -is [Net.WebException])) { $ex = $ex.InnerException }
            if ($null -eq $ex -or $null -eq $ex.Response) { throw }
            $response = $ex.Response
        }
        try {
            $status = [int]$response.StatusCode
            $location = [string]$response.Headers['Location']
        } finally { $response.Close() }
        $url = "$GitHub/$Repo/releases/latest"
        if ($status -lt 300 -or $status -gt 399) {
            throw "GitHub answered HTTP $status for $url instead of a redirect, so the latest release is unknown (rate limit, outage or a proxy in the way). Nothing was installed. Try again later, or set BOT_FARM_SILENCE_TRACKER_VERSION to a release tag."
        }
        if ($location -match '/releases/tag/([^/?#]+)$') {
            $tag = $Matches[1]
            if ($tag -notmatch '^[A-Za-z0-9._+-]+$') { throw "Unexpected release tag from GitHub: $tag" }
            return $tag
        }
        if ($location -match '/releases$') { return $null }
        throw "GitHub redirected $url to an unexpected place ($location), so the latest release is unknown. Nothing was installed."
    }

    # Check File against the entry for Name in the downloaded SHA256SUMS.txt.
    function Test-Checksum([string]$File, [string]$Name, [string]$Sums) {
        Write-Host '==> Verifying SHA256'
        $expected = $null
        foreach ($line in Get-Content -LiteralPath $Sums) {
            $parts = $line.Trim() -split '\s+', 2
            if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $Name) { $expected = $parts[0]; break }
        }
        if (-not $expected) { throw "SHA256SUMS.txt has no entry for $Name. Nothing was installed." }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash.ToLower()
        if ($actual -ne $expected.ToLower()) {
            throw "Checksum mismatch for $Name (expected $expected, got $actual). Nothing was installed."
        }
        Write-Host "    ok: $actual"
    }

    # Download the Python package (wheel) attached to the release into $tmp and check it
    # against SHA256SUMS.txt. Returns its path, or $null when the release has no wheel.
    function Get-ReleaseWheel {
        $sums = Join-Path $tmp 'SHA256SUMS.txt'
        $code = Get-Download "$base/SHA256SUMS.txt" $sums
        if ($code -eq 404) { return $null }
        if ($code -ne 200) { throw "Downloading $base/SHA256SUMS.txt failed with HTTP $code. Nothing was installed." }
        $name = $null
        foreach ($line in Get-Content -LiteralPath $sums) {
            $parts = $line.Trim() -split '\s+', 2
            if ($parts.Count -eq 2) {
                $file = $parts[1].TrimStart('*')
                if ($file -match '^bot_farm_silence_tracker-[A-Za-z0-9._+]+-py3-none-any\.whl$') { $name = $file; break }
            }
        }
        if (-not $name) { return $null }
        $wheel = Join-Path $tmp $name
        Write-Host "==> Downloading $name"
        Write-Host "    $base/$name"
        $code = Get-Download "$base/$name" $wheel
        if ($code -ne 200) { throw "Downloading $base/$name failed with HTTP $code. Nothing was installed." }
        Test-Checksum $wheel $name $sums
        Test-Attestation $wheel
        return $wheel
    }

    function Confirm-UvInstall {
        Write-Host "uv is not installed. It can be installed with Astral's official installer:"
        Write-Host "    $UvInstallerUrl"
        if (Test-Flag $env:BOT_FARM_SILENCE_TRACKER_ALLOW_UV) {
            Write-Host 'BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1 is set, so installing it.'
            return
        }
        if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            $answer = Read-Host 'Install uv now? [y/N]'
            if ($answer -match '^(y|yes)$') { return }
            throw 'uv was not installed. Install it yourself (https://docs.astral.sh/uv/) and rerun, or set BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1.'
        }
        throw 'There is no console to ask whether to install uv. Set BOT_FARM_SILENCE_TRACKER_ALLOW_UV=1 to allow it, or install uv yourself (https://docs.astral.sh/uv/).'
    }

    function Install-WithUv([string]$Src) {
        if (Test-Flag $env:BOT_FARM_SILENCE_TRACKER_NO_UV) {
            throw 'BOT_FARM_SILENCE_TRACKER_NO_UV=1 is set, so uv is not used. Nothing was installed.'
        }
        if (Test-Path -LiteralPath $Src) { $Src = (Resolve-Path -LiteralPath $Src).Path }
        $origPath = $env:Path
        $localBin = Join-Path $HOME '.local\bin'
        $env:Path = "$localBin;$env:Path"
        try {
            if (Test-Command 'uv') {
                Write-Host "==> uv found: $((Get-Command uv).Source)"
            } else {
                Confirm-UvInstall
                Invoke-Expression (Invoke-RestMethod -Uri $UvInstallerUrl)
                $env:Path = "$localBin;$env:Path"
                if (-not (Test-Command 'uv')) {
                    throw 'uv was installed but is not on PATH. Open a new PowerShell window and rerun this script.'
                }
            }
            Write-Host "==> Installing $ToolName with uv from: $Src"
            Invoke-Checked 'uv' @('tool', 'install', '--force', $Src)
            $toolBin = "$(Invoke-Native 'uv' @('tool', 'dir', '--bin'))".Trim()
            if (-not $toolBin) { $toolBin = $localBin }
            $exe = Join-Path $toolBin "$ToolName.exe"
            if (-not (Test-Path -LiteralPath $exe)) { $exe = Join-Path $toolBin $ToolName }
            Write-Host '==> Verifying'
            if (-not (Test-Path -LiteralPath $exe)) { throw "$ToolName was not found in $toolBin after install." }
            Invoke-Checked $exe @('--version')
            Save-Platforms $exe
        } finally {
            $env:Path = $origPath
        }
        if (-not (Test-Command $ToolName)) {
            Write-Host ''
            Write-Host "Note: $toolBin may not be on the PATH of new terminals yet."
            Write-Host "Run 'uv tool update-shell' and open a new PowerShell window, or call $exe directly."
        }
        Write-CredentialHelp
        Write-Host ''
        Write-Host 'Done. Try it:'
        Write-Host "  $(Get-FirstRunCommand)"
        Write-Host ''
        Write-Host "Upgrade later: uv tool upgrade $ToolName   (or rerun this script)"
        Write-Host "Uninstall:     uv tool uninstall $ToolName"
    }

    function Invoke-UvFallback([string]$Reason) {
        Write-Host $Reason
        if (Test-Flag $env:BOT_FARM_SILENCE_TRACKER_NO_UV) {
            throw 'BOT_FARM_SILENCE_TRACKER_NO_UV=1 is set, so not falling back to uv. Nothing was installed.'
        }
        if ($base) {
            $wheel = Get-ReleaseWheel
            if ($wheel) {
                Write-Host 'Installing the Python package from the release with uv instead.'
                Install-WithUv $wheel
                return
            }
        }
        # Only a test server can stand in for the repository of an unpublished copy.
        if ($Unpublished -and -not $env:BOT_FARM_SILENCE_TRACKER_GITHUB_URL) { throw $UnpublishedMessage }
        $src = "git+$GitHub/$Repo"
        if ($tag) { $src = "$src@$tag" }
        Write-Host 'Installing from source with uv instead. This needs git (https://git-scm.com/).'
        Install-WithUv $src
    }

    # Add the folder to the user PATH the way rustup and uv do: edit the raw registry value
    # so entries such as %USERPROFILE%\bin stay unexpanded, and keep it a REG_EXPAND_SZ.
    function Add-ToUserPath([string]$Dir) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
        if ($null -eq $key) { $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment') }
        try {
            $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $entries = @($raw -split ';' | Where-Object { $_ })
            $wanted = $Dir.TrimEnd('\')
            foreach ($entry in $entries) {
                if ([Environment]::ExpandEnvironmentVariables($entry).TrimEnd('\') -ieq $wanted) { return $false }
            }
            $key.SetValue('Path', (($entries + $Dir) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
        } finally {
            $key.Close()
        }
        # Tell running programs (such as Explorer) that the environment changed, so new
        # terminals started from them see the new PATH. Failure here is harmless.
        try {
            if (-not ('BotFarmSilenceTrackerInstall.Native' -as [type])) {
                Add-Type -Namespace BotFarmSilenceTrackerInstall -Name Native -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
            }
            $result = [UIntPtr]::Zero
            [void][BotFarmSilenceTrackerInstall.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
        } catch { }
        return $true
    }

    function Test-Attestation([string]$File) {
        # A copy that has not been published has no repository to check the attestation
        # against. The installer only gets here then when a download base override
        # replaces GitHub.
        if ($Unpublished) {
            Write-Warning "This copy has not been published (the repository $Repo still holds the owner placeholder), so the build provenance attestation was not checked; only the SHA256 checksum was verified."
            return
        }
        if ($env:BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL -or -not (Test-Command 'gh')) { return }
        # gh writes to stderr (for example "You are not logged into any GitHub hosts").
        # Under Windows PowerShell 5.1 with 'Stop', redirected native stderr would abort
        # the installer, so this function judges gh by its exit code only.
        $ErrorActionPreference = 'Continue'
        & gh auth status *> $null
        if ($LASTEXITCODE -ne 0) { return }
        Write-Host '==> Verifying build provenance with gh attestation verify'
        $verifyOutput = (& gh attestation verify $File --repo $Repo 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) {
            # A release made while the repository is private has no attestations (they need
            # a public repository or GitHub Enterprise Cloud). That is not a failed check,
            # so warn and carry on; the SHA256 checksum was already verified.
            if ($verifyOutput -match '(?i)no attestations found') {
                Write-Warning "$(Split-Path -Leaf $File) has no build provenance attestation, so only its SHA256 checksum was verified."
                return
            }
            throw "The build provenance attestation for $(Split-Path -Leaf $File) did not verify. Nothing was installed."
        }
        Write-Host '    ok'
    }

    # Chosen first, and before anything is downloaded: a misspelled platform installs
    # nothing, and the menu (only with a console) is answered before the unattended part.
    if ($Platforms) {
        $PlatformList = ConvertTo-PlatformList $Platforms
        $PlatformFrom = 'flag'
    } else {
        $PlatformList = $null
        if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            $PlatformList = Read-PlatformChoice
            $PlatformFrom = 'menu'
        }
        if (-not $PlatformList) {
            $PlatformList = $DefaultPlatform
            $PlatformFrom = 'default'
        }
    }

    if ($Source) {
        Install-WithUv $Source
        return
    }

    $GitHub = $env:BOT_FARM_SILENCE_TRACKER_GITHUB_URL
    if (-not $GitHub) { $GitHub = 'https://github.com' }
    $GitHub = $GitHub.TrimEnd('/')
    Test-Url $GitHub

    # $base is the folder URL of the release files and $tag the release tag; either can be
    # empty (no release yet, or a RELEASE_BASE_URL without a version).
    $tag = $Version
    $base = $null
    if ($env:BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL) {
        $base = $env:BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL.TrimEnd('/')
        Test-Url $base
    } else {
        if (-not $tag) {
            Write-Host '==> Looking up the latest release'
            $tag = Get-LatestTag
            if ($tag) { Write-Host "    $tag" } else { Write-Host '    there is no release yet' }
        }
        if ($tag) { $base = "$GitHub/$Repo/releases/download/$tag" }
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('bfst-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    # The finally block removes the temp folder on success, on errors and on Ctrl-C.
    try {
        # ------------------------------------------------------------ prebuilt binary path
        $arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
        if ($arch -eq 'ARM64') {
            Write-Host 'Windows on ARM: using the x64 program, which Windows runs through emulation.'
        } elseif ($arch -ne 'AMD64') {
            Invoke-UvFallback "No prebuilt program for Windows $arch."
            return
        }
        if (-not $base) {
            Invoke-UvFallback 'There is no release yet, so there is no prebuilt program to download.'
            return
        }

        $asset = "$ToolName-windows-x64.exe"
        $tmpExe = Join-Path $tmp $asset
        Write-Host "==> Downloading $asset"
        Write-Host "    $base/$asset"
        $code = Get-Download "$base/$asset" $tmpExe
        if ($code -eq 404) {
            if ($Version -or $env:BOT_FARM_SILENCE_TRACKER_RELEASE_BASE_URL) {
                throw "$asset was not found at $base (HTTP 404). Check the version tag. Nothing was installed."
            }
            Invoke-UvFallback "Release $tag has no prebuilt program for this system."
            return
        }
        if ($code -ne 200) { throw "Downloading $base/$asset failed with HTTP $code. Nothing was installed." }
        $sums = Join-Path $tmp 'SHA256SUMS.txt'
        $code = Get-Download "$base/SHA256SUMS.txt" $sums
        if ($code -ne 200) { throw "Downloading $base/SHA256SUMS.txt failed with HTTP $code. Nothing was installed." }
        Test-Checksum $tmpExe $asset $sums
        Test-Attestation $tmpExe

        # Lets the script be exercised with PowerShell on macOS or Linux in tests.
        if (-not $OnWindows) { & chmod 755 $tmpExe }
        Write-Host '==> Checking that it runs on this computer'
        Invoke-Native $tmpExe @('--version')
        if ($LASTEXITCODE -ne 0) { throw 'The downloaded program does not run on this computer. Nothing was installed.' }

        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        $exe = Join-Path $InstallDir "$ToolName.exe"
        Copy-Item -Force -LiteralPath $tmpExe -Destination $exe
        Write-Host "==> Installed to $exe"
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
    }

    if (Test-Flag $env:BOT_FARM_SILENCE_TRACKER_NO_MODIFY_PATH) {
        Write-Host "Note: the user PATH was not changed. Run $exe directly, or add $InstallDir to PATH."
    } elseif (-not $OnWindows) {
        Write-Host "Note: not on Windows, so the user PATH was not changed."
    } elseif (Add-ToUserPath $InstallDir) {
        Write-Host "==> Added $InstallDir to your user PATH."
        Write-Host '    Open a new PowerShell window for it to take effect.'
    }

    Save-Platforms $exe
    Write-CredentialHelp
    Write-Host ''
    Write-Host 'Done. Next, try a first run (in a new PowerShell window):'
    Write-Host "  $(Get-FirstRunCommand)"
    Write-Host ''
    Write-Host "Uninstall: delete $InstallDir and remove it from your user PATH."
} @args
