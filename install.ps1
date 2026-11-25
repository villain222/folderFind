#Requires -Version 5.0
<#
.SYNOPSIS
    Installs the "ff" fuzzy folder finder for the current user.

.DESCRIPTION
    Copies ff.py to %LOCALAPPDATA%\folderFind, creates an ff.cmd launcher,
    adds that folder to the user PATH, and appends a small ff function to the
    PowerShell profile so that picking a folder actually changes directory.

.PARAMETER Dest
    Install folder. Defaults to %LOCALAPPDATA%\folderFind.

.PARAMETER NoProfile
    Do not touch the PowerShell profile (ff will print paths but not cd).

.PARAMETER NoPath
    Do not modify the user PATH.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [string]$Dest = (Join-Path $env:LOCALAPPDATA "folderFind"),
    [string]$ProfilePath = $PROFILE.CurrentUserAllHosts,
    [switch]$NoProfile,
    [switch]$NoPath
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Find-Python {
    $candidates = @()
    foreach ($name in @("python.exe", "python3.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($exe) { $candidates += ($exe | Select-Object -Last 1) }
        } catch { }
    }
    foreach ($exe in $candidates) {
        if (-not $exe) { continue }
        $exe = ([string]$exe).Trim()
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        try {
            $ok = & $exe -c "import sys; print(1 if sys.version_info >= (3, 8) else 0)" 2>$null
            if (($ok | Select-Object -Last 1) -eq "1") { return $exe }
        } catch { }
    }
    return $null
}

$source = Join-Path $PSScriptRoot "ff.py"
if (-not (Test-Path -LiteralPath $source)) {
    throw "ff.py not found next to install.ps1 (expected $source)"
}

$python = Find-Python
if (-not $python) {
    throw "Python 3.8 or newer was not found. Install it from https://www.python.org/downloads/ and try again."
}
Write-Host "Using Python: $python"

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$target = Join-Path $Dest "ff.py"
Copy-Item -LiteralPath $source -Destination $target -Force
Write-Host "Copied ff.py to $Dest"

# ff.cmd lets both cmd.exe and PowerShell find the command on PATH.
$shim = Join-Path $Dest "ff.cmd"
$shimText = "@echo off`r`n`"$python`" `"$target`" %*`r`n"
[System.IO.File]::WriteAllText($shim, $shimText, $utf8NoBom)
Write-Host "Created launcher $shim"

if (-not $NoPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $already = $false
    if ($userPath) {
        foreach ($part in ($userPath -split ";")) {
            if ($part.Trim().TrimEnd("\") -ieq $Dest.TrimEnd("\")) { $already = $true }
        }
    }
    if (-not $already) {
        $newPath = $Dest
        if ($userPath) { $newPath = $userPath.TrimEnd(";") + ";" + $Dest }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "Added $Dest to the user PATH (new terminals only)."
    }
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $env:Path = $machinePath + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not $NoProfile) {
    $profileDir = Split-Path -Parent $ProfilePath
    if ($profileDir -and -not (Test-Path -LiteralPath $profileDir)) {
        New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    }

    $env:FOLDERFIND_LAUNCHER = $shim
    try {
        $snippet = (& $python $target --init powershell) -join "`r`n"
    } finally {
        Remove-Item Env:FOLDERFIND_LAUNCHER -ErrorAction SilentlyContinue
    }

    $existing = ""
    if (Test-Path -LiteralPath $ProfilePath) {
        $existing = Get-Content -LiteralPath $ProfilePath -Raw -ErrorAction SilentlyContinue
        if ($null -eq $existing) { $existing = "" }
    }
    $pattern = "(?s)\r?\n?# >>> folderfind >>>.*?# <<< folderfind <<<\r?\n?"
    $existing = [regex]::Replace($existing, $pattern, "")
    $block = "# >>> folderfind >>>`r`n" + $snippet + "`r`n# <<< folderfind <<<`r`n"
    if ($existing.Trim().Length -gt 0) {
        $newContent = $existing.TrimEnd() + "`r`n`r`n" + $block
    } else {
        $newContent = $block
    }
    Set-Content -LiteralPath $ProfilePath -Value $newContent -Encoding Default -NoNewline
    Write-Host "Added the ff function to $ProfilePath"
}

Write-Host ""
& $shim --version
Write-Host ""
Write-Host "Installed. Next steps:" -ForegroundColor Green
Write-Host "  1. Open a new terminal (or run: . `"$ProfilePath`")"
Write-Host "  2. Run: ff -u          # build the folder index"
Write-Host "  3. Run: ff ai proj     # pick a folder and cd into it"
