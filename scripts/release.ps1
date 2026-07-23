# Release helper: read version from pyproject.toml, tag, and push.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/release.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/release.ps1 -DryRun

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

Push-Location $Root
try {
    # Read version from pyproject.toml
    $pyproject = Get-Content pyproject.toml -Raw
    if ($pyproject -notmatch 'version\s*=\s*"([^"]+)"') {
        throw "Cannot find version in pyproject.toml"
    }
    $version = $Matches[1]
    $tag = "v$version"

    # Check tag doesn't already exist
    $existing = git tag -l $tag
    if ($existing) {
        throw "Tag $tag already exists locally. Delete it first: git tag -d $tag"
    }

    # Verify we're on main and clean
    $branch = git branch --show-current
    if ($branch -ne "main") {
        Write-Warning "Not on 'main' branch (current: $branch). Press Enter to continue or Ctrl+C to abort."
        $null = Read-Host
    }

    Write-Host "Version : $version"
    Write-Host "Tag     : $tag"
    Write-Host ""

    if ($DryRun) {
        Write-Host "[DryRun] Would run: git tag $tag && git push origin $tag"
        exit 0
    }

    git tag $tag
    git push origin $tag

    Write-Host ""
    Write-Host "Tag $tag pushed. Release workflow will build and publish at:"
    Write-Host "  https://github.com/alex8224/synapse-agent/releases"
} finally {
    Pop-Location
}
