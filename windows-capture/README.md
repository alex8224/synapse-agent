# windows-capture

A small Windows screenshotter for **windows** (not the desktop), built on the real
**Windows Graphics Capture (WGC)** API, with a WPF GUI, a persistent local JSON-RPC service and a
CLI that a Python `stdlib`-only consumer can drive.

It is deliberately narrow: pick one top-level window, capture N frames at an interval, read the
results back over a named pipe. Nothing is injected, hooked or hooked-around; anti-cheat is never
bypassed.

---

## 1. What it does

* Enumerates capturable top-level windows (HWND + PID + process + process start time + title).
* Captures the selected window with `GraphicsCaptureItem::CreateForWindow` driven by a
  free-threaded `Direct3D11CaptureFramePool` on a D3D11 device. Frames are read back from the GPU
  and PNG-encoded in memory.
* Because WGC taps the window's own composition surface, **the window's own pixels are captured even
  when it is fully covered by another window** (verified, see §8).
* Runs a **named-pipe JSON-RPC 2.0 service** so other processes (e.g. a Synapse tool call) can list
  windows, start jobs, poll state, cancel and read results.
* Persists configuration, re-validates the target on every use, and reuses the WGC session and GPU
  resources for a valid target.
* Optionally creates one process-owned virtual Xbox 360 controller through ViGEmBus. Every button or
  stick operation requires a finite 1–5000 ms duration and returns to neutral in a `finally` block.
  This input capability is independent from capture and uses the same same-user pipe.

### What it explicitly does **not** do

* No desktop/window cropping, no `PrintWindow`, no `BitBlt` fallback. If WGC cannot capture the
  window, the tool fails and says so.
* No injection, no hooks, no anti-cheat bypass. Elevated targets may impose permission constraints;
  elevation does not bypass protected content or capture exclusion. Such windows may fail or
  produce black frames.
* It does not silently switch to a different window when the target closes. A closed or minimized
  target is a **clear failure**.
* It never re-labels a cached frame as new (see §6).
* DirectX/OpenGL/Vulkan compatibility depends on the target's presentation mode, not just its HWND
  or graphics API. These targets have **not** been individually validated; exclusive fullscreen
  is best-effort — see §8.

---

## 2. Requirements

| | |
| --- | --- |
| OS | Windows 10 **1903 (build 18362)** or newer, Windows 11 |
| Target framework | `net8.0-windows10.0.19041.0`, `SupportedOSPlatformVersion` `10.0.18362.0` |
| SDK | .NET 8 SDK (built and tested with 9.0.310 installed) |
| GPU | any D3D11-capable device (WARP is not requested; hardware is) |
| NuGet | `Vortice.Direct3D11` 3.8.3 (D3D11/DXGI interop). WinRT projections come from the Windows SDK pack. |

---

## 3. Layout

```
windows-capture/
  WindowsCapture.sln
  Directory.Build.props
  src/WindowsCapture.Core/     net8.0  - no Windows APIs, fully unit tested
    Abstractions.cs              limits, clock, error codes, ICaptureEngine
    Model.cs                     TargetRef, CaptureRequest/Plan, AppConfig, JobView, ...
    CapturePlanner.cs            override > saved > default + validation
    TargetIdentity.cs            HWND + PID + process start time re-validation
    ConfigStore.cs               atomic JSON config, tolerant of corruption
    JobService.cs                single-concurrent job scheduler, cancel, TTL, result store
    RequestRegistry.cs           request_id idempotency + chunking helper
    Json.cs / Protocol.cs        wire options, canonical fingerprints, line framing
    RpcDispatcher.cs             the whole RPC surface + IRpcHost contract
  src/WindowsCapture.App/      net8.0-windows10.0.19041.0, WPF
    Capture/WgcCaptureEngine.cs  real Windows Graphics Capture + D3D11 + PNG
    Capture/Win32.cs             COM/Win32 interop
    Capture/WindowEnumerator.cs  top-level window enumeration
    Capture/ImageCodec.cs        BGRA <-> PNG, pixel assertions
    RpcServer.cs                 named-pipe server (PipeOptions.CurrentUserOnly)
    PipeClient.cs                pipe client + single-instance mutex
    HostService.cs               IRpcHost implementation (GUI + RPC share one job service)
    CliOptions.cs / CliRunner.cs CLI parsing and in-host execution
    SelfTest.cs                  --self-test: private window + real WGC verification
    App.cs / MainWindow.xaml / SettingsWindow.xaml
    Program.cs                   single entry point for every mode
  tests/WindowsCapture.Tests/  net8.0, xunit - 101 tests, no Windows APIs
```

---

## 4. Build, test, run

```powershell
cd windows-capture
dotnet restore
dotnet build -c Release
dotnet test  -c Release

# run the GUI + RPC host
src\WindowsCapture.App\bin\Release\net8.0-windows10.0.19041.0\windows-capture.exe

# or start it hidden (RPC only)
...\windows-capture.exe --hidden
```

> **Environment note.** If `dotnet build` reports `NETSDK1045` / an unexpected SDK 6 path, the shell
> has `MSBUILDSDKSPATH` (and possibly `DOTNET_ROOT`) pointing at another SDK. Clear them for the
> session:
> ```powershell
> $env:MSBUILDSDKSPATH = $null
> $env:DOTNET_ROOT = 'C:\Program Files\dotnet'
> ```

### Modes

| Command | Behaviour |
| --- | --- |
| *(no args)* / `--show-ui` | Become (or focus) the GUI + RPC host. A second launch forwards to the first and exits. |
| `--hidden` | Same, but the window is never shown. |
| `--capture [options]` | Forward a capture command to the host (starting a hidden one if needed). |
| `--rpc '<json>'` / `--rpc-file <path>` | Forward one raw JSON-RPC line and print the raw response. |
| `--list-windows` | Sugar for the `list_windows` method. |
| `--exit` | Ask the host to shut down and release everything. |
| `--self-test` | Create a private window and verify real WGC capture (§8). |
| `--help`, `--version` | |

Capture options: `--count N`, `--interval-ms N`, `--start-delay-ms N`, `--max-edge N`,
`--ttl-seconds N`, `--frame-timeout-ms N`, `--no-reuse`, `--output-dir DIR`,
`--hwnd 0x1A2B`, `--pid N`, `--target-json '<json>'`, `--wait-timeout-ms N`.
Common: `--pipe NAME`, `--no-autostart`.

### Exit codes

`0` ok · `1` unexpected · `2` usage · `3` no host · `4` RPC error response · `5` capture failed ·
`6` cancelled · `7` timeout · `8` target required · `9` self-test failed.

`--rpc`, `--capture` and `--list-windows` print **exactly one JSON document on stdout**;
diagnostics go to stderr.

### GUI behaviour

* **Closing the window hides it** — the RPC service keeps running. Relaunch the executable (or use
  `--show-ui`) to bring it back.
* **`Exit (release)`** (or `--exit`) actually exits, disposing the WGC session, GPU resources, the
  pipe server and every job.
* The GUI and the RPC server share **one** `JobService`, so only one capture job can run at a time
  no matter who asked for it.

---

## 5. RPC contract

Transport: a **named pipe**, `PipeOptions.CurrentUserOnly` (same-user only). No TCP port is ever
opened. Framing: **UTF-8, one JSON document per line, `\n` terminated**, compact (no embedded
newlines).

Default pipe name: `windows-capture-<user SID with '-' replaced by '_'>`
(e.g. `windows-capture-S_1_5_21_...`). Override with `--pipe` on both sides.
Discover it from `get_state` → `pipe`.

### Envelope

Request:

```json
{"jsonrpc":"2.0","id":1,"method":"start","params":{...},"request_id":"optional-idempotency-key"}
```

Success: `{"jsonrpc":"2.0","id":1,"result":{...}}`

Error:

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"...","data":{"error_code":"invalid_params","detail":"got 0"}}}
```

`code` is the JSON-RPC code (`-32700` parse, `-32600` invalid request, `-32601` method not found,
`-32602` invalid params, `-32603` internal, `-32000` server). **Always branch on
`error.data.error_code`**, which is stable.

`error_code` values: `parse_error`, `invalid_request`, `method_not_found`, `invalid_params`,
`target_required`, `target_not_found`, `target_identity_mismatch`, `target_closed`,
`target_minimized`, `no_frame`, `no_new_frame`, `capture_busy`, `capture_failed`, `cancelled`,
`timeout`, `limit_exceeded`, `job_not_found`, `job_not_finished`, `result_released`,
`result_expired`, `result_range_invalid`, `request_id_conflict`, `ui_unavailable`, `internal_error`.

### Idempotency

Send `"request_id":"<key>"` on any call. A retry with the **same** `request_id` and the **same**
params replays the original payload (with the new `id`). The same `request_id` with **different**
params returns `request_id_conflict`. The cache is bounded (LRU, `max_request_registry_entries`).

### Methods

#### `ping`
→ `{"pong":true,"now_ms":<int>}`

#### `get_state`
→ `{ok, version, pid, pipe, ui_visible, selected_target, config, config_path, config_error, limits, active_job, jobs, now_ms}`

#### `list_windows`
→ `{windows:[{hwnd:"0x1234", hwnd_value:4660, pid, title, process, process_start_utc, class_name, visible, minimized, is_foreground, is_selected}]}`

#### `set_target`
params `{"target":{...}|null}` → `{ok, selected_target}`

#### `start`
params (all optional except a target must be resolvable):

```json
{
  "target": {"hwnd":"0x1A2B","pid":123,"process_start_utc":"2024-01-01T00:00:00Z"},
  "count": 3, "interval_ms": 200, "start_delay_ms": 0, "max_edge": 1280,
  "output_dir": null, "format": "png", "ttl_seconds": 300,
  "frame_timeout_ms": 5000, "allow_reuse": true,
  "save_config": false, "interactive_if_needed": true
}
```

→ **returns immediately** with the job snapshot (`job_id`, `state:"queued"`, `target`, `plan`, …).
`hwnd` accepts `"0x1A2B"`, `"6699"` or `6699`.

If no target can be resolved and `interactive_if_needed` is true, the picker UI is opened and the
call returns `target_required`. **This build does not block waiting for the user's selection** —
call `set_target`/`start` again after choosing. Documented simplification, not a wait.

#### `list_jobs`
→ `{jobs:[<job snapshot>...], active_job:<id|null>}`

#### `get_job`
params `{"job_id":"..."}` → job snapshot:

```json
{
  "job_id":"job-...","state":"queued|running|completed|cancelled|failed",
  "target":{...},"requested":3,"captured":2,
  "created_at":<ms>,"started_at":<ms|null>,"finished_at":<ms|null>,"expires_at":<ms>,
  "result_released":false,"cancel_requested":false,"result_bytes":123456,
  "frames":[{"index":0,"captured_at":<ms>,"mime":"image/png","width":1184,"height":792,
             "bytes":71515,"source":"fresh|reused","path":null}],
  "error":{"code":"...","message":"..."}|null,
  "plan":{...}
}
```

#### `cancel`
params `{"job_id":"..."}` → job snapshot (usually already `cancelled`; idempotent).

#### `read_result`
params `{"job_id":"...","index":0,"offset":0,"length":262144}`
→

```json
{"job_id":"...","index":0,"frame_count":3,"offset":0,"length":262144,"total_bytes":75110,
 "eof":false,"mime":"image/png","width":1184,"height":792,"captured_at":<ms>,
 "source":"fresh","data_base64":"..."}
```

`length` is clamped to `limits.max_chunk_bytes` (default 256 KiB) and to the bytes remaining.
Loop with `offset += length` until `eof`. Reads of a *running* job are allowed and return the frames
captured so far.

#### `release_result`
params `{"job_id":"..."}` → `{ok, released:<bool>, job_id}`. Frees the frame payloads immediately
(idempotent). Later `read_result` calls return `result_released` until the job's TTL elapses.

#### `invoke_cli`
params `{"args":["--capture","--count","1"],"timeout_ms":300000}`
→ `{exit_code:<int>, stdout:"<one JSON document>", stderr:""}`.
This is what the CLI uses to relay a command line to the running host.

### Virtual gamepad RPC

The host can create one ordinary system-wide virtual Xbox 360 controller when ViGEmBus is installed.
It does not inject into games, target a specific process, read a physical controller, or bypass
anti-cheat. A program that accepts XInput may observe the controller, so callers must release it at
the end of every task.

| Method | Parameters | Behaviour |
| --- | --- | --- |
| `gamepad.get_state` | none | `{available, connected, unavailable_reason, user_index, max_hold_ms}` |
| `gamepad.connect` | none | Creates and neutralises the virtual controller. |
| `gamepad.press` | `{button, hold_ms}` | White-listed Xbox button, required `hold_ms: 1..5000`; released automatically. |
| `gamepad.move` | `{direction, hold_ms}` | Finite left-stick `forward`, `backward`, `left`, or `right`; neutralised automatically. |
| `gamepad.look` | `{x, y, hold_ms}` | Finite right-stick axes in `[-1, 1]`; neutralised automatically. |
| `gamepad.release_all` | none | Idempotently neutralises every control. |
| `gamepad.disconnect` | none | Releases every control and removes the virtual controller. |

Button names are `a`, `b`, `x`, `y`, `dpad_up`, `dpad_down`, `dpad_left`, `dpad_right`,
`left_bumper`, `right_bumper`, `left_stick`, `right_stick`, `menu`, and `view`.

Stable errors include `gamepad_unavailable`, `gamepad_not_connected`, `gamepad_duration_exceeded`,
`gamepad_invalid_button`, and `gamepad_invalid_direction`.

### Agent CLI

`src/GameControl.Cli` builds the standalone `game-control` executable for the `game-control` Agent
Skill. It is intentionally not a raw RPC relay: it only emits the RPC methods above and requires a
finite `--hold-ms` for every physical input.

```powershell
$env:WINDOWS_CAPTURE_EXE = (Resolve-Path 'src\WindowsCapture.App\bin\Release\net8.0-windows10.0.19041.0\windows-capture.exe')
.\src\GameControl.Cli\bin\Release\net8.0\game-control.exe status
.\src\GameControl.Cli\bin\Release\net8.0\game-control.exe press --button view --hold-ms 80
.\src\GameControl.Cli\bin\Release\net8.0\game-control.exe screenshot --count 1 --start-delay-ms 0
.\src\GameControl.Cli\bin\Release\net8.0\game-control.exe release-all
```

### Limits (defaults, reported by `get_state`)

`max_request_bytes` 1 MiB · `max_chunk_bytes` 256 KiB · `max_jobs` 32 · `max_frames_per_job` 600 ·
`max_job_result_bytes` 128 MiB · `max_interval_ms` 600000 · `max_start_delay_ms` 600000 ·
`max_edge` 8192 · `default_ttl_seconds` 300 · `max_ttl_seconds` 3600 ·
`default_frame_timeout_ms` 5000 · `max_frame_timeout_ms` 60000 ·
`max_request_registry_entries` 256 · `max_pipe_instances` 16.
`pipe_idle_timeout_ms` 30000 · `pipe_write_timeout_ms` 30000 · `sweep_interval_ms` 30000.

`max_pipe_instances` bounds how many clients are served at once: the server reserves a slot before
creating each pipe instance, so it can never create more instances than this and the accept loop
cannot fault on "all pipe instances are busy". `pipe_idle_timeout_ms` drops a connection that stays
silent between requests; `sweep_interval_ms` is how often finished jobs are swept for TTL expiry
even when no request arrives.

### Python (standard library only)

```python
import json, subprocess, time

EXE = r"C:\path\to\windows-capture.exe"
PIPE = None  # or pass --pipe on both sides

def rpc(method, params=None, request_id=None, exe=EXE):
    """One JSON-RPC call via the CLI relay: stdout is exactly one JSON document."""
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    if request_id:
        req["request_id"] = request_id
    out = subprocess.run([exe, "--rpc", json.dumps(req)],
                         capture_output=True, text=True, timeout=120)
    doc = json.loads(out.stdout)          # exit code is bounded; see README §4
    if "error" in doc:
        raise RuntimeError(doc["error"]["data"]["error_code"])
    return doc["result"]

state = rpc("get_state")
windows = rpc("list_windows")["windows"]
target = next(w for w in windows if w["title"] == "My App")

job = rpc("start", {"target": target, "count": 3, "interval_ms": 200}, request_id="job-1")
job_id = job["job_id"]

while rpc("get_job", {"job_id": job_id})["state"] in ("queued", "running"):
    time.sleep(0.1)

info = rpc("get_job", {"job_id": job_id})
for frame in info["frames"]:
    chunks, offset = [], 0
    while True:
        c = rpc("read_result", {"job_id": job_id, "index": frame["index"], "offset": offset})
        chunks.append(__import__("base64").b64decode(c["data_base64"]))
        offset += c["length"]
        if c["eof"]:
            break
    open(f"frame{frame['index']}.png", "wb").write(b"".join(chunks))

rpc("release_result", {"job_id": job_id})
```

`--rpc` autostarts a hidden host when none is running; pass `--no-autostart` to fail with exit 3
instead. A raw `\\.\pipe\<name>` client works too — the framing is just newline-delimited JSON.

---

## 6. Design notes

* **Parameter precedence:** temporary override > saved config > built-in default. A temporary
  override is never written to disk unless the request sets `save_config: true`, and even then only
  the fields the caller actually supplied are persisted.
* **Target identity:** a persisted/selected target is `HWND + PID + process start time`. Windows
  recycles HWNDs, so the start time is re-checked on every `start`; a mismatch fails with
  `target_identity_mismatch` rather than capturing a stranger's window. If the start time cannot be
  read the check degrades to `unverifiable` and the capture proceeds (reported, not hidden).
* **Session reuse:** the `GraphicsCaptureItem`, `Direct3D11CaptureFramePool`,
  `GraphicsCaptureSession`, D3D11 device and staging texture are cached per target and reused across
  frames and jobs. Changing target (or a dead HWND) disposes and rebuilds them.
* **Fresh vs reused frames.** WGC window capture is **change-driven**: a window that never
  recomposes delivers exactly one frame, and `framePool.Recreate()` does *not* force a new one
  (measured — §8). Each tick therefore waits up to `frame_timeout_ms` for a genuinely new frame:
  * new frame → `source:"fresh"`, `captured_at` = when the OS produced it;
  * nothing new → the previous frame is returned with `source:"reused"` and its **original**
    `captured_at`, never a fabricated timestamp;
  * `allow_reuse:false` (`--no-reuse`) turns that case into a `no_new_frame` failure instead;
  * no frame ever produced → `no_frame` with an explanation.
* **Minimized / closed targets** fail clearly (`target_minimized`, `target_closed`) and never fall
  back to another window or the desktop.
* **Results** live in memory (plus optional `output_dir` files); each job has a TTL and can be
  released explicitly. The tool never writes into any Synapse attachment directory.
* **Pipe server liveness.** The accept loop reserves a slot (bounded by `max_pipe_instances`)
  *before* creating each pipe instance and releases it when the connection ends, so it can never
  create more instances than the limit and never hits the "all pipe instances are busy" error that
  used to fault the loop permanently; transient accept failures back off briefly and retry instead
  of spinning hot. Each request read has an idle deadline (`pipe_idle_timeout_ms`) and each response
  a write deadline (`pipe_write_timeout_ms`), so a client that connects and then goes silent — or a
  malicious same-user client — is dropped and its slot reused.
* **Releasing a result while a job is still running** frees the payloads immediately and stops
  further retention, so the freed memory stays freed and the frame/payload indices cannot drift
  apart. The job keeps capturing and reports its terminal state as usual.
* **TTL sweeps** run on a timer (`sweep_interval_ms`) as well as on each request, so retained
  results never outlive their TTL just because nobody called in.

---

## 7. Configuration

`%LOCALAPPDATA%\WindowsCapture\config.json` (atomic write; a corrupt file degrades to defaults and
is reported via `get_state.config_error`):

```json
{
  "count": 3, "interval_ms": 250, "start_delay_ms": 0, "max_edge": 0,
  "output_dir": null, "format": "png", "ttl_seconds": 300,
  "frame_timeout_ms": 5000, "allow_reuse": true,
  "last_target": {"hwnd":"0x1A2B","hwnd_value":6699,"pid":123,"process_start_utc":"...","title":"..."}
}
```

`last_target` is a convenience only and is always re-validated before use.

---

## 8. Acceptance / verification

### Automated (all green on this machine)

```
dotnet build -c Release   ->  0 warnings, 0 errors
dotnet test  -c Release   ->  101 passed, 0 failed
```

Covered: parameter precedence and validation, target identity (verified / mismatch / unverifiable /
not requested), line framing (CRLF, pipelining, UTF-8 split across reads, oversized frames),
request canonicalisation, chunk boundary maths, idempotency registry (miss / replay / conflict /
eviction), job lifecycle (queued→running→completed), single-concurrency, cancel, TTL expiry,
release, size limits, reuse handling, disk output, and the full RPC surface (parse error, invalid
request, unknown method, target required, idempotent replay with a changed `id`, conflict, error
replay, chunked read + release, `invoke_cli`, `get_state`, `list_windows`, `set_target`).
Also covered: the lifetime regressions (releasing the result of a running job, the periodic TTL
sweep without any request, and concurrent cancel/sweep/start without racing disposal).

### Real WGC smoke test — `--self-test`

`windows-capture --self-test` first drives the real named-pipe server against itself — saturating
every pipe instance with idle clients, proving a further client cannot be served, then releasing the
clients and proving a fresh `ping` succeeds; and proving an idle client is dropped once its idle
deadline passes. It then creates **its own** throw-away window (never a user window), covers it with
a second window it also owns, captures it, and destroys both. Only pipe instances and windows this
process creates are ever touched. Observed result on this machine (Windows 11, DPI 125%):

```json
{
  "pipe_saturation":    {"ok": true, "max_instances": 3, "instances_held": true, "saturated": true,
                         "recovered": true},
  "pipe_idle_timeout":  {"ok": true, "max_instances": 2, "idle_timeout_ms": 2000, "saturated": true,
                         "recovered": true},
  "occluded_capture":   {"ok": true, "target_match_ratio": 1.0, "occluder_match_ratio": 0.0,
                         "width": 857, "height": 616, "source": "fresh"},
  "static_window_ticks":[{"tick":0,"source":"fresh","captured_at":1789828764615},
                         {"tick":1,"source":"reused","captured_at":1789828764615},
                         {"tick":2,"source":"reused","captured_at":1789828764615}],
  "repaint_capture":    {"ok": true, "second_match_ratio": 1.0, "source": "fresh"},
  "minimized":          {"ok": true, "error_code": "target_minimized"},
  "closed":             {"ok": true, "error_code": "target_closed"},
  "ok": true
}
```

That is: while fully covered by another window the target's **own** pixels were captured (occluder
ratio 0), a repainted window produced a genuinely fresh frame, a static window was honestly reported
as `reused` with its original timestamp, and minimized/closed targets failed clearly.

### Manual end-to-end checks performed

* `--help`, `--version`.
* Host started with `--show-ui`; `get_state`, `list_windows` (found the host's own GUI window),
  `set_target`.
* `--capture --hwnd <own GUI window> --count 2 --interval-ms 200 --output-dir <tmp>` → `completed`,
  2 `fresh` frames, both PNGs written (1184×792, 71/74 KB, 262 distinct sampled colours — real
  content, not a blank image).
* `read_result` chunking (64-byte chunk → 88 base64 chars, `eof:false`, then a huge `length` clamped
  to the whole frame with `eof:true`), `release_result` → `read_result` ⇒ `result_released`.
* `request_id` retry returned the identical `job_id`; a differing `request_id` param set ⇒
  `request_id_conflict`.
* `capture_busy` on overlapping starts; `cancel` ⇒ `cancelled`.
* `target_identity_mismatch` for a wrong `process_start_utc`.
* `save_config:true` wrote `count`/`interval_ms`/`last_target` to the config file.
* Second launch forwarded to the running instance and exited 0.
* `--exit` ⇒ host gone, pipe closed, `ping` then returns exit 3.
* Error paths: parse error (exit 4), `count:0` (exit 5, `invalid_params`), unknown HWND (exit 2,
  `target_not_found`), no host with `--no-autostart` (exit 3), unknown flag (exit 2).

### Not verified — stated honestly

* **No user window was ever captured for validation.** Every capture above targeted a window this
  process created itself.
* DX / OpenGL / Vulkan windows were **not** individually exercised. They are ordinary HWNDs to WGC
  and are expected to work, but this is an expectation, not a measurement.
* Multi-monitor / mixed-DPI / HDR setups, window resizing mid-job, and Windows 10 1903–21H2 were not
  tested (this machine is Windows 11). `framePool.Recreate` on a size change is implemented but only
  exercised indirectly.
* `interactive_if_needed` opens the picker but does **not** wait for a selection (§5 `start`).
* The GUI was not exercised by an automated UI driver; it was driven manually.

---

## 9. Known limitations

* One capture job at a time, per process.
* Results are held in memory (TTL-bounded) unless `output_dir` is set.
* `png` is the only output format in v1.
* Elevated/protected windows need this tool to run elevated too.
* A window that never recomposes can only be reported as `reused`; there is no way to force WGC to
  emit a new frame.
* No tray icon: closing the window hides it, and `--show-ui` / relaunching brings it back.
