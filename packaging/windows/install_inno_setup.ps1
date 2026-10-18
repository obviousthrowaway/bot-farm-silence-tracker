# Download Inno Setup (pinned version), check its SHA256 checksum, and install it for the
# current user into a folder of your choice. Prints the path of ISCC.exe, the compiler.
#
#   pwsh packaging/windows/install_inno_setup.ps1 -Destination C:\path\to\innosetup
#
# The checksum is the one GitHub lists for the release file on the official Inno Setup
# repository (jrsoftware/issrc). To update, change both lines below together.
param(
    [Parameter(Mandatory = $true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Version = '6.7.3'
$Sha256 = '9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732'
$Url = "https://github.com/jrsoftware/issrc/releases/download/is-$($Version.Replace('.', '_'))/innosetup-$Version.exe"

$download = Join-Path ([IO.Path]::GetTempPath()) "innosetup-$Version.exe"
Write-Host "Downloading $Url"
Invoke-WebRequest -Uri $Url -OutFile $download -UseBasicParsing
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $download).Hash.ToLower()
if ($actual -ne $Sha256) {
    throw "Checksum mismatch for innosetup-$Version.exe: expected $Sha256, got $actual"
}
Write-Host "SHA256 ok: $actual"

$process = Start-Process -FilePath $download -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/CURRENTUSER', "/DIR=$Destination"
)
if ($process.ExitCode -ne 0) { throw "The Inno Setup installer failed with exit code $($process.ExitCode)" }
$iscc = Join-Path $Destination 'ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) { throw "ISCC.exe was not found in $Destination" }
Write-Output $iscc
