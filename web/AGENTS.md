# Synapse Web Console — Collaboration Guide

Frontend conventions for `web/`. The repository-wide guide is the root `AGENTS.md`
(domain packages, Python/Rust, release process); this file only covers the browser
console. Note that the agent-md middleware (`src/synapse/app/agent_md.py`) injects
only a project-root `AGENTS.md`, so when working from the repository root this file
is a reference, not an automatically injected instruction block.

## Architecture

- `web/` is the React 19 + TypeScript + Vite console frontend for the runtime daemon. It builds to `web/dist/`, a standalone build asset that is **not** packaged in the Python wheel; deployment points at it explicitly (`synapse-web-console --static-dir dist`). Host/runtime docs live in `docs/web-console/`; the user-facing layout description is `web/README.md`.
- `src/runtime-client/` is the DOM-free protocol core: host coupling goes through an injected `SocketLike`, and only the default implementation names `WebSocket`. `src/client/` holds the browser-side host HTTP surface (`bootstrap.ts` pairing/session, `runtimeStatus.ts` diagnostics, `deepLink.ts` launch params) plus `export *` shims for modules that moved into the core (`artifacts`, `artifactsDiff`, `recoverability`, `SynapseRuntimeClient`, `types`) — new protocol code goes in `src/runtime-client/`, and the shims stay so existing import paths keep resolving.
- That boundary is machine-checked: `tsconfig.runtime-client.json` type-checks `src/runtime-client` with `lib: ["ES2023"]`, no DOM lib, `strict`, so a stray `window`/`document`/`WebSocket` fails there instead of silently depending on the browser. `tests/runtimeClientBoundary.test.ts` guards the same boundary.
- `src/runtime-client/contract.generated.ts` is generated from `src/synapse/runtime/service/contract_registry.py` by `scripts/export_contract_manifest.py` (same source as `src/synapse/runtime/service/contract_manifest.json`). Never hand-write wire DTOs; `types.ts` only re-exports the generated shapes.
- Components live in `src/components/`. The bottom bar is a "static manifest + item module" host: a new entry is one `src/components/bottomBar/*Item.tsx` module plus one line in `src/components/bottomBar/manifest.tsx`. The host owns the shared rules (three-track grid, single `openId`, popover/modal ownership, narrow-screen `keep`/`compact`/`more` policy) and is not edited per entry; keyboard entries come from `src/components/consoleShortcuts.ts`.
- Zustand stores live in `src/stores/`. Entry-local lifecycles (e.g. `codexUsage`) stay out of `useConsoleStore`, and per-row view state (folding, measured heights) stays local so it cannot rebuild the transcript.
- `src/markdown/` is the render pipeline (parse, highlight, mermaid, tex, ansi, file paths, image refs).
- There is no router: the only URL is the origin (`/runtime-ws` is derived from it), and `?action=new-session` is the single shortcut action.
- Layout is a two-column shell in `src/App.tsx`; shared geometry lives in `src/index.css` (`.console-gutter`, `.console-column`, `.no-scrollbar`). The invariants are described in prose in `web/README.md` and pinned by static guards.
- `get_runtime_config.py` and `get_transcript.py` are dev-only debug helpers; run them from the repository root with `uv run --no-sync python web/get_runtime_config.py`.

## Coding standards

- TypeScript + React only. oxlint is the only automated baseline (`npm run lint`); `react/rules-of-hooks` is an error. There is no formatter config — match the surrounding file (2-space indent; `src/**` uses semicolons and relative imports carry the `.ts`/`.tsx` extension).
- The console holds **no credential path**: no token file, no `Authorization` header, no credential in a URL or in the socket URL, no credential in browser storage, no pairing code from the environment. `tests/sourceGuard.test.ts` asserts this statically over `src/**` + `vite.config.ts`; the only browser-storage exemptions are the non-secret `transcriptCache` and `appearance` stores.
- Vite is development-only: hot reload plus a controlled proxy for `/api` and `/runtime-ws`. The proxy targets only a loopback console host, rewrites `Origin`, never injects credentials, and keeps `server.host` on loopback. Do not add business logic or an auth bypass there.
- Keep browser APIs out of `src/runtime-client/`; DOM access belongs to components and stores.
- Comment the *why* — invariants, state transitions, and layout constraints — as the existing modules do; a guard test usually pins the same rule.
- Update `web/README.md` when console-visible behavior changes (layout, shortcuts, panels, PWA surface) and `docs/web-console/` when host/runtime behavior changes.

## Development & testing

Node >= 22.18 (CI pins 24) strips TypeScript natively, so `node --test` runs the `.ts` sources with no transpiler step.

```powershell
cd web
npm ci
npm run lint          # oxlint (local only; CI runs ruff, not oxlint)
npx tsc -b            # type-check all three tsconfig projects
npm test              # node --test "tests/*.test.ts"
npm run build         # tsc -b && vite build -> dist/
```

Verification order: narrowest test file → related guards → full `npm test`.

```powershell
cd web
node --test tests/bottomBarContract.test.ts
node --test tests/transcriptLabels.test.ts tests/transcriptLayoutGuard.test.ts
```

`*.test.ts` are the fast regression net. The `*Guard.test.ts` / `sourceGuard.test.ts` / `*LayoutGuard` files pin source-level contracts (geometry, regions, ordering, forbidden APIs) — change them deliberately, not incidentally. CI's `contract` job only type-checks and runs `runtimeContractFixture` / `runtimeClientBoundary` / `sourceGuard`, so run the files related to your change locally.

`*.verify.ts` are real-browser acceptance scripts. They are **not** matched by `npm test`; run them explicitly:

```powershell
cd web
$env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''
node --test tests/shellLayout.verify.ts
```

They launch headless Chrome/Edge through `tests/helpers/cdp.ts` (`SYNAPSE_CDP_BROWSER` overrides the executable) and each brings up what it needs — its own `synapse-web-console` host or a fixture server. Use them when a change alters rendered geometry, focus/keyboard behavior, or virtualized scrolling: a green static guard does not prove a flex chain still stretches.

| Change area | Preferred tests |
| --- | --- |
| bottom bar / shortcuts | `tests/bottomBarContract.test.ts`, `bottomBarLayout.test.ts`, `bottomBarDismiss.test.ts`, `bottomBarInteraction.verify.ts`, `bottomBarMore.verify.ts` |
| shell / top bar / transcript layout | `tests/shellLayout.test.ts`, `topBarLayout.test.ts`, `transcriptLayoutGuard.test.ts`, `transcriptScrollGuard.test.ts`, plus the matching `.verify.ts` |
| transcript view model | `tests/historyMapper.test.ts`, `liveEventReducer.test.ts`, `liveDeltaBatch.test.ts`, `transcriptLabels.test.ts`, `transcriptVirtualization.test.ts` |
| markdown / math / mermaid / images | `tests/markdownParser.test.ts`, `markdownRenderGuard.test.ts`, `markdownTableGuard.test.ts`, `mathBlocks.test.ts`, `mathRender.test.ts`, `mermaidPolicy.test.ts`, `imageRefs.test.ts` |
| runtime client / wire contract | `tests/runtimeContractFixture.test.ts`, `runtimeClientBoundary.test.ts`, `runtimeClientReconcile.test.ts`, `runtimeClientRecovery.test.ts`, `sourceGuard.test.ts` |
| stores / views | the `tests/<name>.test.ts` matching the store (`goalView`, `mcpRuntimeView`, `usageView`, `todoView`, `sessionList`, `appearance`, ...) |
| PWA / window controls overlay | `tests/pwaManifest.test.ts`, `tests/windowControlsOverlay.test.ts` |

Contract changes (wire types, versions, capability lists) go through the generator first:

```powershell
# from the repository root
uv run --no-sync python scripts/export_contract_manifest.py         # regenerate both artifacts
uv run --no-sync python scripts/export_contract_manifest.py --check # fail on drift
# then from web/
npx tsc -b
node --test tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts
```

The cross-language fixtures live in `tests/fixtures/runtime_contract/` and are consumed by both the Python and the TypeScript side.

PWA icons under `public/` are committed and have no generation script; after regenerating them run `npm test` — `tests/pwaManifest.test.ts` reads the real PNG headers and fails on a wrong size or a non-root-path reference.
