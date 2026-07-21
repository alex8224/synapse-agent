# Install coding-agent as a user-level console tool (PATH entry).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Editable
#   powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Uninstall

param(
    [switch]$Editable = $true,
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Require-Uv {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv not found. Install: https://docs.astral.sh/uv/getting-started/installation/"
    }
}

Require-Uv

if ($Uninstall) {
    Write-Host "uv tool uninstall synapse"
    uv tool uninstall synapse
    exit $LASTEXITCODE
}

Push-Location $Root
try {
    $args = @("tool", "install")
    if ($Editable) {
        $args += "--editable"
    }
    if ($Force) {
        $args += "--force"
    }
    $args += "."
    Write-Host ("uv " + ($args -join " "))
    & uv @args
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Installed. Verify:"
Write-Host "  coding-agent version"
Write-Host "  coding-agent tui -w ."
Write-Host ""
$tools = & uv tool dir 2>$null
if ($tools) {
    Write-Host "uv tools dir: $tools"
}
