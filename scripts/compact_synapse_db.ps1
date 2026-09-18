#Requires -Version 7.0
<#
.SYNOPSIS
    压缩 Synapse 本地数据库：清理派生数据后 VACUUM，回收磁盘空间。

.DESCRIPTION
    依次执行（可用开关裁剪）：
      1. 删除全部子 agent 检查点（checkpoint_ns 非空）及其 writes。主历史（checkpoint_ns 为空）不受影响。
      2. 回收冗余的 DeltaChannel 快照：每个线程每个通道只保留最新的 N 个。
      3. 裁剪模型请求压缩诊断事件：保留最近 N 天，且每个会话保底保留 M 条。
      4. 对每个库执行 VACUUM，随后 WAL checkpoint，把内容合并回主文件并截断 WAL。

    VACUUM 需要独占访问，因此必须在 Synapse 完全退出后运行。
    脚本可重复执行：已符合保留条件的数据不会被再次删除。

    步骤 2 说明：LangGraph 的 DeltaChannel 平时只在 checkpoint 里存增量，但会周期性
    把完整状态物化为 _DeltaSnapshot（deepagents 设为每 50 次更新一次）。实测 0.7%
    的快照行占了 checkpoints.sqlite 约 70% 的字节。重建时只会用到"最近的祖先快照"，
    因此更旧的快照属于冗余。
    硬约束：每个线程必须保留最新快照 —— 含上下文压缩（Overwrite）的线程，其 writes
    不是完整变更日志，从空或过旧的快照回放会产生不同的消息内容（静默、不报错）。
    该步骤在改写前后比对每个线程的内容指纹，不一致会报错并要求回滚。
    注意：删除旧快照会失去"读取历史检查点"（时间旅行/回滚）的能力。

.PARAMETER SynapseDir
    .synapse 目录，默认 <当前目录>\.synapse

.PARAMETER NoPurge
    只做 VACUUM，不删除任何数据。

.PARAMETER EventRetentionDays
    压缩诊断事件保留天数，默认 7；0 表示不按时间裁剪。

.PARAMETER KeepEventsPerThread
    每个会话保底保留的事件条数，默认 20。

.PARAMETER SnapshotKeep
    步骤 2 中每个线程每个通道保留的最新快照数量，默认 1。
    设为 0 会被拒绝：最新快照必须保留。

.PARAMETER SkipSnapshotReclaim
    跳过步骤 2（回收冗余快照）。

.PARAMETER SnapshotBackup
    步骤 2 运行前把 checkpoints.sqlite 备份为 checkpoints.sqlite.bak。
    该步骤不可逆；未加此开关时会以 --no-backup 明确跳过备份检查。

.PARAMETER DryRun
    只打印将要执行的操作与当前体积，不修改任何数据。

.PARAMETER Force
    跳过"应用是否仍在运行"的前置检查（有风险，仅调试用）。

.EXAMPLE
    pwsh -File scripts/compact_synapse_db.ps1 -DryRun

.EXAMPLE
    pwsh -File scripts/compact_synapse_db.ps1

.EXAMPLE
    pwsh -File scripts/compact_synapse_db.ps1 -SnapshotBackup -SnapshotKeep 3
#>
[CmdletBinding()]
param(
    [string]$SynapseDir = (Join-Path (Get-Location) '.synapse'),
    [switch]$NoPurge,
    [int]$EventRetentionDays = 7,
    [int]$KeepEventsPerThread = 20,
    [int]$SnapshotKeep = 1,
    [switch]$SkipSnapshotReclaim,
    [switch]$SnapshotBackup,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Write-Head([string]$m) { Write-Host ''; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Good([string]$m) { Write-Host "    $m" -ForegroundColor Green }
function Write-Note([string]$m) { Write-Host "    $m" }
function Write-Caution([string]$m) { Write-Host "    $m" -ForegroundColor Yellow }
function Stop-Script([string]$m) { Write-Host ''; Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }
function Format-Size([double]$bytes) {
    if ($bytes -ge 1GB) { return ('{0:N2} GB' -f ($bytes / 1GB)) }
    return ('{0:N1} MB' -f ($bytes / 1MB))
}
function Get-Footprint([string]$path) {
    $sum = 0
    foreach ($p in @($path, "$path-wal", "$path-shm")) {
        if (Test-Path -LiteralPath $p) { $sum += (Get-Item -LiteralPath $p).Length }
    }
    return $sum
}

# ---------------- 前置检查 ----------------
if (-not (Test-Path -LiteralPath $SynapseDir)) { Stop-Script "找不到目录: $SynapseDir" }
$SynapseDir = (Resolve-Path -LiteralPath $SynapseDir).Path

$sqliteCmd = Get-Command sqlite3 -ErrorAction SilentlyContinue
if (-not $sqliteCmd) { Stop-Script 'PATH 中找不到 sqlite3，请先安装（例如 scoop install sqlite）' }
$Sqlite = $sqliteCmd.Source

$candidates = @('checkpoints.sqlite', 'tool-outputs.sqlite', 'transcript.sqlite', 'search-index.sqlite', 'sessions.sqlite')
$paths = @{}
foreach ($n in $candidates) {
    $p = Join-Path $SynapseDir $n
    if (Test-Path -LiteralPath $p) { $paths[$n] = $p }
}
if (-not $paths.ContainsKey('checkpoints.sqlite')) { Stop-Script "找不到 $SynapseDir\checkpoints.sqlite" }
$ordered = @($candidates | Where-Object { $paths.ContainsKey($_) })

function Invoke-Sql([string]$db, [string]$sql) {
    $out = & $Sqlite $db $sql 2>&1
    if ($LASTEXITCODE -ne 0) { Stop-Script "sqlite3 执行失败 (exit $LASTEXITCODE)`n        SQL: $sql`n        $out" }
    return $out
}
function Invoke-Scalar([string]$db, [string]$sql) {
    $out = & $Sqlite $db $sql 2>&1
    if ($LASTEXITCODE -ne 0) { Stop-Script "sqlite3 执行失败 (exit $LASTEXITCODE)`n        SQL: $sql`n        $out" }
    return (($out | ForEach-Object { "$_" }) -join ' ').Trim()
}

if ($Force) {
    Write-Head '前置检查：已用 -Force 跳过占用检查'
} else {
    Write-Head '前置检查：确认 Synapse 已退出'
    foreach ($n in $ordered) {
        try {
            $fs = [System.IO.File]::Open($paths[$n], [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $fs.Close()
        } catch {
            Stop-Script "文件被占用：$($paths[$n])`n       请完全退出 Synapse TUI / synapse-runtime 后重试，或加 -Force 跳过检查"
        }
    }
    Write-Good '没有进程持有数据库文件'
}

$before = @{}
foreach ($n in $ordered) { $before[$n] = Get-Footprint $paths[$n] }
$totalBefore = 0
Write-Head '当前体积'
foreach ($n in $ordered) {
    Write-Note ('{0,-22} {1,10}' -f $n, (Format-Size $before[$n]))
    $totalBefore += $before[$n]
}
Write-Note ('{0,-22} {1,10}' -f '合计', (Format-Size $totalBefore))

# ---------------- 步骤 1：子 agent 检查点 ----------------
if ($NoPurge) {
    Write-Head '步骤 1/4  清理子 agent 检查点：跳过（-NoPurge）'
} else {
    Write-Head '步骤 1/4  清理子 agent 检查点'
    $cp = $paths['checkpoints.sqlite']
    $info = Invoke-Scalar $cp "SELECT count(*) || ' 个检查点 / ' || printf('%.2f GB', coalesce(sum(length(checkpoint)),0)/1073741824.0) || '，另有 ' || (SELECT count(*) FROM writes WHERE length(checkpoint_ns) > 0) || ' 条 writes' FROM checkpoints WHERE length(checkpoint_ns) > 0;"
    Write-Note "待删除: $info"
    if ($DryRun) {
        Write-Caution 'DryRun：跳过删除'
    } else {
        Invoke-Sql $cp "PRAGMA busy_timeout=60000; BEGIN IMMEDIATE; DELETE FROM writes WHERE length(checkpoint_ns) > 0; DELETE FROM checkpoints WHERE length(checkpoint_ns) > 0; COMMIT;" | Out-Null
        Write-Good '已删除；主历史（checkpoint_ns 为空）未受影响'
    }
}

# ---------------- 步骤 2：回收冗余 DeltaChannel 快照 ----------------
if ($NoPurge) {
    Write-Head '步骤 2/4  回收冗余快照：跳过（-NoPurge）'
} elseif ($SkipSnapshotReclaim) {
    Write-Head '步骤 2/4  回收冗余快照：跳过（-SkipSnapshotReclaim）'
} else {
    Write-Head '步骤 2/4  回收冗余 DeltaChannel 快照'
    Write-Note '每个线程每个通道只保留最新的快照；最新快照必须保留'
    $reclaim = Join-Path $PSScriptRoot 'reclaim_checkpoint_snapshots.py'
    if (-not (Test-Path -LiteralPath $reclaim)) {
        Write-Caution "找不到 $reclaim，跳过"
    } elseif (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Caution 'PATH 中找不到 uv，跳过'
    } else {
        $pyArgs = @('run', '--no-sync', 'python', $reclaim,
            '--synapse-dir', $SynapseDir,
            '--keep', "$SnapshotKeep",
            '--force')
        if ($DryRun) {
            $pyArgs += '--dry-run'
        } elseif ($SnapshotBackup) {
            $bak = Join-Path $SynapseDir 'checkpoints.sqlite.bak'
            if (Test-Path -LiteralPath $bak) {
                Write-Note "备份已存在：$bak"
            } else {
                Write-Note "创建备份 $bak ..."
                Copy-Item -LiteralPath (Join-Path $SynapseDir 'checkpoints.sqlite') -Destination $bak
            }
        } else {
            $pyArgs += '--no-backup'
            Write-Caution '未做备份（加 -SnapshotBackup 可在运行前自动备份）'
        }
        $out = & uv @pyArgs 2>&1
        $code = $LASTEXITCODE
        foreach ($line in $out) { Write-Note "$line" }
        if ($code -ne 0) { Stop-Script "快照回收失败（退出码 $code）" }
        Write-Good '快照回收完成'
    }
}

# ---------------- 步骤 3：压缩诊断事件 ----------------
$to = $paths['tool-outputs.sqlite']
if ($NoPurge) {
    Write-Head '步骤 3/4  裁剪压缩诊断事件：跳过（-NoPurge）'
} elseif (-not $to) {
    Write-Head '步骤 3/4  裁剪压缩诊断事件：未找到 tool-outputs.sqlite，跳过'
} else {
    Write-Head '步骤 3/4  裁剪模型请求压缩诊断事件'
    $has = Invoke-Scalar $to "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='model_request_compression_events';"
    if ([int]$has -eq 0) {
        Write-Caution '该表不存在，跳过'
    } else {
        $total = Invoke-Scalar $to 'SELECT count(*) FROM model_request_compression_events;'
        $cut = $null
        if ($EventRetentionDays -gt 0) {
            $cut = (Get-Date).ToUniversalTime().AddDays(-$EventRetentionDays).ToString('yyyy-MM-ddTHH:mm:ssZ')
        }
        Write-Note ('当前 {0} 条；保留策略：最近 {1} 天 + 每会话 {2} 条' -f $total, $EventRetentionDays, $KeepEventsPerThread)
        $keepSql = "SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY thread_id ORDER BY id DESC) rn FROM model_request_compression_events) WHERE rn <= $KeepEventsPerThread"
        $where = "id NOT IN ($keepSql)"
        if ($cut) { $where = "created_at < '$cut' AND $where" }
        $doomed = Invoke-Scalar $to "SELECT count(*) || ' 条 / ' || printf('%.1f MB', coalesce(sum(length(event_json)),0)/1048576.0) FROM model_request_compression_events WHERE $where;"
        Write-Note "待删除: $doomed"
        if ($DryRun) {
            Write-Caution 'DryRun：跳过删除'
        } else {
            Invoke-Sql $to "PRAGMA busy_timeout=60000; BEGIN IMMEDIATE; DELETE FROM model_request_compression_events WHERE $where; COMMIT;" | Out-Null
            Write-Good '已裁剪'
        }
    }
}

# ---------------- 步骤 4：VACUUM ----------------
Write-Head '步骤 4/4  VACUUM 回收空间'
if ($DryRun) {
    Write-Caution 'DryRun：跳过 VACUUM'
} else {
    foreach ($n in $ordered) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-Sql $paths[$n] 'VACUUM;' | Out-Null
        Invoke-Sql $paths[$n] 'PRAGMA wal_checkpoint(TRUNCATE);' | Out-Null
        $sw.Stop()
        Write-Good ('{0,-22} 用时 {1,5:N1}s' -f $n, $sw.Elapsed.TotalSeconds)
    }
    Write-Note 'VACUUM 之后必须再 checkpoint，否则新内容会滞留在 WAL 中'
}

# ---------------- 结果 ----------------
Write-Head '结果'
$rows = @()
$totalAfter = 0
foreach ($n in $ordered) {
    $after = Get-Footprint $paths[$n]
    $totalAfter += $after
    $rows += [pscustomobject]@{
        文件   = $n
        清理前 = Format-Size $before[$n]
        清理后 = Format-Size $after
        释放   = Format-Size ($before[$n] - $after)
    }
}
$rows | Format-Table -AutoSize | Out-String | Write-Host
Write-Note ('合计: {0} -> {1}，释放 {2}' -f (Format-Size $totalBefore), (Format-Size $totalAfter), (Format-Size ($totalBefore - $totalAfter)))

if (-not $DryRun) {
    Write-Head '完整性校验'
    foreach ($n in $ordered) {
        $ic = Invoke-Scalar $paths[$n] 'PRAGMA integrity_check;'
        $pages = Invoke-Scalar $paths[$n] "SELECT page_count || ' 页，空闲 ' || freelist_count || ' 页' FROM pragma_page_count(), pragma_freelist_count();"
        if ($ic -ne 'ok') { Write-Caution ('{0,-22} integrity={1}' -f $n, $ic) }
        else { Write-Note ('{0,-22} integrity=ok  {1}' -f $n, $pages) }
    }
}

Write-Host ''
Write-Good '完成。'