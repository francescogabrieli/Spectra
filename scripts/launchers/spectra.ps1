param(
    [Parameter(Position = 0)]
    [string]$Command,

    [int]$Port = 8080,
    [switch]$Build,
    [switch]$NoOpen,
    [switch]$NoUpdate
)

$ErrorActionPreference = "Stop"

function Show-Usage {
    Write-Host @"
Usage:
  .\spectra.ps1 start [-Port 8080] [-Build] [-NoOpen] [-NoUpdate]
  .\spectra.ps1 stop
  .\spectra.ps1 logs
  .\spectra.ps1 status

Commands:
  start   Auto-update if safe, then start Spectra with Docker Compose in background
  stop    Stop Spectra containers
  logs    Follow Spectra logs
  status  Show container status
"@
}

function Test-DockerReady {
    try {
        docker info *> $null
        return $true
    } catch {
        return $false
    }
}

function Require-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Error "docker command not found. Install Docker Desktop first."
    }

    if (Test-DockerReady) {
        return
    }

    Write-Host "Docker daemon not running. Trying to start Docker Desktop..."

    $candidates = @(
        "$Env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$Env:ProgramFiles(x86)\Docker\Docker\Docker Desktop.exe",
        "$Env:LocalAppData\Programs\Docker\Docker\Docker Desktop.exe"
    )

    foreach ($exe in $candidates) {
        if (Test-Path $exe) {
            Start-Process -FilePath $exe | Out-Null
            break
        }
    }

    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if (Test-DockerReady) {
            Write-Host "Docker Desktop is ready."
            return
        }
    }

    Write-Error "Docker daemon is not running. Start Docker Desktop and retry."
}

function Invoke-SafeAutoUpdate {
    if ($NoUpdate -or $Env:SPECTRA_AUTO_UPDATE -eq "0") {
        return $false
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        return $false
    }

    try {
        git rev-parse --is-inside-work-tree *> $null
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
    } catch {
        return $false
    }

    git diff --quiet --ignore-submodules --
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Local git changes detected. Skipping auto-update."
        return $false
    }

    git diff --cached --quiet --ignore-submodules --
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Local git changes detected. Skipping auto-update."
        return $false
    }

    $branch = (git branch --show-current 2>$null).Trim()
    if (-not $branch) {
        return $false
    }

    $upstream = (git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null).Trim()
    if (-not $upstream) {
        return $false
    }

    $remote = ($upstream -split '/')[0]
    if (-not $remote) {
        return $false
    }

    git fetch --quiet $remote
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Could not reach $remote. Skipping auto-update."
        return $false
    }

    $counts = (git rev-list --left-right --count "HEAD...$upstream" 2>$null).Trim()
    if (-not $counts) {
        return $false
    }

    $parts = $counts -split '\s+'
    $ahead = [int]$parts[0]
    $behind = [int]$parts[1]

    if ($ahead -gt 0 -and $behind -gt 0) {
        Write-Host "Local branch has diverged from $upstream. Skipping auto-update."
        return $false
    }

    if ($behind -le 0) {
        return $false
    }

    Write-Host "Updating Spectra from $upstream..."
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Auto-update failed. Continuing with local checkout."
        return $false
    }

    return $true
}

if (-not $Command) {
    Show-Usage
    exit 1
}

Set-Location -Path $PSScriptRoot

switch ($Command.ToLowerInvariant()) {
    "start" {
        Require-Docker
        if (Invoke-SafeAutoUpdate) {
            $Build = $true
        }
        $args = @("compose", "up", "-d")
        if ($Build) {
            $args += "--build"
        }
        $Env:SPECTRA_PORT = "$Port"
        try {
            & docker @args
        } finally {
            Remove-Item Env:SPECTRA_PORT -ErrorAction SilentlyContinue
        }

        $url = "http://localhost:$Port"
        Write-Host "Spectra is starting on $url"
        if (-not $NoOpen) {
            Start-Process $url | Out-Null
            Write-Host "Browser opened."
        }
    }

    "stop" {
        Require-Docker
        docker compose down
    }

    "logs" {
        Require-Docker
        docker compose logs -f
    }

    "status" {
        Require-Docker
        docker compose ps
    }

    "help" {
        Show-Usage
    }

    default {
        Write-Error "Unknown command: $Command"
    }
}
