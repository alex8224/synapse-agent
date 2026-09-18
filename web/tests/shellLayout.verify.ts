/**
 * Browser verification for the two-column shell layout.
 *
 * The static guards (`shellLayout.test.ts`) pin the *source*; a flex chain that
 * silently stops stretching still compiles, so this script measures the rendered
 * geometry in a real headless browser against a real `synapse-web-console` host.
 *
 * No runtime daemon is needed: pairing alone renders the workspace shell (the
 * transcript stays empty and the read-only diagnostics banner may appear).  The
 * host's synthetic token file lives in a temp dir, is never printed and is
 * removed with it.
 *
 * Reading width: on desktop the shared gutters are 10% of the workspace pane each
 * side, so `.console-column` takes ~80% of the pane with no `rem` cap (a wider
 * window really is wider); below the `lg` breakpoint the gutters fall back to a
 * flat `2rem` and the column fills the rest.  Both are measured against the pane
 * the composer wrapper spans.
 *
 * Run:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/shellLayout.verify.ts
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
  // Structural comparison: some checks compare edge pairs, not primitives.
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  if (!ok) failures.push(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label} = ${JSON.stringify(actual)}`)
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

/**
 * Terminate the host and anything it spawned.
 *
 * Windows has no POSIX process groups, so it keeps `taskkill /T`; everywhere else
 * the host is spawned detached (see the `detached` option below), so the negative
 * pid kills the whole group and the runtime daemon cannot outlive the run.
 */
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

interface Rect {
  left: number
  right: number
  top: number
  bottom: number
  width: number
  height: number
}

interface Snapshot {
  viewport: { w: number; h: number }
  sidebar: Rect
  header: Rect
  /** The header's centre grid track: the session title chip. */
  title: Rect
  /**
   * The sidebar tree's computed `scrollbar-width` and focusability.  Null in the
   * collapsed rail, which is a different `<nav>` and carries no tree.
   */
  tree: { scrollbarWidth: string; tabIndex: string | null } | null
  footer: Rect
  /** The workspace pane (`main`): the reference for the reading width. */
  pane: Rect
  card: Rect
  /**
   * The composer wrapper (`w-full` + `.console-gutter`): its padded content box is
   * the width the reading column fills, so this is the gutter-inset workspace.
   */
  composerBox: { clientWidth: number; paddingLeft: number; paddingRight: number }
  columns: Rect[]
  scroller: {
    clientWidth: number
    offsetWidth: number
    scrollbarWidth: string
    rect: Rect
    column: Rect
  } | null
  /**
   * The scroller's bottom reserve for the floating composer: the padding that
   * keeps the newest line clear of the card, the matching scroll padding, and the
   * measured card height the composer published.
   */
  composerReserve: {
    paddingBottom: number
    scrollPaddingBottom: number
    published: string
  } | null
  navText: string
  scrollWidth: number
}

const SNAPSHOT = `(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect()
    return {
      left: Math.round(r.left), right: Math.round(r.right),
      top: Math.round(r.top), bottom: Math.round(r.bottom),
      width: Math.round(r.width), height: Math.round(r.height),
    }
  }
  const sidebar = document.querySelector('nav')
  const header = document.querySelector('header')
  // The middle grid track of the header: the session title chip.
  const title = header.children[1]
  const treeElement = sidebar.querySelector('.sidebar-scroll')
  const tree = treeElement?.closest('[inert]') ? null : treeElement
  const footer = document.querySelector('footer')
  const card = document.querySelector('#console-composer').closest('.console-column')
  const cardWrapper = card.parentElement
  const cardWrapperStyle = getComputedStyle(cardWrapper)
  const scroller = document.querySelector('main .no-scrollbar')
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    sidebar: rect(sidebar), header: rect(header), footer: rect(footer),
    title: rect(title),
    tree: tree === null ? null : {
      scrollbarWidth: getComputedStyle(tree).scrollbarWidth,
      tabIndex: tree.getAttribute('tabindex'),
    },
    pane: rect(document.querySelector('main')),
    card: rect(card),
    composerBox: {
      clientWidth: cardWrapper.clientWidth,
      paddingLeft: parseFloat(cardWrapperStyle.paddingLeft),
      paddingRight: parseFloat(cardWrapperStyle.paddingRight),
    },
    // Every reading column in the pane except the composer: the transcript and the
    // diagnostics notice (when it is up) share the chat edges.  The composer is
    // narrower on purpose, so it is measured separately (see checkComposer).
    columns: [...document.querySelectorAll('main .console-column')].filter((el) => el !== card).map(rect),
    scroller: scroller === null ? null : {
      clientWidth: scroller.clientWidth,
      offsetWidth: scroller.offsetWidth,
      scrollbarWidth: getComputedStyle(scroller).scrollbarWidth,
      rect: rect(scroller),
      column: rect(scroller.querySelector('.console-column')),
    },
    composerReserve: scroller === null ? null : {
      paddingBottom: parseFloat(getComputedStyle(scroller).paddingBottom),
      scrollPaddingBottom: parseFloat(getComputedStyle(scroller).scrollPaddingBottom),
      published: getComputedStyle(document.documentElement).getPropertyValue('--composer-h').trim(),
    },
    navText: sidebar.innerText,
    scrollWidth: document.documentElement.scrollWidth,
  }
})()`

/**
 * Scroll the transcript to its end and report whether the newest content landed
 * above the floating composer.
 *
 * The reserved padding is what makes this true; measuring it only proves the
 * mechanism, not the guarantee the reader actually depends on.
 */
const BOTTOM_VISIBLE = `(() => {
  const scroller = document.querySelector('main .no-scrollbar')
  const card = document.querySelector('#console-composer').closest('.console-column')
  if (scroller === null || card === null) return false
  scroller.scrollTop = scroller.scrollHeight
  const items = scroller.querySelectorAll('.console-column > *')
  if (items.length === 0) return true
  const newest = items[items.length - 1].getBoundingClientRect()
  return newest.bottom <= card.getBoundingClientRect().top
})()`

/**
 * Fill the pairing input, then submit it as a separate task.
 *
 * Two reasons the old single-task version raced: the gate renders while the store
 * is still probing `GET /api/session` (`pairingState === 'checking'`), when the
 * submit button is disabled and the form refuses to pair; and writing the
 * controlled input only schedules a React state update, so a submit in the same
 * task can still observe an empty code.  `PAIR_READY` waits for both to settle.
 */
const PAIR_FILL = `(() => {
  const input = document.querySelector('#pairing-code')
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
  setter.call(input, '__CODE__')
  input.dispatchEvent(new Event('input', { bubbles: true }))
  return true
})()`

/** The gate accepts a submit once the probe settled and the code is complete. */
const PAIR_READY = `(() => {
  const button = document.querySelector('#pairing-code')?.closest('form')?.querySelector('button[type=submit]')
  return button !== null && button !== undefined && button.disabled === false
})()`

const PAIR_SUBMIT = `(() => {
  document.querySelector('#pairing-code').closest('form').requestSubmit()
  return true
})()`

function setViewport(client: CdpClient, page: PageHandle, width: number, height: number) {
  return client.send(
    'Emulation.setDeviceMetricsOverride',
    { width, height, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  )
}

/**
 * The reading columns share the chat column's edges.
 *
 * The transcript scrolls with no visible scrollbar, so nothing narrows the scroll
 * port: the transcript and the diagnostics notice share both edges.  The composer
 * is excluded -- it is narrower on purpose (see `checkComposer`).
 */
function checkReadingColumns(snapshot: Snapshot): void {
  const reference = snapshot.columns[0]
  if (reference === undefined) return
  for (const column of snapshot.columns) {
    check(
      `reading column at x=${column.left} shares the chat edges`,
      [column.left, column.right],
      [reference.left, reference.right],
    )
  }
}

/** The composer cap from `index.css` (`--composer-max: 48rem`, root font 16px). */
const COMPOSER_MAX = 48 * 16

/** The chat column's width: the transcript's own reading column. */
function chatColumn(snapshot: Snapshot): number {
  return snapshot.scroller?.column.width ?? -1
}

/**
 * Desktop: the chat column is 80% of the workspace pane.
 *
 * The gutters are 10% of the pane each side, so the reading width is a real
 * fraction of the pane rather than a `rem` cap.
 */
function checkDesktopColumn(snapshot: Snapshot): void {
  const expected = Math.round(snapshot.pane.width * 0.8)
  check(
    `chat column takes ~80% of the ${snapshot.pane.width}px workspace`,
    Math.abs(chatColumn(snapshot) - expected) <= 2,
    true,
  )
}

/**
 * Below the breakpoint the gutters fall back to a flat `2rem` (root font size
 * 16px), so the chat column fills the rest of the pane.
 */
function checkNarrowColumn(snapshot: Snapshot): void {
  const expected = snapshot.pane.width - 2 * 32
  check(
    `narrow chat column fills the ${expected}px gutter-inset workspace`,
    Math.abs(chatColumn(snapshot) - expected) <= 2,
    true,
  )
}

/**
 * The composer is narrower than the chat column, and centred on the same axis.
 *
 * A single-line input does not need the reading width, so it is capped at
 * `--composer-max`; below the cap the gutter-inset width decides, exactly like the
 * chat column.  The media query that switches the gutters is on the viewport.
 */
function checkComposer(snapshot: Snapshot): void {
  const inset = snapshot.viewport.w >= 1024
    ? Math.round(snapshot.pane.width * 0.8)
    : snapshot.pane.width - 2 * 32
  const expected = Math.min(inset, COMPOSER_MAX)
  check(
    `composer is ${expected}px wide, not the reading width`,
    Math.abs(snapshot.card.width - expected) <= 2,
    true,
  )
  const column = snapshot.scroller?.column
  if (column === undefined) return
  check(
    'composer is centred on the chat column',
    Math.abs(snapshot.card.left + snapshot.card.right - (column.left + column.right)) <= 2,
    true,
  )
  // Below the cap the two are the same width (there is nothing to give up); past it
  // the composer stops growing while the chat column keeps going.
  check('composer is not wider than the chat column', snapshot.card.width <= column.width, true)
  if (column.width > COMPOSER_MAX) {
    check(
      `composer stays at its ${COMPOSER_MAX}px cap while the chat column is ${column.width}px`,
      snapshot.card.width <= COMPOSER_MAX,
      true,
    )
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
    const ready = (await evaluate(client, page, expression)) as boolean
    if (ready === true) return
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`timed out waiting for: ${expression}`)
}

async function main(): Promise<void> {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'shell-layout-'))
  const tokenFile = path.join(tempDir, 'token')
  // Synthetic fixture: never a real credential, never printed, removed with the dir.
  fs.writeFileSync(tokenFile, 'shell-layout-verification-fixture', { mode: 0o600 })

  console.log('=== two-column shell layout (real host, headless browser) ===')
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
      // POSIX: own process group so `killTree` can take the runtime daemon too.
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
      const metadata = JSON.parse(line) as { url: string }
      origin = metadata.url.replace(/\/$/, '')
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
    await evaluate(client, page, PAIR_FILL.replace('__CODE__', pairingCode))
    // The gate renders while the store is still probing `GET /api/session`
    // (`pairingState === 'checking'`); in that state the submit button is
    // disabled and the form refuses to pair, so wait for the probe to settle
    // before submitting instead of racing it.
    await waitFor(client, page, PAIR_READY)
    await evaluate(client, page, PAIR_SUBMIT)
    await waitFor(client, page, `document.querySelector('#console-composer') !== null`)
    // Let the post-pairing layout settle (project bootstrap, fonts, icons).
    await new Promise((resolve) => setTimeout(resolve, 1500))

    await setViewport(client, page, 1440, 900)
    await new Promise((resolve) => setTimeout(resolve, 500))
    const wide = (await evaluate(client, page, SNAPSHOT)) as Snapshot
    console.log('--- 1440x900 ---')
    console.log(JSON.stringify(wide, null, 2))
    console.log('')
    check('sidebar starts at the top edge', wide.sidebar.top, 0)
    check('sidebar spans the viewport height', wide.sidebar.height, wide.viewport.h)
    check('sidebar keeps its 240px width', wide.sidebar.width, 240)
    check('header starts right of the sidebar', wide.header.left, wide.sidebar.width)
    check('header stays at the top edge', wide.header.top, 0)
    check('status strip starts right of the sidebar', wide.footer.left, wide.sidebar.width)
    check('status strip ends at the bottom edge', wide.footer.bottom, wide.viewport.h)
    check('the pane renders its reading columns', wide.columns.length >= 1, true)
    checkReadingColumns(wide)
    checkDesktopColumn(wide)
    checkComposer(wide)
    check('composer sits above the status strip', wide.card.bottom <= wide.footer.top, true)
    // The composer floats over the transcript — that is what makes its acrylic
    // visible — so the scroller reserves the card's *measured* height and the
    // newest streamed line still has to land above it.
    const reserve = wide.composerReserve
    check(
      'the transcript reserves the floating composer',
      reserve !== null && reserve.paddingBottom >= wide.card.height + 16,
      true,
    )
    check(
      'the scroll end lands above the composer',
      reserve?.scrollPaddingBottom,
      reserve?.paddingBottom,
    )
    check('the reserve is the measured card, not a guess', reserve?.published, `${wide.card.height}px`)
    check('the newest line stays visible at the bottom', await evaluate(client, page, BOTTOM_VISIBLE), true)
    check('the composer floats over the transcript', wide.card.top < wide.scroller!.rect.bottom, true)
    check('no horizontal overflow', wide.scrollWidth, wide.viewport.w)
    // The session title sits on the centre line of the workspace column: both
    // header side tracks are equal `1fr`, so the middle track cannot drift.  A
    // couple of pixels of rounding slack is allowed.
    check(
      'the session title is centred on the header',
      Math.abs(wide.title.left + wide.title.right - (wide.header.left + wide.header.right)) <= 2,
      true,
    )
    // The sidebar tree scrolls but shows no scrollbar; it stays keyboard-focusable.
    check('the expanded sidebar exposes its tree', wide.tree !== null, true)
    check('the sidebar tree hides its scrollbar', wide.tree?.scrollbarWidth, 'none')
    // The transcript scrolls the same way (wheel / touch / keyboard), and its
    // hidden scrollbar is what keeps its column on the reading edges.
    check('the transcript hides its scrollbar', wide.scroller?.scrollbarWidth, 'none')
    check('the sidebar tree stays keyboard-focusable', wide.tree?.tabIndex, '0')
    check('nav names the new-task entry', wide.navText.includes('新建任务'), true)
    check('nav shows the Ctrl+N hint', wide.navText.includes('Ctrl+N'), true)
    check('search shows the Ctrl+K hint', wide.navText.includes('Ctrl+K'), true)

    // Wide workspace: the chat column keeps growing while the composer stops at its
    // cap, so the two are visibly decoupled.
    await setViewport(client, page, 1920, 1080)
    await new Promise((resolve) => setTimeout(resolve, 500))
    const extraWide = (await evaluate(client, page, SNAPSHOT)) as Snapshot
    console.log('')
    console.log('--- 1920x1080 (chat grows, composer capped) ---')
    console.log(
      JSON.stringify(
        { viewport: extraWide.viewport, card: extraWide.card, composerBox: extraWide.composerBox },
        null,
        2,
      ),
    )
    checkDesktopColumn(extraWide)
    checkComposer(extraWide)
    check('a wide workspace widens the chat column', chatColumn(extraWide) > chatColumn(wide), true)
    check('a wide workspace does not widen the composer', extraWide.card.width, wide.card.width)
    check('wide workspace has no horizontal overflow', extraWide.scrollWidth, extraWide.viewport.w)
    await setViewport(client, page, 1440, 900)
    await new Promise((resolve) => setTimeout(resolve, 300))

    // Collapsed rail: the header and the strip must follow the 44px column.
    await evaluate(
      client,
      page,
      `(() => { document.querySelector('header button').click(); return true })()`,
    )
    await new Promise((resolve) => setTimeout(resolve, 500))
    const collapsed = (await evaluate(client, page, SNAPSHOT)) as Snapshot
    console.log('')
    console.log('--- collapsed rail ---')
    console.log(JSON.stringify({ sidebar: collapsed.sidebar, header: collapsed.header }, null, 2))
    check('collapsed rail keeps the full height', collapsed.sidebar.height, collapsed.viewport.h)
    check('header follows the collapsed rail', collapsed.header.left, collapsed.sidebar.width)
    check('status strip follows the collapsed rail', collapsed.footer.left, collapsed.sidebar.width)
    check('the collapsed rail carries no tree', collapsed.tree, null)

    // Narrow window: the chips truncate instead of pushing the pane sideways.
    await evaluate(
      client,
      page,
      `(() => { document.querySelector('header button').click(); return true })()`,
    )
    await setViewport(client, page, 900, 700)
    await new Promise((resolve) => setTimeout(resolve, 500))
    const narrow = (await evaluate(client, page, SNAPSHOT)) as Snapshot
    console.log('')
    console.log('--- 900x700 ---')
    console.log(JSON.stringify({ viewport: narrow.viewport, card: narrow.card, columns: narrow.columns }, null, 2))
    check('narrow window has no horizontal overflow', narrow.scrollWidth, narrow.viewport.w)
    checkReadingColumns(narrow)
    checkNarrowColumn(narrow)
    checkComposer(narrow)

    // Compact window: still near full width, so only the flat `2rem` gutters remain.
    await setViewport(client, page, 768, 640)
    await new Promise((resolve) => setTimeout(resolve, 500))
    const compact = (await evaluate(client, page, SNAPSHOT)) as Snapshot
    console.log('')
    console.log('--- 640x640 ---')
    console.log(
      JSON.stringify(
        { viewport: compact.viewport, card: compact.card, composerBox: compact.composerBox },
        null,
        2,
      ),
    )
    check('compact window has no horizontal overflow', compact.scrollWidth, compact.viewport.w)
    checkReadingColumns(compact)
    checkNarrowColumn(compact)
    checkComposer(compact)

    for (const width of [360, 390, 430, 640]) {
      await setViewport(client, page, width, 800)
      await client.send('Emulation.setTouchEmulationEnabled', { enabled: true }, page.sessionId)
      await new Promise((resolve) => setTimeout(resolve, 300))
      const phone = await evaluate(client, page, `(() => {
        const main = document.querySelector('main').getBoundingClientRect();
        const card = document.querySelector('.ui-composer').getBoundingClientRect();
        const nav = document.querySelector('#console-navigation');
        return { closed: nav.hidden, full: Math.abs(main.width - innerWidth) < 2,
          cardFits: card.left >= 10 && card.right <= innerWidth - 10,
          wideEnough: card.width >= innerWidth - 30 };
      })()`) as Record<string, boolean>
      for (const [key, value] of Object.entries(phone)) check(`${width}px ${key}`, value, true)
      await evaluate(client, page, `(() => { const button = document.querySelector('header button'); button.focus(); button.click(); })()`)
      await new Promise((resolve) => setTimeout(resolve, 150))
      check(`${width}px drawer opens without squeezing content`, await evaluate(client, page, `(() => {
        const nav = document.querySelector('#console-navigation');
        return !nav.hidden && nav.getBoundingClientRect().right <= innerWidth &&
          document.querySelector('.console-workspace').inert &&
          Math.abs(document.querySelector('main').getBoundingClientRect().width - innerWidth) < 2;
      })()`), true)
      await evaluate(client, page, `document.querySelector('.navigation-close').click()`)
      await new Promise((resolve) => setTimeout(resolve, 150))
      check(`${width}px focus returns to toggle`, await evaluate(client, page,
        `document.activeElement === document.querySelector('header button')`), true)
    }
    await client.send('Emulation.setTouchEmulationEnabled', { enabled: false }, page.sessionId)
    await setViewport(client, page, 1440, 900)
    await new Promise((resolve) => setTimeout(resolve, 300))
    check('desktop sidebar preference survives mobile navigation', await evaluate(client, page,
      `Math.round(document.querySelector('nav').getBoundingClientRect().width)`), 240)

    const shot = (await client.send(
      'Page.captureScreenshot',
      { format: 'png' },
      page.sessionId,
    )) as { data: string }
    const shotPath = path.join(import.meta.dirname, '.shell-layout.png')
    fs.writeFileSync(shotPath, Buffer.from(shot.data, 'base64'))
    console.log('')
    console.log(`screenshot     : ${shotPath}`)
  } finally {
    if (browser !== undefined) await closeBrowser(browser)
    killTree(host.pid)
    fs.rmSync(tempDir, { recursive: true, force: true })
  }

  console.log('')
  if (failures.length > 0) {
    console.log(`FAILED (${failures.length}):`)
    for (const failure of failures) console.log(`  - ${failure}`)
    process.exitCode = 1
  } else {
    console.log('ALL CHECKS PASSED')
  }
}

await main()
