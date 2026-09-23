/**
 * Browser verification for the status strip's interaction contract.
 *
 * The static guards (`bottomBarLayout.test.ts`, `bottomBarDismiss.test.ts`) pin
 * the wiring and `bottomBarContract.test.ts` renders the tracks; neither can see
 * what a *real* browser does with a keypress, a click outside a portalled panel,
 * a modal's focus round trip, or a phone-band window.  This script measures the
 * rendered strip in headless Chrome against a real `synapse-web-console` host:
 *
 *  1. the strip renders its tracks and every entry in them,
 *  2. F1 / F5 / F6 open their overlays, the same key closes, and one open id
 *     means another key replaces the open overlay (never two at once),
 *  3. key repeat is ignored,
 *  4. a popover hangs above the strip, and a click outside *its own trigger and
 *     panel* closes it — including a click elsewhere in the strip,
 *  5. a modal owns its scrim and its Escape, and hands focus back,
 *  6. the phone band keeps every entry reachable: the strip is one row, no track
 *     is hidden, and the compact metrics chip opens the complete list,
 *  7. switching sessions closes the open overlay.
 *
 * No runtime daemon is needed: pairing alone renders the workspace shell (the
 * transcript stays empty and the read-only diagnostics banner may appear).  The
 * host's synthetic token file lives in a temp dir, is never printed and is
 * removed with it.
 *
 * Run:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/bottomBarInteraction.verify.ts
 */
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import type { AddressInfo } from 'node:net'
import net from 'node:net'
import { CdpClient, closeBrowser, evaluate, launchBrowser, openPage } from './helpers/cdp.ts'
import type { BrowserHandle, PageHandle } from './helpers/cdp.ts'
import { httpProbe } from './helpers/httpProbe.ts'
import { stripAnsi } from './helpers/stripAnsi.ts'

const WEB_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(WEB_ROOT, '..')

const failures: string[] = []
function check(label: string, actual: unknown, expected: unknown): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  if (!ok) failures.push(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label} = ${JSON.stringify(actual)}`)
}

/** Whether an element matches a selector right now. */
async function exists(client: CdpClient, page: PageHandle, selector: string): Promise<boolean> {
  return (
    (await evaluate(client, page, `document.querySelector(${JSON.stringify(selector)}) !== null`)) ===
    true
  )
}

function note(label: string, detail: string): void {
  console.log(`  [SKIP] ${label}: ${detail}`)
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.on('error', reject)
    probe.listen(0, '127.0.0.1', () => {
      const port = (probe.address() as AddressInfo).port
      probe.close(() => resolve(port))
    })
  })
}

/** Terminate the host and anything it spawned (the runtime daemon included). */
function killTree(pid: number | undefined): void {
  if (pid === undefined) return
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
    return
  }
  try {
    process.kill(-pid, 'SIGKILL')
  } catch {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      /* already gone */
    }
  }
}

async function waitFor(
  client: CdpClient,
  page: PageHandle,
  expression: string,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if ((await evaluate(client, page, expression)) === true) return
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(`timed out waiting for: ${expression}`)
}

/**
 * The keys the strip answers to, with their virtual key codes.
 *
 * A real key event (not a synthetic `dispatchEvent`) is what makes this
 * faithful: the browser delivers it to the focused element and it bubbles to
 * `document` and `window`, which is where the strip's listeners live.
 */
const KEYS: Record<string, { code: string; vk: number }> = {
  F1: { code: 'F1', vk: 112 },
  F5: { code: 'F5', vk: 116 },
  F6: { code: 'F6', vk: 117 },
  Escape: { code: 'Escape', vk: 27 },
}

/** Press a key, then let the overlay's entrance animation settle. */
async function pressKey(
  client: CdpClient,
  page: PageHandle,
  key: string,
  repeat = false,
): Promise<void> {
  const spec = KEYS[key]
  if (spec === undefined) throw new Error(`unknown key ${key}`)
  const common = {
    key,
    code: spec.code,
    windowsVirtualKeyCode: spec.vk,
    nativeVirtualKeyCode: spec.vk,
  }
  await client.send(
    'Input.dispatchKeyEvent',
    { ...common, type: 'rawKeyDown', autoRepeat: repeat },
    page.sessionId,
  )
  await client.send('Input.dispatchKeyEvent', { ...common, type: 'keyUp' }, page.sessionId)
  await new Promise((resolve) => setTimeout(resolve, 450))
}

/** Centre of an element, or `null` when it is not on screen. */
function centreOf(selector: string): string {
  return `(() => {
    const el = document.querySelector(${JSON.stringify(selector)})
    if (el === null) return null
    const rect = el.getBoundingClientRect()
    if (rect.width === 0 || rect.height === 0) return null
    return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) }
  })()`
}

/** A real mouse click at the element's centre (so `mousedown` really fires). */
async function click(client: CdpClient, page: PageHandle, selector: string): Promise<boolean> {
  const centre = (await evaluate(client, page, centreOf(selector))) as { x: number; y: number } | null
  if (centre === null) return false
  for (const type of ['mousePressed', 'mouseReleased']) {
    await client.send(
      'Input.dispatchMouseEvent',
      { type, x: centre.x, y: centre.y, button: 'left', clickCount: 1 },
      page.sessionId,
    )
  }
  await new Promise((resolve) => setTimeout(resolve, 150))
  return true
}

async function setViewport(client: CdpClient, page: PageHandle, width: number, height: number) {
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width, height, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  )
  await new Promise((resolve) => setTimeout(resolve, 400))
}

/** What the strip currently shows, plus which overlay (if any) is up. */
const STRIP = `(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect()
    return { left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top), bottom: Math.round(r.bottom), width: Math.round(r.width), height: Math.round(r.height) }
  }
  const footer = document.querySelector('footer')
  if (footer === null) return null
  const track = (name) => {
    const el = footer.querySelector('[data-region="' + name + '"]')
    if (el === null) return null
    return {
      display: getComputedStyle(el).display,
      entries: [...el.querySelectorAll('[data-entry]')].map((node) => node.getAttribute('data-entry')),
      text: el.innerText.replace(/\\n+/g, ' ').trim(),
      separators: el.querySelectorAll('[data-separator]').length,
      rect: rect(el),
    }
  }
  // A modal portals its scrim (the dialog is inside it), a popover portals the
  // panel itself; the mobile drawer is a dialog too, so it must not be picked up.
  const dialog = document.querySelector('body [role="dialog"][aria-modal="true"]') ??
    document.querySelector('body > [role="dialog"]')
  return {
    footer: rect(footer),
    display: getComputedStyle(footer).display,
    left: track('left'),
    centre: track('center'),
    right: track('right'),
    entries: [...footer.querySelectorAll('[data-entry]')].map((node) => node.getAttribute('data-entry')),
    entryBoxes: Object.fromEntries(
      [...footer.querySelectorAll('[data-entry]')].map((node) => [node.getAttribute('data-entry'), rect(node)]),
    ),
    buttons: [...footer.querySelectorAll('button')].map((node) => ({
      text: node.innerText.replace(/\\n+/g, ' ').trim(),
      title: node.getAttribute('title'),
      expanded: node.getAttribute('aria-expanded'),
    })),
    overlay: dialog === null ? null : {
      label: dialog.getAttribute('aria-label'),
      modal: dialog.getAttribute('aria-modal'),
      rect: rect(dialog),
      text: dialog.innerText.replace(/\\n+/g, ' | ').slice(0, 240),
    },
    activeElement: document.activeElement === null ? null : {
      tag: document.activeElement.tagName,
      title: document.activeElement.getAttribute('title'),
      text: (document.activeElement.innerText ?? '').replace(/\\n+/g, ' ').trim().slice(0, 40),
    },
  }
})()`

interface Strip {
  footer: { left: number; right: number; top: number; bottom: number; width: number; height: number }
  display: string
  left: Track | null
  centre: Track | null
  right: Track | null
  entries: string[]
  entryBoxes: Record<string, Track['rect']>
  buttons: Array<{ text: string; title: string | null; expanded: string | null }>
  overlay: {
    label: string | null
    modal: string | null
    rect: { left: number; right: number; top: number; bottom: number }
    text: string
  } | null
  activeElement: { tag: string; title: string | null; text: string } | null
}

interface Track {
  display: string
  entries: string[]
  text: string
  separators: number
  rect: { left: number; right: number; top: number; bottom: number; width: number; height: number }
}

const MCP_PANEL = '[role="dialog"][aria-label="MCP 工具与服务器"]'
const HELP_PANEL = '[role="dialog"][aria-label="快捷键与使用帮助"]'
const GOAL_PANEL = '[role="dialog"][aria-label="目标 (Goal)"]'
const METRICS_PANEL = '[role="dialog"][aria-label="本轮与会话指标"]'

/**
 * The selected session row's identity.
 *
 * The row's `title` is `${title}\n${thread_id}`, so it changes exactly when the
 * console attaches to another session — a real identity, not just "something
 * disappeared".
 */
const CURRENT_SESSION = `(() => {
  const row = document.querySelector('nav li.ui-nav-row > button[aria-current="page"]')
  return row === null ? null : row.getAttribute('title')
})()`

async function main(): Promise<void> {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bottom-bar-'))
  const tokenFile = path.join(tempDir, 'token')
  // Synthetic fixture: never a real credential, never printed, removed with the dir.
  fs.writeFileSync(tokenFile, 'bottom-bar-verification-fixture', { mode: 0o600 })

  console.log('=== status strip interactions (real host, headless browser) ===')
  console.log(`workspace      : ${REPO_ROOT}`)
  console.log(`static dir     : ${path.join(WEB_ROOT, 'dist')}`)
  console.log('')

  const host = spawn(
    'uv',
    [
      'run', '--no-sync', 'python', '-m', 'synapse.web_console.entry',
      '--workspace', REPO_ROOT,
      '--static-dir', path.join(WEB_ROOT, 'dist'),
      '--port', '0',
      '--host', '127.0.0.1',
      '--state-dir', tempDir,
      '--token-file', tokenFile,
      '--runtime-port', String(await freePort()),
    ],
    {
      cwd: REPO_ROOT,
      env: { ...process.env, NODE_OPTIONS: '' },
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: process.platform !== 'win32',
    },
  )

  let stdout = ''
  let stderr = ''
  let origin = ''
  let pairingCode = ''
  host.stdout?.on('data', (chunk: Buffer) => {
    stdout += chunk.toString()
    const line = stdout.split('\n').find((entry) => entry.trim().startsWith('{'))
    if (line !== undefined && origin === '') {
      origin = (JSON.parse(line) as { url: string }).url.replace(/\/$/, '')
    }
  })
  host.stderr?.on('data', (chunk: Buffer) => {
    stderr += stripAnsi(chunk.toString())
    const match = /pairing code ([0-9A-Z]{8})/.exec(stderr)
    if (match !== null) pairingCode = match[1]
  })

  let browser: BrowserHandle | undefined
  let client: CdpClient | undefined
  try {
    const deadline = Date.now() + 90_000
    while ((origin === '' || pairingCode === '') && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 300))
    }
    if (origin === '' || pairingCode === '') {
      throw new Error(`host did not announce itself (stdout=${stdout.trim()} stderr=${stderr.trim()})`)
    }
    console.log(`host origin    : ${origin}`)
    console.log('')

    browser = await launchBrowser()
    const version = JSON.parse(
      (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
    ) as { webSocketDebuggerUrl: string }
    client = await CdpClient.connect(version.webSocketDebuggerUrl)

    const page = await openPage(client, `${origin}/`)
    await waitFor(client, page, `document.querySelector('#pairing-code') !== null`)
    await evaluate(
      client,
      page,
      `(() => {
        const input = document.querySelector('#pairing-code')
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
        setter.call(input, '${pairingCode}')
        input.dispatchEvent(new Event('input', { bubbles: true }))
        return true
      })()`,
    )
    await waitFor(
      client,
      page,
      `(() => {
        const button = document.querySelector('#pairing-code')?.closest('form')?.querySelector('button[type=submit]')
        return button !== undefined && button !== null && button.disabled === false
      })()`,
    )
    await evaluate(client, page, `document.querySelector('#pairing-code').closest('form').requestSubmit()`)
    await waitFor(client, page, `document.querySelector('#console-composer') !== null`)
    await setViewport(client, page, 1440, 900)
    await new Promise((resolve) => setTimeout(resolve, 1500))

    // --- 1. the strip renders its manifest --------------------------------
    const wide = (await evaluate(client, page, STRIP)) as Strip
    console.log('--- 1440x900 ---')
    console.log(JSON.stringify({ footer: wide.footer, entries: wide.entries, entryBoxes: wide.entryBoxes }, null, 2))
    check('the strip is the wide three-track grid', wide.display, 'grid')
    check('the strip starts right of the sidebar', wide.footer.left > 0, true)
    check('the strip ends at the bottom edge', wide.footer.bottom, 900)
    check('the activity entry is painted', /空闲|运行中/.test(wide.left?.text ?? ''), true)
    check('the MCP entry is painted', (wide.left?.text ?? '').includes('mcp:'), true)
    check('the goal entry is painted', (wide.left?.text ?? '').includes('goal:'), true)
    check('the strip paints the manifest entries', wide.entries, ['activity', 'mcp', 'goal', 'workflow', 'telemetry'])
    check('the right track paints no control (F1 is keyboard-only)', wide.right?.entries ?? [], [])
    check('the centre track carries the telemetry entry', wide.centre?.entries, ['telemetry'])
    check('the left track separates its entries', wide.left?.separators, 3)
    check('the left entries are the manifest', wide.left?.entries.length, 4)

    // --- 2. F1 / F5 / F6, one open id -------------------------------------
    await pressKey(client, page, 'F1')
    const help = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- F1 ---')
    console.log(JSON.stringify(help.overlay, null, 2))
    check('F1 opens the help modal', help.overlay?.label, '快捷键与使用帮助')
    check('the help modal is a modal', help.overlay?.modal, 'true')
    check('the help list comes from the shortcut table', help.overlay?.text.includes('MCP 服务器'), true)
    check('the help list prints the F1 row', help.overlay?.text.includes('打开快捷键帮助'), true)
    check('the help list prints the F6 row', help.overlay?.text.includes('目标管理'), true)
    check('the help modal is the portalled one', await exists(client, page, HELP_PANEL), true)

    await pressKey(client, page, 'F1')
    const helpClosed = (await evaluate(client, page, STRIP)) as Strip
    check('the same key closes it', helpClosed.overlay, null)

    await pressKey(client, page, 'F5')
    const mcp = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- F5 ---')
    console.log(JSON.stringify({ overlay: mcp.overlay, entryBoxes: mcp.entryBoxes }, null, 2))
    check('F5 opens the MCP popover', mcp.overlay?.label, 'MCP 工具与服务器')
    check('the MCP popover is the portalled panel', await exists(client, page, MCP_PANEL), true)
    check('the MCP popover is not a modal', mcp.overlay?.modal, null)
    check('the popover hangs above the strip', (mcp.overlay?.rect.bottom ?? 0) <= mcp.footer.top, true)
    check(
      'the portalled popover stays inside the window',
      (mcp.overlay?.rect.left ?? -1) >= 0 &&
        (mcp.overlay?.rect.right ?? 9999) <= 1440 &&
        (mcp.overlay?.rect.top ?? -1) >= 0,
      true,
    )
    check('the trigger reports itself expanded', mcp.buttons.some((b) => b.expanded === 'true'), true)

    // A click on the strip *outside* the trigger and the panel closes it.
    await click(client, page, 'footer [data-region="left"] [data-entry="activity"]')
    const afterStripClick = (await evaluate(client, page, STRIP)) as Strip
    check('a click elsewhere in the strip closes the popover', afterStripClick.overlay, null)

    // ...and so does a click on the transcript.
    await pressKey(client, page, 'F5')
    await click(client, page, 'main')
    const afterOutside = (await evaluate(client, page, STRIP)) as Strip
    check('a click outside the strip closes the popover', afterOutside.overlay, null)

    // A click *inside* the panel keeps it open.
    await pressKey(client, page, 'F5')
    const panelHit = await click(client, page, `${MCP_PANEL} button`)
    const afterPanelClick = (await evaluate(client, page, STRIP)) as Strip
    check('a click inside the panel keeps it open', afterPanelClick.overlay?.label, 'MCP 工具与服务器')
    check('the panel really has a control to click', panelHit, true)

    // Holding the key down must not flap the panel.
    await pressKey(client, page, 'F5')
    const closedByToggle = (await evaluate(client, page, STRIP)) as Strip
    check('the same key closes the popover', closedByToggle.overlay, null)
    await pressKey(client, page, 'F5', true)
    const afterRepeat = (await evaluate(client, page, STRIP)) as Strip
    check('a repeated keydown is ignored', afterRepeat.overlay, null)
    // Ignored, but still the strip's: the browser default must never run, or a
    // held F5 would reload the page.  A synthetic event reads `defaultPrevented`
    // synchronously — the exact signal a real hold is judged by — without
    // depending on listener order or on a reload actually happening.
    const repeatedPrevented = await evaluate(
      client,
      page,
      `(() => {
        const event = new KeyboardEvent('keydown', { key: 'F5', repeat: true, bubbles: true, cancelable: true })
        window.dispatchEvent(event)
        return event.defaultPrevented
      })()`,
    )
    check('a repeated F5 is still prevented (a held key cannot reload the page)', repeatedPrevented, true)
    const unrelatedPrevented = await evaluate(
      client,
      page,
      `(() => {
        const event = new KeyboardEvent('keydown', { key: 'F9', bubbles: true, cancelable: true })
        window.dispatchEvent(event)
        return event.defaultPrevented
      })()`,
    )
    check('a key no entry claims is not intercepted', unrelatedPrevented, false)
    await pressKey(client, page, 'F5')
    await pressKey(client, page, 'F6')
    const replaced = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- F5 then F6 ---')
    console.log(JSON.stringify(replaced.overlay, null, 2))
    check('another key replaces the open overlay', replaced.overlay?.label, '目标 (Goal)')
    check('only one overlay is ever up', replaced.overlay?.modal, 'true')
    check('the goal dialog is the portalled one', await exists(client, page, GOAL_PANEL), true)
    check(
      'the strip never renders two overlays at once',
      await evaluate(client, page, `document.querySelectorAll('body [role="dialog"]').length`),
      1,
    )

    // --- 3. a modal owns its scrim, Escape and focus ----------------------
    await pressKey(client, page, 'Escape')
    const escaped = (await evaluate(client, page, STRIP)) as Strip
    check('Escape closes the modal', escaped.overlay, null)
    // Opening it from its own trigger hands focus back to that trigger.
    const triggerHit = await click(client, page, 'footer [data-entry="goal"]')
    const openedByClick = (await evaluate(client, page, STRIP)) as Strip
    check('the goal trigger opens the dialog', openedByClick.overlay?.label, '目标 (Goal)')
    check('the trigger really was clicked', triggerHit, true)
    await pressKey(client, page, 'Escape')
    const focusBack = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- modal focus ---')
    console.log(JSON.stringify({ before: openedByClick.activeElement, after: focusBack.activeElement }, null, 2))
    check('Escape returns focus to the trigger', /设置目标|目标/.test(focusBack.activeElement?.title ?? ''), true)

    // --- 4. the phone band keeps everything reachable ---------------------
    await setViewport(client, page, 360, 740)
    const phone = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- 360x740 ---')
    console.log(JSON.stringify({ footer: phone.footer, display: phone.display, left: phone.left, centre: phone.centre, right: phone.right }, null, 2))
    check('the phone strip is one row', phone.display, 'flex')
    check('the left track is painted', (phone.left?.entries.length ?? 0) >= 3, true)
    check('the left track is not hidden', phone.left?.display !== 'none', true)
    check('the centre track is painted (a compact chip, not clipped)', (phone.centre?.entries.length ?? 0) >= 1, true)
    check('the centre track is not hidden', phone.centre?.display !== 'none', true)
    check('the strip is a touch-height row', phone.footer.height >= 44, true)
    check('the strip still ends at the bottom edge', phone.footer.bottom, 740)
    check('the compact chip opens the complete metrics', await click(client, page, 'footer [data-entry="telemetry"]'), true)
    const metrics = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- compact metrics ---')
    console.log(JSON.stringify(metrics.overlay, null, 2))
    check('the metrics popover opens', metrics.overlay?.label, '本轮与会话指标')
    check('the metrics popover is the portalled one', await exists(client, page, METRICS_PANEL), true)
    check('the metrics popover stays on screen', (metrics.overlay?.rect.left ?? -1) >= 0 && (metrics.overlay?.rect.right ?? 9999) <= 360, true)
    check('the full metric list is reachable', metrics.overlay?.text.includes('本轮与会话指标'), true)
    await pressKey(client, page, 'Escape')
    const metricsClosed = (await evaluate(client, page, STRIP)) as Strip
    check('Escape closes the compact popover', metricsClosed.overlay, null)

    // F5 still works in the phone band, and its panel fits the window.
    await pressKey(client, page, 'F5')
    const phoneMcp = (await evaluate(client, page, STRIP)) as Strip
    console.log('')
    console.log('--- phone F5 ---')
    console.log(JSON.stringify(phoneMcp.overlay, null, 2))
    check('F5 opens the panel in the phone band', phoneMcp.overlay?.label, 'MCP 工具与服务器')
    check(
      'the panel fits the phone window',
      (phoneMcp.overlay?.rect.left ?? -1) >= 0 && (phoneMcp.overlay?.rect.right ?? 9999) <= 360,
      true,
    )
    await pressKey(client, page, 'Escape')

    // --- 5. a session switch closes the open overlay ----------------------
    await setViewport(client, page, 1440, 900)
    const sessions = (await evaluate(
      client,
      page,
      `(() => {
        // The session rows: the title button of each tree row (the rename /
        // delete buttons carry an aria-label, the title button does not).
        const rows = [...document.querySelectorAll('nav li.ui-nav-row > button:not([aria-label])')]
        const other = rows.find((node) => node.getAttribute('aria-current') !== 'page')
        return { count: rows.length, hasOther: other !== undefined }
      })()`,
    )) as { count: number; hasOther: boolean }
    console.log('')
    console.log('--- session switch ---')
    console.log(JSON.stringify(sessions, null, 2))
    if (sessions.hasOther) {
      // A modal, not a popover: an outside click would close a popover anyway,
      // so only the store subscription can be what closes this one.
      await pressKey(client, page, 'F6')
      const before = (await evaluate(client, page, STRIP)) as Strip
      const identityBefore = (await evaluate(client, page, CURRENT_SESSION)) as string | null
      const switched = await click(
        client,
        page,
        'nav li.ui-nav-row > button:not([aria-label]):not([aria-current="page"])',
      )
      await new Promise((resolve) => setTimeout(resolve, 800))
      const after = (await evaluate(client, page, STRIP)) as Strip
      const identityAfter = (await evaluate(client, page, CURRENT_SESSION)) as string | null
      console.log('')
      console.log('--- session switch identity ---')
      console.log(JSON.stringify({ before: identityBefore, after: identityAfter }, null, 2))
      check('a session row was clickable', switched, true)
      // An overlay disappearing proves nothing on its own — an outside click, an
      // error, or the row merely re-rendering could do it.  Assert the console
      // really attached to another session (the selected row's identity changed)
      // *and* that the modal belonging to the previous session is gone.
      check(
        'the switch really moved to another session',
        identityBefore !== null && identityAfter !== null && identityBefore !== identityAfter,
        true,
      )
      check(
        'the session switch closes the open modal',
        before.overlay?.label === '目标 (Goal)' && after.overlay === null,
        true,
      )
    } else {
      note('session switch', `no second session to switch to (rows=${sessions.count})`)
    }
  } finally {
    if (browser !== undefined) await closeBrowser(browser)
    killTree(host.pid)
    host.kill()
    fs.rmSync(tempDir, { recursive: true, force: true })
  }

  console.log('')
  if (failures.length > 0) {
    console.log(`FAILED (${failures.length}):`)
    for (const failure of failures) console.log(`  - ${failure}`)
    process.exitCode = 1
    return
  }
  console.log('all checks passed')
}

await main()
