[CmdletBinding()]
param(
    [switch]$Deploy,
    [switch]$RestartMachine,
    [switch]$PortAlreadyPublished,
    [switch]$MountWorkspace,
    [string]$Smolvm = "$env:LOCALAPPDATA\Programs\smolvm\smolvm.exe",
    [string]$Machine = 'agy-box',
    [ValidateRange(1024, 65535)][int]$Port = 19091,
    [string]$HostRoot = (Resolve-Path "$PSScriptRoot\..\..").Path,
    [string]$GuestRoot = '/workspace',
    [string]$Agent = '/root/.local/bin/agy'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Program" }
}

function Quote-Sh([string]$Value) {
    # POSIX single-quote escaping without evaluating any user-supplied shell text.
    return "'" + $Value.Replace("'", "'" + '"' + "'" + '"' + "'") + "'"
}

$HostRoot = (Resolve-Path -LiteralPath $HostRoot).Path
if (-not $GuestRoot.StartsWith('/') -or -not $Agent.StartsWith('/')) {
    throw 'GuestRoot and Agent must be absolute POSIX paths.'
}
$out = Join-Path $PSScriptRoot '../../.tmp/acp-tcp-bridge'
$null = New-Item -ItemType Directory -Force -Path $out
$out = (Resolve-Path $out).Path
$client = Join-Path $out 'acp-tcp-bridge.exe'
$server = Join-Path $out 'acp-tcp-bridge-linux-amd64'
$oldGOOS = $env:GOOS
$oldGOARCH = $env:GOARCH
$oldCGO = $env:CGO_ENABLED
Push-Location $PSScriptRoot
try {
    $env:CGO_ENABLED = '0'
    $env:GOARCH = 'amd64'
    $env:GOOS = 'windows'
    Invoke-Checked 'go' @('build', '-trimpath', '-o', $client, '.')
    $env:GOOS = 'linux'
    Invoke-Checked 'go' @('build', '-trimpath', '-o', $server, '.')
} finally {
    $env:GOOS = $oldGOOS
    $env:GOARCH = $oldGOARCH
    $env:CGO_ENABLED = $oldCGO
    Pop-Location
}
Write-Host "Built Windows client and static Linux/amd64 server in $out"
if (-not $Deploy) { return }

if (-not $PortAlreadyPublished -and -not $RestartMachine) {
    throw 'Publishing a new port requires restarting the VM. Re-run with -Deploy -RestartMachine, or use -PortAlreadyPublished only if the port is already mapped to loopback.'
}
if ($RestartMachine -and $PortAlreadyPublished) {
    throw 'Choose either -RestartMachine or -PortAlreadyPublished, not both.'
}
if ($MountWorkspace -and -not $RestartMachine) {
    throw 'MountWorkspace requires an explicitly authorized RestartMachine.'
}
# Validate the intended guest paths before any disruptive restart.
$check = 'test -x ' + (Quote-Sh $Agent)
if (-not $MountWorkspace) { $check += ' && test -d ' + (Quote-Sh $GuestRoot) }
Invoke-Checked $Smolvm @('machine', 'exec', '--name', $Machine, '--', '/bin/sh', '-c', $check)
if ($RestartMachine -and (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    throw "Host port $Port is already listening; choose a free port or use the existing mapping explicitly."
}
if ($RestartMachine) {
    Write-Host "Explicitly authorized restart: $Machine; adding loopback TCP mapping ${Port}:${Port}."
    Invoke-Checked $Smolvm @('machine', 'stop', '--name', $Machine)
    Invoke-Checked $Smolvm @('machine', 'update', '--name', $Machine, '--port', "${Port}:${Port}")
    if ($MountWorkspace) {
        Invoke-Checked $Smolvm @('machine', 'update', '--name', $Machine, '--volume', "${HostRoot}:${GuestRoot}:rw")
    }
    Invoke-Checked $Smolvm @('machine', 'start', '--name', $Machine)
}

# Tokens never appear in command arguments, stdout, ACP messages, or tracked files.
# Protect the host directory before creating the secret.
$acl = Get-Acl -LiteralPath $out
$acl.SetAccessRuleProtection($true, $false)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$rule = [Security.AccessControl.FileSystemAccessRule]::new(
    $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
)
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $out -AclObject $acl
$tokenFile = Join-Path $out 'token'
if (-not (Test-Path -LiteralPath $tokenFile)) {
    $token = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    [IO.File]::WriteAllText($tokenFile, $token)
    Remove-Variable token
}
$guestDir = "/root/.local/share/synapse-acp-bridge-$Port"
$guestBinary = "$guestDir/acp-tcp-bridge"
$guestToken = "$guestDir/token"
$guestLog = "$guestDir/server.log"
$guestPid = "$guestDir/server.pid"

# Fail closed rather than overwrite a running bridge or rotate its credentials.
$prepare = 'set -eu; d=' + (Quote-Sh $guestDir) + '; mkdir -p "$d"; chmod 700 "$d"; ' +
    'if test -f "$d/server.pid" && kill -0 "$(cat "$d/server.pid")" 2>/dev/null; then ' +
    'echo "Bridge already running; stop its recorded PID before redeploying" >&2; exit 1; fi; ' +
    'test -d ' + (Quote-Sh $GuestRoot) + '; test -x ' + (Quote-Sh $Agent)
Invoke-Checked $Smolvm @('machine', 'exec', '--name', $Machine, '--', '/bin/sh', '-c', $prepare)
Invoke-Checked $Smolvm @('machine', 'cp', '--mode', '700', $server, "${Machine}:$guestBinary")
Invoke-Checked $Smolvm @('machine', 'cp', '--mode', '600', $tokenFile, "${Machine}:$guestToken")
$launch = 'umask 077; echo $$ > ' + (Quote-Sh $guestPid) + '; exec ' + (Quote-Sh $guestBinary) +
    ' serve --listen ' + (Quote-Sh "0.0.0.0:$Port") + ' --allow-remote --token-file ' +
    (Quote-Sh $guestToken) + ' --workdir ' + (Quote-Sh $GuestRoot) + ' -- ' +
    (Quote-Sh $Agent) + ' acp --dangerously-skip-permissions > ' + (Quote-Sh $guestLog) + ' 2>&1'
Invoke-Checked $Smolvm @('machine', 'exec', '--name', $Machine, '--detach', '--', '/bin/sh', '-c', $launch)

# Wait for listener publication without relying on a fixed startup sleep.
$published = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $socket = [Net.Sockets.TcpClient]::new()
    try {
        $task = $socket.ConnectAsync('127.0.0.1', $Port)
        if ($task.Wait(200) -and $socket.Connected) { $published = $true; break }
    } catch { } finally { $socket.Dispose() }
    Start-Sleep -Milliseconds 100
}
if (-not $published) { throw 'Bridge port did not become reachable; check guest server.log and port publishing.' }

# Real initialize smoke test; do not create an agent session or call its cloud models.
$probe = @{ jsonrpc = '2.0'; id = 1; method = 'initialize'; params = @{
    protocolVersion = 1; clientCapabilities = @{};
    clientInfo = @{ name = 'synapse-bridge-check'; version = '1' }
}} | ConvertTo-Json -Compress -Depth 5
$psi = [Diagnostics.ProcessStartInfo]::new($client)
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
foreach ($arg in @('client', '--connect', "127.0.0.1:$Port", '--token-file', $tokenFile,
    '--host-root', $HostRoot, '--guest-root', $GuestRoot)) { $null = $psi.ArgumentList.Add($arg) }
$process = [Diagnostics.Process]::Start($psi)
try {
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.WriteLine($probe)
    $process.StandardInput.Flush()
    $responseTask = $process.StandardOutput.ReadLineAsync()
    if (-not $responseTask.Wait(8000)) { throw 'ACP initialize exceeded 8 seconds.' }
    if (-not $responseTask.Result) { throw 'Bridge exited without an ACP response; check the protected guest server log.' }
    $response = $responseTask.Result | ConvertFrom-Json
    if ($response.id -ne 1 -or -not $response.result -or $response.result.protocolVersion -ne 1) {
        throw 'ACP initialize returned an unexpected response.'
    }
    Write-Host 'ACP initialize succeeded through host stdio -> TCP -> guest agy (protocol v1).'
} finally {
    if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
    $process.Dispose()
}

# Emit an example, never overwrite the user's runtime catalog automatically.
$config = @{ agents = @(@{
    id = 'agy-1'; name = 'Antigravity CLI (Sandbox 1)'; command = $client;
    args = @('client', '--connect', "127.0.0.1:$Port", '--token-file', $tokenFile,
        '--host-root', $HostRoot, '--guest-root', $GuestRoot);
    permission_mode = 'full_access'; env = @{}; create_timeout_s = 120
}) }
$configPath = Join-Path $out 'external-acp-agents.example.json'
$config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $configPath -Encoding utf8
Write-Host "Example catalog: $configPath (merge the agy-1 entry into your runtime catalog)."
Write-Host "Guest PID file: $guestPid; bridge must be launched again after a VM restart."
