#Requires -Version 5.1
<#
.SYNOPSIS
    One-click desktop launcher for the Synapse Web Console (Windows).

.DESCRIPTION
    Starts the loopback console host (``synapse web-console``) detached in the background,
    waits until it answers on the loopback port, then opens the console in its own desktop
    window.  Preference order: the PWA installed from the console page (``--app-id`` Start
    Menu shortcut, i.e. the real installed app window), then an Edge/Chrome ``--app=`` window,
    then a normal browser window.  Running it again while the host is alive only opens the
    window, so the desktop icon is idempotent.

    An installed PWA keeps the origin it was installed from, so it only reaches the console
    while the host runs on that same port/origin; pass ``-NoPwa`` (or ``-PwaShortcut``) when
    you moved the console to another port.

    Pairing is disabled by default (``--no-pairing``) so the icon needs no code.  The host
    stays loopback-only and single-user; pass ``-Pairing`` to keep the one-time pairing code
    (it is copied to the clipboard and shown in a popup).

    Logs and the recorded host PID live in ``%LOCALAPPDATA%\Synapse\web-console``:
    ``console.out`` (stdout metadata line), ``console.err`` (stderr, pairing code when
    enabled), ``launcher.log``.

.EXAMPLE
    pwsh -NoProfile -File scripts\web-console-desktop.ps1 -InstallShortcut
    Start the console, open the window, and (re)create the desktop shortcut.

.EXAMPLE
    pwsh -NoProfile -File scripts\web-console-desktop.ps1 -Stop
    Stop the console host.  A runtime daemon this host started stays alive as a service and
    is reused next time; add -WithDaemon to stop the daemon published for the state dir too.

.EXAMPLE
    pwsh -NoProfile -File scripts\web-console-desktop.ps1 -Restart
    Stop the console host, wait for the port to be released, then start it again and open the
    window.  The daemon keeps running (a live agent turn would survive); add -WithDaemon for a
    full restart of the runtime as well.
#>
[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$Workspace,
    [string]$StateDir,
    [string]$LogDir,
    [switch]$Pairing,
    [switch]$DefaultBrowser,
    [switch]$NoPwa,
    [switch]$NoOpen,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$WithDaemon,
    [switch]$InstallShortcut,
    [switch]$NoPopup,
    [string]$ShortcutName = 'Synapse 控制台',
    [ValidateSet('start', 'restart', 'stop')]
    [string]$ShortcutAction = 'start',
    [string]$PwaName = 'Synapse',
    [string]$PwaShortcut,
    [int]$StartupTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
if (-not $Workspace) { $Workspace = $repo }
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
if (-not $StateDir) { $StateDir = Join-Path $env:USERPROFILE '.synapse\runtime' }
if (-not $LogDir) { $LogDir = Join-Path $env:LOCALAPPDATA 'Synapse\web-console' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$launcherLog = Join-Path $LogDir 'launcher.log'
$pidFile = Join-Path $LogDir 'console.pid'
$outFile = Join-Path $LogDir 'console.out'
$errFile = Join-Path $LogDir 'console.err'
$consoleUrl = "http://127.0.0.1:$Port/"

function Write-LauncherLog {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
}

function Show-Failure {
    param([string]$Message)
    Write-LauncherLog "FAIL $Message"
    Write-Host $Message
    if ($NoPopup) { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup($Message, 30, 'Synapse Web Console', 48) | Out-Null
    } catch {
        # Popup is a convenience only; the launcher log already has the message.
    }
}

function Test-ConsoleEndpoint {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return ($response.StatusCode -eq 200 -and $response.Content -like '*Synapse Console*')
    } catch {
        # Anything that is not the console's index page counts as "not ours".
        return $false
    }
}

function Test-LoopbackPort {
    param([int]$Port, [int]$TimeoutMilliseconds = 400)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait($TimeoutMilliseconds)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-ConsoleHostProcess {
    param([int]$Port)
    $portPattern = "*--port $Port*"
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like $portPattern -and
            ($_.CommandLine -like '*web_console.entry*' -or $_.CommandLine -like '*web-console*')
        }
}

function Quote-Argument {
    param([string]$Value)
    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function Resolve-HostCommand {
    $arguments = @(
        '-m', 'synapse.web_console.entry',
        '--workspace', $Workspace,
        '--state-dir', $StateDir,
        '--port', "$Port"
    )
    $staticDir = Join-Path $repo 'web\dist'
    if (Test-Path -LiteralPath $staticDir) {
        $arguments += @('--static-dir', $staticDir)
    }
    if (-not $Pairing) {
        $arguments += '--no-pairing'
    }
    $venvPython = Join-Path $repo '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        return @{ Executable = $venvPython; Arguments = $arguments }
    }
    # No venv (fresh checkout): fall back to the module form through uv.
    return @{ Executable = 'uv'; Arguments = @('run', '--no-sync', 'python') + $arguments }
}

function Start-ConsoleHost {
    param([hashtable]$Command)
    $cmdFile = Join-Path $LogDir 'start-console.cmd'
    $line = (@($Command.Executable) + $Command.Arguments |
        ForEach-Object { Quote-Argument $_ }) -join ' '
    $content = @(
        '@echo off'
        'chcp 65001 > nul'
        "cd /d `"$repo`""
        "$line > `"$outFile`" 2> `"$errFile`""
    ) -join "`r`n"
    # UTF-8 without BOM: cmd.exe would choke on a BOM before "@echo off".
    [IO.File]::WriteAllText($cmdFile, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-LauncherLog "starting console host: $line"

    # Detached start: `cmd.exe /c` plus Win32_Process.Create keeps the child out of this
    # process tree and off the caller's stdout, which Start-Process with redirection does not
    # (see README "Web 控制台").
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = "cmd.exe /c `"$cmdFile`""
        CurrentDirectory = $repo
    }
    if ($created.ReturnValue -ne 0) {
        throw "could not start the console host (Win32_Process.Create returned $($created.ReturnValue))"
    }

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-LoopbackPort -Port $Port) { return }
        Start-Sleep -Milliseconds 250
    }

    $tail = ''
    if (Test-Path -LiteralPath $errFile) {
        $tail = (Get-Content -LiteralPath $errFile -Tail 5 -ErrorAction SilentlyContinue) -join ' '
    }
    throw "the console host did not answer on $consoleUrl within $StartupTimeoutSeconds s. $tail"
}

function Save-HostPid {
    $hostProcess = Get-ConsoleHostProcess -Port $Port | Select-Object -First 1
    if ($hostProcess) {
        Set-Content -LiteralPath $pidFile -Value $hostProcess.ProcessId -Encoding ASCII
    }
}

function Get-AppModeBrowser {
    $candidates = @()
    $userChoice = (Get-ItemProperty `
        'HKCU:\SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' `
        -ErrorAction SilentlyContinue).ProgId
    if ($userChoice -like 'MSEdge*') { $candidates += 'msedge.exe' }
    elseif ($userChoice -like 'Chrome*') { $candidates += 'chrome.exe' }
    $candidates += @('msedge.exe', 'chrome.exe')
    foreach ($name in $candidates) {
        $path = (Get-ItemProperty `
            "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$name" `
            -ErrorAction SilentlyContinue).'(default)'
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

function Get-InstalledPwaShortcut {
    # Installed PWA shortcuts (Chrome/Edge) live in the Start Menu and carry ``--app-id=``.
    # Launching one opens the real installed app window (own identity, taskbar jump list,
    # window-controls-overlay title bar) instead of a bare ``--app=`` window.
    # A leftover shortcut is not proof: the app id must still exist in the browser profile
    # (``<User Data>\<profile>\Web Applications\Manifest Resources\<app-id>``), otherwise the
    # browser would just open a normal window.
    $roots = @(
        (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
        (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs')
    )
    $shell = New-Object -ComObject WScript.Shell
    $matches = @()
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $candidates = Get-ChildItem -LiteralPath $root -Recurse -Filter '*.lnk' -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*$PwaName*" }
        foreach ($candidate in $candidates) {
            try {
                $shortcut = $shell.CreateShortcut($candidate.FullName)
            } catch {
                continue
            }
            if ($shortcut.Arguments -notlike '*--app-id=*') { continue }
            if (Test-PwaInstalled -Shortcut $shortcut) {
                $matches += $candidate.FullName
            } else {
                Write-LauncherLog "ignoring stale PWA shortcut $($candidate.FullName)"
            }
        }
    }
    # "Synapse Console.lnk" beats a duplicate install named "Synapse Console (1).lnk".
    return @($matches | Sort-Object { if ($_ -match '\(\d+\)\.lnk$') { 1 } else { 0 } })
}

function Get-PwaUserDataRoot {
    param([string]$TargetPath)
    if (-not $TargetPath) { return $null }
    $exe = Split-Path -Path $TargetPath -Leaf
    if ($exe -like 'msedge*') { return (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\User Data') }
    if ($exe -like 'chrome*') { return (Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data') }
    return $null
}

function Test-PwaInstalled {
    param([object]$Shortcut)
    $target = [string]$Shortcut.TargetPath
    if (-not $target -or -not (Test-Path -LiteralPath $target)) { return $false }
    $arguments = [string]$Shortcut.Arguments
    $appIdMatch = [regex]::Match($arguments, '--app-id=([0-9a-zA-Z]{32})')
    if (-not $appIdMatch.Success) { return $false }
    $profileMatch = [regex]::Match($arguments, '--profile-directory="([^"]*)"')
    if (-not $profileMatch.Success) {
        $profileMatch = [regex]::Match($arguments, '--profile-directory=(\S+)')
    }
    $profile = if ($profileMatch.Success) { $profileMatch.Groups[1].Value } else { 'Default' }
    $userDataRoot = Get-PwaUserDataRoot -TargetPath $target
    if (-not $userDataRoot) { return $false }
    $manifestDir = Join-Path $userDataRoot "$profile\Web Applications\Manifest Resources\$($appIdMatch.Groups[1].Value)"
    return (Test-Path -LiteralPath $manifestDir)
}

function Open-ConsoleWindow {
    if (-not $DefaultBrowser) {
        if (-not $NoPwa) {
            $pwa = @()
            if ($PwaShortcut) {
                $resolved = (Resolve-Path -LiteralPath $PwaShortcut).Path
                $shell = New-Object -ComObject WScript.Shell
                if (-not (Test-PwaInstalled -Shortcut $shell.CreateShortcut($resolved))) {
                    # Explicitly requested: still open it, but say that it looks stale.
                    Write-LauncherLog "WARNING $resolved looks stale (target or installed app missing)"
                }
                $pwa = @($resolved)
            } else {
                $pwa = Get-InstalledPwaShortcut
            }
            if ($pwa.Count -gt 1) {
                Write-LauncherLog "several installed PWAs match '$PwaName': $($pwa -join '; ')"
            }
            if ($pwa.Count -gt 0) {
                Write-LauncherLog "opening installed PWA $($pwa[0])"
                Start-Process -FilePath $pwa[0]
                return
            }
            Write-LauncherLog "no installed PWA matching '$PwaName' found; falling back"
        }
        $browser = Get-AppModeBrowser
        if ($browser) {
            Write-LauncherLog "opening app window via $browser"
            Start-Process -FilePath $browser -ArgumentList "--app=$consoleUrl"
            return
        }
    }
    Write-LauncherLog 'opening the default browser'
    Start-Process $consoleUrl
}

function Read-PairingCode {
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (Test-Path -LiteralPath $errFile) {
            $text = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue
            $match = [regex]::Match([string]$text, 'pairing code ([0-9A-Z]{8})')
            if ($match.Success) { return $match.Groups[1].Value }
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

function Stop-ConsoleHost {
    $targets = @()
    if (Test-Path -LiteralPath $pidFile) {
        $recorded = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($recorded -match '^\d+$') { $targets += [int]$recorded }
    }
    $targets += @(Get-ConsoleHostProcess -Port $Port | Select-Object -ExpandProperty ProcessId)
    $targets = @($targets | Sort-Object -Unique)
    if ($targets.Count -eq 0) {
        Write-Host "没有在 $Port 端口上运行的控制台宿主。"
        return
    }
    foreach ($targetPid in $targets) {
        Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        Write-LauncherLog "stopped console host pid=$targetPid"
        Write-Host "已停止控制台宿主（pid $targetPid）。"
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue

    if ($WithDaemon) {
        $metadataPath = Join-Path $StateDir 'daemon.json'
        if (Test-Path -LiteralPath $metadataPath) {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
            if ($metadata.pid) {
                Stop-Process -Id ([int]$metadata.pid) -Force -ErrorAction SilentlyContinue
                Write-LauncherLog "stopped runtime daemon pid=$($metadata.pid)"
                Write-Host "已停止运行时 daemon（pid $($metadata.pid)）。"
            }
        }
    }
    Write-Host '提示：宿主自启的 daemon 会作为服务继续运行，下次启动直接复用。'
}

function Wait-PortReleased {
    param([int]$Port, [int]$TimeoutSeconds = 10)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-LoopbackPort -Port $Port -TimeoutMilliseconds 200)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function New-ShortcutIcon {
    param([string]$PngPath)
    if (-not (Test-Path -LiteralPath $PngPath)) { return $null }
    $icoPath = Join-Path $LogDir 'synapse-console.ico'
    try {
        $png = [IO.File]::ReadAllBytes($PngPath)
        if ($png.Length -lt 24) { return $null }
        # PNG IHDR: width/height as big-endian UInt32 at offsets 16 and 20.
        $width = [int]($png[16] * 16777216 + $png[17] * 65536 + $png[18] * 256 + $png[19])
        $height = [int]($png[20] * 16777216 + $png[21] * 65536 + $png[22] * 256 + $png[23])
        $stream = New-Object IO.MemoryStream
        $writer = New-Object IO.BinaryWriter($stream)
        try {
            $writer.Write([UInt16]0)            # reserved
            $writer.Write([UInt16]1)            # type: icon
            $writer.Write([UInt16]1)            # one image
            $writer.Write([byte]($width % 256)) # width (0 means 256)
            $writer.Write([byte]($height % 256))
            $writer.Write([byte]0)              # palette
            $writer.Write([byte]0)              # reserved
            $writer.Write([UInt16]1)            # color planes
            $writer.Write([UInt16]32)           # bits per pixel
            $writer.Write([UInt32]$png.Length)
            $writer.Write([UInt32]22)           # offset: ICONDIR + one ICONDIRENTRY
            $writer.Write($png, 0, $png.Length)
            $writer.Flush()
            [IO.File]::WriteAllBytes($icoPath, $stream.ToArray())
        } finally {
            $writer.Dispose()
            $stream.Dispose()
        }
        return $icoPath
    } catch {
        Write-LauncherLog "icon conversion failed: $($_.Exception.Message)"
        return $null
    }
}

function Resolve-Pythonw {
    $venvPythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $venvPythonw) { return $venvPythonw }
    $command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Install-HiddenLauncher {
    # Windows Terminal (the default console host on Windows 11) cannot hide a console window for
    # `pwsh -WindowStyle Hidden`, so a shortcut pointing straight at pwsh.exe still flashes a
    # black box.  pythonw.exe is a GUI-subsystem host: it starts pwsh with CREATE_NO_WINDOW, so no
    # console is created at all.  Returns $null when no pythonw.exe is available, in which case the
    # shortcut falls back to pwsh.exe and the window comes back.
    $pythonw = Resolve-Pythonw
    if (-not $pythonw) {
        Write-LauncherLog 'pythonw.exe not found; the shortcut will show a console window'
        return $null
    }
    $pwshCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    $hostExe = if ($pwshCommand) { $pwshCommand.Source } else { (Get-Command powershell.exe).Source }
    $wrapperPath = Join-Path $LogDir 'launch-console.py'
    $content = @"
# Generated by scripts/web-console-desktop.ps1 - start the console launcher with no console window.
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

subprocess.Popen(
    [r"$hostExe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r"$PSCommandPath", *sys.argv[1:]],
    creationflags=CREATE_NO_WINDOW,
    close_fds=True,
)
"@
    [IO.File]::WriteAllText($wrapperPath, $content, (New-Object System.Text.UTF8Encoding($false)))
    return @{ Host = $pythonw; Script = $wrapperPath }
}

function Install-DesktopShortcut {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $lnkPath = Join-Path $desktop ($ShortcutName + '.lnk')
    $pwshCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    $hostExe = if ($pwshCommand) { $pwshCommand.Source } else { (Get-Command powershell.exe).Source }
    $iconPath = New-ShortcutIcon -PngPath (Join-Path $repo 'web\public\icon-192.png')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnkPath)
    $actionArgument = switch ($ShortcutAction) {
        'restart' { ' -Restart' }
        'stop' { ' -Stop' }
        default { '' }
    }
    $launcher = Install-HiddenLauncher
    if ($launcher) {
        $shortcut.TargetPath = $launcher.Host
        $shortcut.Arguments = "`"$($launcher.Script)`"$actionArgument"
    } else {
        $shortcut.TargetPath = $hostExe
        $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`"$actionArgument"
    }
    $shortcut.WorkingDirectory = $repo
    $shortcut.Description = 'Synapse Web 控制台：后台启动并打开窗口'
    if ($iconPath) { $shortcut.IconLocation = "$iconPath,0" }
    $shortcut.Save()
    Write-LauncherLog "desktop shortcut written: $lnkPath"
    Write-Host "桌面快捷方式已创建：$lnkPath"
}

try {
    if ($Stop) {
        if (-not $Restart) {
            Stop-ConsoleHost
            return
        }
    }

    if ($Restart) {
        Write-Host '重启控制台：先停宿主…'
        Stop-ConsoleHost
        if (-not (Wait-PortReleased -Port $Port)) {
            throw "端口 $Port 在 10 s 内没有释放，无法重启。"
        }
    }

    if (Test-LoopbackPort -Port $Port) {
        if (Test-ConsoleEndpoint -Url $consoleUrl) {
            Write-Host "控制台已在运行：$consoleUrl"
            Write-LauncherLog "console host already listening on port $Port"
            Save-HostPid
        } else {
            throw "端口 $Port 已被其它程序占用，且它不是 Synapse 控制台。请用 -Port 换一个端口。"
        }
    } else {
        Start-ConsoleHost -Command (Resolve-HostCommand)
        Save-HostPid
        Write-Host "控制台已启动：$consoleUrl（日志：$LogDir）"
        if ($Pairing) {
            $code = Read-PairingCode
            if ($code) {
                Set-Clipboard -Value $code
                Write-Host "配对码 $code 已复制到剪贴板，在窗口中输入即可。"
            } else {
                Write-Host "未能自动读取配对码，请查看 $errFile。"
            }
        }
    }

    if (-not $NoOpen) {
        Open-ConsoleWindow
    }

    if ($InstallShortcut) {
        Install-DesktopShortcut
    }
} catch {
    Show-Failure $_.Exception.Message
    exit 1
}
