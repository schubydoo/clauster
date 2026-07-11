#Requires -Version 5.1
<#
.SYNOPSIS
  Clauster uninstaller (Windows, PowerShell) - the counterpart to install.ps1 (#816).

    irm https://raw.githubusercontent.com/schubydoo/clauster/main/uninstall.ps1 | iex

  Auto-detects how clauster was installed (the standalone clauster.exe from
  install.ps1, or a `uv tool` / `pipx` / `pip` / `scoop` package), removes the right
  artifact, removes the install dir from the user PATH, and removes the state
  directory (clauster.db, state.json, hosted_state.json, tls\, backups\, sockets,
  logs) and the config yaml.

  Safe by construction: -DryRun prints what would be removed and changes nothing; a
  confirmation prompt guards any deletion (skip with -Yes); -KeepConfig / -KeepData
  move clauster.yml / clauster.db aside to a printed backup path; it never deletes a
  path outside the known clauster locations and refuses a state dir that resolves to
  the user profile or a drive root; and it fails closed (reports, exits non-zero)
  rather than guessing when no install can be identified.

  Environment overrides (mirror install.ps1 + the app's resolution):
    CLAUSTER_INSTALL_DIR   where clauster.exe lives; default: %LOCALAPPDATA%\Programs\clauster
    CLAUSTER_STATE_DIR     state directory; default: read from config, else ~\.clauster
    CLAUSTER_CONFIG        config path; default: the app's search order
#>

param(
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$KeepConfig,
    [switch]$KeepData,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Write-Info($m) { Write-Host "[INFO]  $m" -ForegroundColor Blue }
function Write-Ok($m)   { Write-Host "[ OK ]  $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "[WARN]  $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[ERR ]  $m" -ForegroundColor Red }

function Show-Usage {
    Write-Host @'
Usage: uninstall.ps1 [-DryRun] [-Yes] [-KeepConfig] [-KeepData] [-Help]

  -DryRun       Show what would be removed without removing anything.
  -Yes          Do not prompt for confirmation.
  -KeepConfig   Preserve clauster.yml (moved aside to a printed backup path).
  -KeepData     Preserve clauster.db (moved aside to a printed backup path).
  -Help         Show this help.
'@
}

# --- Resolution ------------------------------------------------------------

function Resolve-ConfigPath {
    if ($env:CLAUSTER_CONFIG) {
        # Expand a leading ~ the way the app (Path.expanduser) does — -LiteralPath does not.
        $envCfg = Expand-Home $env:CLAUSTER_CONFIG
        if (Test-Path -LiteralPath $envCfg -PathType Leaf) { return (Resolve-Path -LiteralPath $envCfg).Path }
    }
    if (Test-Path -LiteralPath '.\clauster.yml' -PathType Leaf) {
        return (Resolve-Path -LiteralPath '.\clauster.yml').Path
    }
    if ($env:CLAUSTER_HOME) {
        $homeCfg = Join-Path (Expand-Home $env:CLAUSTER_HOME) 'clauster.yml'
        if (Test-Path -LiteralPath $homeCfg -PathType Leaf) { return $homeCfg }
    }
    return $null
}

function Expand-Home([string]$p) {
    if ($p -eq '~') { return $env:USERPROFILE }
    if ($p -like '~[/\]*') { return (Join-Path $env:USERPROFILE $p.Substring(2)) }
    return $p
}

function Resolve-StateDir {
    if ($env:CLAUSTER_STATE_DIR) { return (Expand-Home $env:CLAUSTER_STATE_DIR) }
    $cfg = Resolve-ConfigPath
    if ($cfg) {
        foreach ($line in Get-Content -LiteralPath $cfg) {
            if ($line -match '^\s*state_dir:\s*(.+?)\s*(#.*)?$') {
                $val = $Matches[1].Trim().Trim('"').Trim("'")
                if ($val) { return (Expand-Home $val) }
            }
        }
    }
    return (Join-Path $env:USERPROFILE '.clauster')
}

# Guard: refuse a state dir that is the user profile or a bare drive root.
function Test-StateDirSafe([string]$d) {
    if ([string]::IsNullOrWhiteSpace($d)) { return $false }
    $full = [System.IO.Path]::GetFullPath($d).TrimEnd('\')
    $profile = $env:USERPROFILE.TrimEnd('\')
    if ($full -eq $profile) { return $false }
    # A drive root like "C:\" has no parent directory.
    if ($null -eq [System.IO.Path]::GetDirectoryName($full)) { return $false }
    return $true
}

function Test-CommandHasClauster([string]$exe, [string[]]$listArgs, [string]$pattern) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    try { $out = & $exe @listArgs 2>$null } catch { return $false }
    return (($out | Out-String) -match $pattern)
}

function Uninstall-Clauster {
    if ($Help) { Show-Usage; $global:LASTEXITCODE = 0; return }

    $installDir = if ($env:CLAUSTER_INSTALL_DIR) { $env:CLAUSTER_INSTALL_DIR }
                  else { Join-Path $env:LOCALAPPDATA 'Programs\clauster' }
    $binaryPath = Join-Path $installDir 'clauster.exe'
    $stateDir   = Resolve-StateDir
    $configPath = Resolve-ConfigPath

    $script:RemovalFailed = $false  # set true by Remove-Target on any removal error

    # --- Detection: collect every install method present -------------------
    $detected = New-Object System.Collections.Generic.List[string]
    $isUv    = Test-CommandHasClauster 'uv'    @('tool','list')    '(?im)^clauster\b'
    $isPipx  = Test-CommandHasClauster 'pipx'  @('list','--short') '(?im)(^|\s)clauster(\s|$)'
    $isScoop = Test-CommandHasClauster 'scoop' @('list')           '(?im)(^|\s)clauster(\s|$)'
    $pip = if (Get-Command 'pip' -ErrorAction SilentlyContinue) { 'pip' }
           elseif (Get-Command 'pip3' -ErrorAction SilentlyContinue) { 'pip3' } else { $null }
    $isPip = $false
    if ($pip) { & $pip show clauster *> $null; $isPip = ($LASTEXITCODE -eq 0) }
    # Detected independently of the package managers so a stale standalone exe that
    # coexists with a uv/pipx/scoop install is still removed (its removal runs after the
    # package uninstalls, so a shim already gone is a harmless no-op).
    $isBinary = (Test-Path -LiteralPath $binaryPath)

    if ($isUv)    { $detected.Add('uv tool') }
    if ($isPipx)  { $detected.Add('pipx') }
    if ($isScoop) { $detected.Add('scoop') }
    if ($isPip)   { $detected.Add('pip') }
    if ($isBinary){ $detected.Add("binary:$binaryPath") }

    # --- Plan --------------------------------------------------------------
    $dryLabel = if ($DryRun) { ' (dry run)' } else { '' }
    Write-Info "Clauster uninstaller$dryLabel"
    Write-Host ''
    Write-Info ("Detected install method(s): " + $(if ($detected.Count) { $detected -join ', ' } else { 'none' }))
    Write-Info ("State directory:            $stateDir" + $(if (Test-Path -LiteralPath $stateDir) { '' } else { ' (absent)' }))
    Write-Info ("Config file:                " + $(if ($configPath) { $configPath } else { '<none found>' }))
    Write-Host ''

    $stateExists  = Test-Path -LiteralPath $stateDir
    $configExists = $configPath -and (Test-Path -LiteralPath $configPath)
    if ($detected.Count -eq 0 -and -not $stateExists -and -not $configExists) {
        throw 'No clauster install found (no binary/package, state dir, or config). Nothing to do.'
    }
    # Fail closed: leftover files with no identifiable install method are ambiguous.
    if ($detected.Count -eq 0) {
        Write-Warn 'Could not identify how clauster was installed (no binary/package found).'
        Write-Warn 'Leaving files in place. If you know they are clauster''s, remove them manually:'
        if ($stateExists)  { Write-Warn "  Remove-Item -Recurse -Force '$stateDir'" }
        if ($configExists) { Write-Warn "  Remove-Item -Force '$configPath'" }
        throw 'Refusing to guess-and-delete.'
    }

    # --- Confirm -----------------------------------------------------------
    if (-not $DryRun -and -not $Yes) {
        $reply = Read-Host 'Proceed with removal? [y/N]'
        if ($reply -notmatch '^(y|yes)$') { Write-Info 'Aborted.'; $global:LASTEXITCODE = 0; return }
    }

    function Remove-Target([string]$path, [switch]$Recurse) {
        if (-not (Test-Path -LiteralPath $path)) { return }
        if ($DryRun) { Write-Host "  would remove: $path"; return }
        # Fail LOUD, never silent: a swallowed error (e.g. a locked file) would leave
        # state — incl. session.secret / session.epoch auth material — behind while the
        # script still reports success. Surface it + flag the run so the summary warns.
        try { Remove-Item -LiteralPath $path -Force -Recurse:$Recurse -ErrorAction Stop }
        catch {
            Write-Warn "Could not remove $path - remove it manually: $($_.Exception.Message)"
            $script:RemovalFailed = $true
        }
    }
    function Invoke-Step([string]$desc, [scriptblock]$action) {
        if ($DryRun) { Write-Host "  would: $desc" } else { & $action }
    }

    # --- 1) Package / binary, per detected method --------------------------
    foreach ($m in $detected) {
        switch -Wildcard ($m) {
            'uv tool' { Write-Info 'Removing uv tool...';   Invoke-Step 'uv tool uninstall clauster'   { & uv tool uninstall clauster } }
            'pipx'    { Write-Info 'Removing pipx package...'; Invoke-Step 'pipx uninstall clauster'    { & pipx uninstall clauster } }
            'scoop'   { Write-Info 'Removing scoop package...'; Invoke-Step 'scoop uninstall clauster'  { & scoop uninstall clauster } }
            'pip'     { Write-Info 'Removing pip package...'; Invoke-Step "$pip uninstall -y clauster"  { & $pip uninstall -y clauster } }
            'binary:*' {
                Write-Info 'Removing binary...'
                Remove-Target ($m -replace '^binary:', '')
                # Drop the install dir from the user PATH (mirrors install.ps1's add).
                $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
                if ($userPath -and (($userPath -split ';') -contains $installDir)) {
                    $newPath = (($userPath -split ';') | Where-Object { $_ -and $_ -ne $installDir }) -join ';'
                    Invoke-Step "remove '$installDir' from user PATH" { [Environment]::SetEnvironmentVariable('Path', $newPath, 'User') }
                }
                # Remove the now-empty install dir.
                if ((Test-Path -LiteralPath $installDir) -and -not (Get-ChildItem -LiteralPath $installDir -Force -ErrorAction SilentlyContinue)) {
                    Remove-Target $installDir -Recurse
                }
            }
        }
    }

    # --- 2) Preserve config / data on request ------------------------------
    $backupDir = Join-Path $env:USERPROFILE 'clauster-uninstall-backup'
    function Save-Aside([string]$src, [string]$label) {
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { return }
        if ($DryRun) { Write-Host "  would keep ${label}: move $src -> $backupDir\"; return }
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        Move-Item -LiteralPath $src -Destination $backupDir -Force
        Write-Ok "Kept ${label}: $backupDir\$(Split-Path -Leaf $src)"
    }
    if ($KeepConfig -and $configExists) { Save-Aside $configPath 'config' }
    if ($KeepData) { Save-Aside (Join-Path $stateDir 'clauster.db') 'database' }

    # --- 3) State directory (guarded) --------------------------------------
    if ($stateExists) {
        if (Test-StateDirSafe $stateDir) {
            # Removed WHOLE in both modes: with -KeepData the DB was already moved to the
            # backup above, so session.secret / session.epoch and everything else go too
            # (a selective child list would strand exactly that auth material).
            Write-Info 'Removing state directory...'
            Remove-Target $stateDir -Recurse
        }
        else { Write-Warn "Refusing to remove an unsafe state_dir path: $stateDir" }
    }

    # --- 4) Config yaml (unless kept / already moved aside) ----------------
    if (-not $KeepConfig -and $configExists -and (Test-Path -LiteralPath $configPath)) {
        Write-Info 'Removing config file...'
        Remove-Target $configPath
    }

    Write-Host ''
    if ($DryRun) {
        Write-Ok 'Dry run complete - nothing was removed.'
    }
    elseif ($script:RemovalFailed) {
        # Something couldn't be removed (warned above) — do NOT report a clean success,
        # and exit non-zero so a caller/CI sees the partial cleanup.
        Write-Warn 'Clauster uninstall finished with errors - some files remain (see warnings above).'
        $global:LASTEXITCODE = 1
        return
    }
    else {
        Write-Ok 'Clauster uninstalled.'
        Write-Warn "Claude Code (the 'claude' CLI) was installed separately and is left untouched."
    }
    $global:LASTEXITCODE = 0
}

# Run via a function + try/catch (no `exit`, which would terminate the host when
# this script is piped into `iex`).
try {
    Uninstall-Clauster
}
catch {
    Write-Err $_.Exception.Message
    $global:LASTEXITCODE = 1
}
