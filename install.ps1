#Requires -Version 5.1
<#
.SYNOPSIS
  Clauster installer (Windows, PowerShell).

    irm https://raw.githubusercontent.com/schubydoo/clauster/main/install.ps1 | iex

  Downloads the signed standalone clauster.exe from the latest GitHub release,
  verifies its SHA-256 against the release's SHA256SUMS, installs it under
  %LOCALAPPDATA%\Programs\clauster, and adds that directory to the user PATH.
  No Python required.

  Clauster spawns the `claude` CLI but does not vendor it - install Claude Code
  separately and keep it on PATH.

  Environment overrides:
    CLAUSTER_VERSION       pin a version (e.g. 0.10.0); default: latest release
    CLAUSTER_INSTALL_DIR   install directory; default: %LOCALAPPDATA%\Programs\clauster
#>

$ErrorActionPreference = 'Stop'
# PowerShell 5.1 on older Windows can default to TLS 1.0, which GitHub rejects.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Owner = 'schubydoo'
$Repo  = 'clauster'

function Write-Info($m) { Write-Host "[INFO]  $m" -ForegroundColor Blue }
function Write-Ok($m)   { Write-Host "[ OK ]  $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "[WARN]  $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[ERR ]  $m" -ForegroundColor Red }

function Show-Fallback {
    Write-Host ''
    Write-Host 'No standalone binary was installed. Try one of these instead:'
    Write-Host "  scoop bucket add clauster https://github.com/$Owner/$Repo; scoop install clauster"
    Write-Host '  uv tool install clauster        # https://docs.astral.sh/uv/'
    Write-Host '  pipx install clauster'
    Write-Host '  pip install clauster'
    Write-Host "Full install guide: https://schubydoo.github.io/$Repo/installation/"
}

function Install-Clauster {
    Write-Info 'Installing clauster'

    # Windows publishes only the x86_64 binary; Windows-on-ARM runs it under x64
    # emulation, so this is the right target on both AMD64 and ARM64.
    $target = 'windows-x86_64'

    $ver = $env:CLAUSTER_VERSION
    if ([string]::IsNullOrWhiteSpace($ver)) {
        Write-Info 'Resolving latest release...'
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$Repo/releases/latest" `
            -Headers @{ 'User-Agent' = 'clauster-install'; 'Accept' = 'application/vnd.github+json' }
        $ver = $rel.tag_name -replace '^v', ''
    }
    if ([string]::IsNullOrWhiteSpace($ver)) { throw 'Could not resolve the latest release version.' }
    Write-Info "Arch: $env:PROCESSOR_ARCHITECTURE | Version: $ver"

    $asset = "clauster-$ver-$target.exe"
    $base  = "https://github.com/$Owner/$Repo/releases/download/v$ver"
    $work  = Join-Path ([System.IO.Path]::GetTempPath()) ('clauster-' + [System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $work -Force | Out-Null

    try {
        # SHA256SUMS is the authoritative list of published binaries - gate on it so
        # an arch with no binary falls back cleanly instead of 404-ing on download.
        $sumsPath = Join-Path $work 'SHA256SUMS'
        Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile $sumsPath -UseBasicParsing
        # Exact field-2 (filename) match, mirroring install.sh's `awk '$2 == a'`, so a
        # sidecar line that merely *contains* the asset name can't supply a wrong hash.
        # sha256sum text mode writes "<hash>  <name>" (two spaces, no '*' marker).
        $expected = ''
        foreach ($l in Get-Content -LiteralPath $sumsPath) {
            $parts = $l -split '\s+', 2
            # TrimStart('*') tolerates a binary-mode marker ("<hash> *<name>"), matching
            # bump_packaging.py; our releases use text mode, so this is defensive parity.
            $name = if ($parts.Count -eq 2) { $parts[1].Trim().TrimStart('*') } else { '' }
            if ($parts.Count -eq 2 -and $name -eq $asset) {
                $expected = $parts[0].ToLower()
                break
            }
        }
        if ([string]::IsNullOrEmpty($expected)) {
            Write-Err "Release v$ver has no $target binary ($asset not in SHA256SUMS)."
            Show-Fallback
            # Signal failure to callers/CI without `exit` (which would kill an iex host).
            $global:LASTEXITCODE = 1
            return
        }

        Write-Info "Downloading $asset..."
        $exePath = Join-Path $work $asset
        Invoke-WebRequest -Uri "$base/$asset" -OutFile $exePath -UseBasicParsing

        Write-Info 'Verifying checksum...'
        $actual = (Get-FileHash -Algorithm SHA256 -Path $exePath).Hash.ToLower()
        if ($actual -ne $expected) {
            throw "Checksum mismatch for ${asset}: expected $expected, got $actual."
        }
        Write-Ok ('Checksum verified (sha256: {0}...)' -f $actual.Substring(0, 12))

        $dir = $env:CLAUSTER_INSTALL_DIR
        if ([string]::IsNullOrWhiteSpace($dir)) {
            $dir = Join-Path $env:LOCALAPPDATA 'Programs\clauster'
        }
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        $dest = Join-Path $dir 'clauster.exe'
        Move-Item -Path $exePath -Destination $dest -Force
        # The download carries a mark-of-the-web zone tag; clear it now that the
        # checksum is verified so the user isn't blocked on first run.
        Unblock-File -Path $dest -ErrorAction SilentlyContinue
        Write-Ok "Installed $dest"

        # Add the install dir to the user PATH if it isn't already there.
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if ($null -eq $userPath) { $userPath = '' }
        if (($userPath -split ';') -notcontains $dir) {
            $newPath = if ([string]::IsNullOrEmpty($userPath)) { $dir } else { "$userPath;$dir" }
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Write-Warn "Added $dir to your user PATH - open a new terminal for it to take effect."
        }

        # Verify: require a zero exit AND a 'clauster' identity banner. A launch
        # failure (bad image, missing DLL) degrades to a warning, not a hard error.
        $banner = ''
        $confirmed = $false
        try {
            $banner = & $dest --version 2>$null
            if ($LASTEXITCODE -eq 0 -and "$banner" -match '^clauster') { $confirmed = $true }
        }
        catch { }
        if ($confirmed) {
            Write-Ok "$banner installed"
        }
        else {
            Write-Warn "Installed to $dest, but '$dest --version' did not confirm a clauster binary."
        }

        Write-Info "Clauster spawns the 'claude' CLI but does not vendor it - make sure Claude Code is on your PATH."
        Write-Info 'The binary is Sigstore-signed but not authenticode-signed, so SmartScreen may warn on first run.'
        Write-Ok 'Installation complete!'
        # Only reached on success. The `--version` probe above can leave $LASTEXITCODE
        # non-zero without throwing; normalize so a successful install reports 0 to a
        # piped `iex` caller / CI. (Failure paths return/throw before here with 1.)
        $global:LASTEXITCODE = 0
    }
    finally {
        Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Run via a function + try/catch (no `exit`, which would terminate the host shell
# when this script is piped into `iex`).
try {
    Install-Clauster
}
catch {
    Write-Err $_.Exception.Message
    # Signal failure to callers/CI without `exit` (which would kill an iex host).
    $global:LASTEXITCODE = 1
}
