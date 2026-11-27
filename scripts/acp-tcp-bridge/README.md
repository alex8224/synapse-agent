# ACP stdio ↔ TCP bridge

Two dependency-free Go binaries bypass broken `smolvm machine exec -i` stdin
forwarding. The Windows client speaks ACP on stdin/stdout; the Linux listener
starts one agent per authenticated TCP connection. Neither side invokes a shell
for agent requests. The server inherits its guest environment (including guest
proxy/account settings), not the host's environment.

## Build and test (PowerShell 7)

```powershell
pwsh -File scripts/acp-tcp-bridge/setup.ps1
Push-Location scripts/acp-tcp-bridge
go test ./... -count=1
go vet ./...
Pop-Location
```

Requires Go 1.23+. Output: `.tmp/acp-tcp-bridge/acp-tcp-bridge.exe` and
`acp-tcp-bridge-linux-amd64` (static, `CGO_ENABLED=0`). No Bun, socat, or Go
installation is needed inside the guest. Source is checkout-only, not a Python
package entry point.

## Deploy to smolvm

**Adding a port requires stopping and restarting the VM, interrupting its current
work.** The script refuses to do so without `-RestartMachine`. Choose a free host
port. Ensure the guest directory is the actual mount of `HostRoot`, not just an
unrelated directory with the same name. The script checks existence, not mount
identity. It only adds a workspace mount when `-MountWorkspace` is explicitly set
alongside `-RestartMachine`; this grants the guest read/write access to HostRoot.
Choose an unused guest path for a new mount (for example `/synapse`).

```powershell
# Explicitly authorize one VM stop/update/start cycle:
pwsh -File scripts/acp-tcp-bridge/setup.ps1 -Deploy -RestartMachine `
  -Machine agy-box -Port 19091 `
  -HostRoot 'F:\project\agent\autoagents\synapse' -GuestRoot /workspace

# If no suitable mount exists, explicitly add one during that restart:
pwsh -File scripts/acp-tcp-bridge/setup.ps1 -Deploy -RestartMachine -MountWorkspace `
  -Machine agy-box -Port 19091 `
  -HostRoot 'F:\project\agent\autoagents\synapse' -GuestRoot /synapse

# Only when this exact host->guest port is already published on loopback:
pwsh -File scripts/acp-tcp-bridge/setup.ps1 -Deploy -PortAlreadyPublished `
  -Machine agy-box -Port 19091 `
  -HostRoot 'F:\project\agent\autoagents\synapse' -GuestRoot /workspace
```

Deployment:

1. Builds both binaries; optionally stops the VM, adds `19091:19091`, restarts it.
2. Creates a random shared token in the host output directory restricted to the
   current Windows user, copies it to a guest directory with mode 700 (token 600).
   The secret is never printed or put on a command line.
3. Starts the listener detached in the guest using `machine exec --detach`;
   interactive stdin forwarding is not used.
4. Performs a real ACP v1 `initialize` through the Windows client. It does **not**
   create a cloud-backed session, submit a prompt, or change the runtime catalog.
5. Writes `.tmp/acp-tcp-bridge/external-acp-agents.example.json`; merge only its
   `agy-1` entry into the existing runtime `external-acp-agents.json`.

The agent is `/root/.local/bin/agy acp --dangerously-skip-permissions` by default.
Change `-Agent` if necessary. A failed smoke test leaves the server running for
inspection; it does not claim deployment succeeded. On repeated deployment the
script refuses to overwrite a running server. Stop the recorded bridge PID first.

The listener is not an auto-start service. After a VM restart, use the detached
launch below or redeploy with `-PortAlreadyPublished` (the PID-file check fails
closed if a stale PID has been reused; verify/remove that stale file manually).

## Manual operation

Generate the same strong token file on both sides by a secure file-copy path.
Keep it private; do not use a literal token argument or environment variable.

Guest:

```sh
/root/.local/share/synapse-acp-bridge-19091/acp-tcp-bridge serve \
  --listen 0.0.0.0:19091 --allow-remote \
  --token-file /root/.local/share/synapse-acp-bridge-19091/token \
  --workdir /workspace -- /root/.local/bin/agy acp --dangerously-skip-permissions
```

Host:

```powershell
& .tmp/acp-tcp-bridge/acp-tcp-bridge.exe client `
  --connect 127.0.0.1:19091 --token-file .tmp/acp-tcp-bridge/token `
  --host-root 'F:\project\agent\autoagents\synapse' --guest-root /workspace
```

`serve` binds loopback unless `--allow-remote` is explicitly set. A guest wildcard
listener is necessary for VM port publishing; **publish only on host loopback**,
restrict any other guest access, and use only trusted private VM networking. The
TCP stream is authenticated but **not encrypted**: this is not safe for LAN or
Internet exposure. For untrusted networks use an authenticated encrypted tunnel.
Full-access ACP is effectively remote code execution in the agent's account.

## Protocol and lifecycle

- A bounded token line and `OK\n` acknowledgement precede the byte stream. These
  never enter either ACP endpoint. Unauthenticated connections never spawn agents.
- No message rewriting unless both mapping roots are set. With mapping enabled,
  newline-delimited JSON is bounded to 50 MiB per frame; only `session/new` and
  `session/load` `params.cwd` change. Out-of-root/traversal paths fail closed.
- Mapping does not mount files. It does not translate MCP executable paths, prompt
  file paths, or agent-returned absolute paths. Configure guest-compatible MCP
  commands separately; use text-only/no-MCP smoke tests first.
- TCP disconnect / host stdin EOF kills the Linux agent process group, including
  subprocesses. EOF means session disposal, not "finish queued requests". Keep
  stdin open until replies have arrived. A remote exit terminates the host adapter
  even if host stdin remains open. No reconnect/replay or transparent new session.
- Maximum 16 concurrent connections (including authentication); configurable with
  `serve --max-clients`. Authentication deadline 5 seconds; client connect/auth
  timeout defaults to 5 seconds. Existing Synapse ACP deadlines still apply.
- Bridge diagnostics only go to stderr. Child stderr is deliberately discarded
  to prevent accidental credential disclosure; no ACP payloads are logged.
- Linux is the supported server target (process-group cleanup); Windows is the
  supported host target. The Windows server fallback does not kill grandchildren.
- No server autostart, TLS, log streaming, or automatic runtime-catalog mutation.

## Stop / inspect

Deployment records the guest listener PID under
`/root/.local/share/synapse-acp-bridge-19091/server.pid`. Stop only that verified
process, not all `agy` processes. SIGTERM closes the listener/connections and kills
its owned agent groups. The protected `server.log` contains bridge metadata only;
agent stderr and ACP data are not persisted. Stop the bridge before removing its
binary, token, or port mapping. Removing the port mapping also requires a stopped
VM; do not restart a running machine without explicit authorization.

### smolvm 1.37.7 Windows runtime socket failure

Observed on this host: both normal stop and `stop --force` can fail with Windows
error 1920. After explicitly authorized termination of the verified VM process,
AF_UNIX reparse-point entries (`agent.logs`, `agent.ready`, `agent.sock`,
`port-forward.sock`, `ports.sock`) remained in the machine data directory and
blocked startup. Preserving those entries under different names allowed startup.
The separate stale `control.sock` also caused update to report `Unreachable` even
though status said stopped; preserving it allowed update to succeed.

This is external smolvm runtime state, not ACP protocol state. The setup script
deliberately does not force-kill VMs or alter socket state automatically. Only
after explicit force-stop authorization, verify the exact VM PID/executable,
ensure it has exited, and preserve only its runtime endpoints; never remove or
rename machine configuration, disk images, or live endpoints.
