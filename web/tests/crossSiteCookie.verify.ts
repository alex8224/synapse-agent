/**
 * C2-2 (phase 5, round 2): does a cross-site page's websocket handshake to the
 * console carry the `SameSite=Strict` session cookie?
 *
 * Method: drive a real browser (Chrome/Edge via CDP, headless) against a
 * controlled console-host stub that mirrors the A4 cookie contract exactly, and
 * read back the head the stub actually received for each handshake. A same-site
 * control case proves the cookie was genuinely stored and attachable, so a
 * missing cookie on the cross-site case is meaningful rather than an artefact.
 *
 * Run:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/crossSiteCookie.verify.ts
 */
import { spawn, spawnSync } from 'node:child_process'
import http from 'node:http'
import net from 'node:net'
import path from 'node:path'
import process from 'node:process'
import type { AddressInfo } from 'node:net'
import { startConsoleStub } from './helpers/consoleStub.ts'
import type { ObservedRequest } from './helpers/consoleStub.ts'
import { CdpClient, closeBrowser, cookiesFor, evaluate, launchBrowser, openPage } from './helpers/cdp.ts'
import { httpProbe } from './helpers/httpProbe.ts'
import { stripAnsi } from './helpers/stripAnsi.ts'

const WEB_ROOT = path.resolve(import.meta.dirname, '..')

const failures: string[] = []
function check(label: string, actual: unknown, expected: unknown): void {
  const ok = actual === expected
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

function waitForPort(host: string, port: number, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const attempt = (): void => {
      const socket = net.connect({ host, port })
      socket.once('connect', () => {
        socket.destroy()
        resolve()
      })
      socket.once('error', () => {
        socket.destroy()
        if (Date.now() > deadline) reject(new Error(`no listener on ${host}:${port}`))
        else setTimeout(attempt, 200)
      })
    }
    attempt()
  })
}

/** A page served from a different loopback *site* (127.0.0.2 != 127.0.0.1). */
async function startCrossSitePage(): Promise<{ origin: string; close: () => Promise<void> }> {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
    res.end('<!doctype html><html><head><title>cross-site page</title></head><body>x</body></html>')
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.2', resolve))
  const port = (server.address() as AddressInfo).port
  return {
    origin: `http://127.0.0.2:${port}`,
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve())
      }),
  }
}

/** In-page probe: open a websocket and report the outcome as a string. */
function wsProbe(url: string): string {
  return `new Promise((resolve) => {
    let settled = false;
    let ws;
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      try { ws.close(); } catch (error) { /* ignore */ }
      resolve(outcome);
    };
    try { ws = new WebSocket(${JSON.stringify(url)}); } catch (error) { resolve('constructor-error'); return; }
    ws.onopen = () => setTimeout(() => finish('open'), 300);
    ws.onerror = () => finish('error');
    ws.onclose = (event) => finish('close:' + event.code);
    setTimeout(() => finish('timeout'), 8000);
  })`
}

function summarize(entry: ObservedRequest | undefined): Record<string, unknown> {
  const cookie = entry?.headers.cookie
  return {
    host: entry?.headers.host ?? null,
    origin: entry?.headers.origin ?? null,
    secFetchSite: entry?.headers['sec-fetch-site'] ?? null,
    secFetchMode: entry?.headers['sec-fetch-mode'] ?? null,
    cookiePresent: cookie !== undefined,
    cookieBytes: typeof cookie === 'string' ? cookie.length : 0,
    authorization: entry?.headers.authorization ?? null,
    userAgent: typeof entry?.headers['user-agent'] === 'string' ? entry.headers['user-agent'].slice(0, 40) : null,
  }
}

async function main(): Promise<void> {
  const stub = await startConsoleStub({ sessionCookie: true })
  const site = await startCrossSitePage()
  const devPort = await freePort()
  const devOrigin = `http://127.0.0.1:${devPort}`

  console.log('=== C2-2 cross-site websocket and the SameSite=Strict console cookie ===')
  console.log(`console host stub      : ${stub.origin}  (session cookie mirrors A4)`)
  console.log(`cross-site page origin : ${site.origin}`)
  console.log(`vite dev server        : ${devOrigin}`)
  console.log('')

  const browser = await launchBrowser()
  console.log(`browser                : ${browser.executable}`)
  console.log(`devtools port          : ${browser.port}`)
  const versionResponse = await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })
  const version = JSON.parse(versionResponse.body) as {
    Browser: string
    'Protocol-Version': string
    webSocketDebuggerUrl: string
  }
  console.log(`browser version        : ${version.Browser} (CDP ${version['Protocol-Version']})`)
  console.log('')

  let dev: ReturnType<typeof spawn> | undefined
  let client: CdpClient | undefined
  try {
    client = await CdpClient.connect(version.webSocketDebuggerUrl)

    const sameSitePage = await openPage(client, `${stub.origin}/`)
    const cookies = await cookiesFor(client, sameSitePage, `${stub.origin}/`)
    console.log('--- cookies stored by the browser for the console host origin ---')
    console.log(JSON.stringify(cookies))
    console.log(`stub cookie value length: ${stub.sessionCookieLength}`)
    console.log('')
    check('exactly one console cookie stored', (cookies as unknown[]).length, 1)
    const cookie = (cookies as Array<Record<string, unknown>>)[0] ?? {}
    check('cookie is SameSite=Strict', cookie.sameSite, 'Strict')
    check('cookie is HttpOnly', cookie.httpOnly, true)
    check('cookie is not Secure (http slice)', cookie.secure, false)
    check('cookie Path=/', cookie.path, '/')

    const cases: Array<{ name: string; pageUrl: string; wsUrl: string; sameSite: boolean }> = []

    cases.push({
      name: 'S  control: page on the console origin -> console host',
      pageUrl: `${stub.origin}/`,
      wsUrl: `ws://127.0.0.1:${stub.port}/runtime-ws`,
      sameSite: true,
    })
    cases.push({
      name: 'X  cross-site page -> console host (direct)',
      pageUrl: `${site.origin}/`,
      wsUrl: `ws://127.0.0.1:${stub.port}/runtime-ws`,
      sameSite: false,
    })

    // The dev server must be up before the two dev-proxy cases run.
    dev = spawn(
      'npm',
      ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(devPort), '--strictPort'],
      {
        cwd: WEB_ROOT,
        env: { ...process.env, SYNAPSE_WEB_CONSOLE_URL: stub.origin, NODE_OPTIONS: '' },
        shell: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    )
    let devLog = ''
    dev.stdout?.on('data', (chunk: Buffer) => {
      devLog += chunk.toString('utf8')
    })
    dev.stderr?.on('data', (chunk: Buffer) => {
      devLog += chunk.toString('utf8')
    })
    await waitForPort('127.0.0.1', devPort, 60_000)

    cases.push({
      name: 'E  same-site page on the dev origin -> dev proxy',
      pageUrl: `${devOrigin}/`,
      wsUrl: `ws://127.0.0.1:${devPort}/runtime-ws`,
      sameSite: true,
    })
    cases.push({
      name: 'D  cross-site page -> dev proxy',
      pageUrl: `${site.origin}/`,
      wsUrl: `ws://127.0.0.1:${devPort}/runtime-ws`,
      sameSite: false,
    })

    const results: Record<string, Record<string, unknown>> = {}
    for (const item of cases) {
      const page = await openPage(client, item.pageUrl)
      const before = stub.observed.length
      const outcome = await evaluate(client, page, wsProbe(item.wsUrl))
      const upgrade = stub.observed.slice(before).find((entry) => entry.kind === 'upgrade')
      const summary = summarize(upgrade)
      results[item.name] = { outcome, ...summary }
      console.log(`--- case ${item.name} ---`)
      console.log(`  page origin        : ${item.pageUrl}`)
      console.log(`  websocket target   : ${item.wsUrl}`)
      console.log(`  browser outcome    : ${JSON.stringify(outcome)}`)
      console.log(`  stub observed      : ${JSON.stringify(summary)}`)
      console.log('')
      check(`case ${item.name}: handshake reached the stub`, upgrade !== undefined, true)
      check(`case ${item.name}: browser outcome`, outcome, 'open')
      check(`case ${item.name}: cookie carried on the handshake`, summary.cookiePresent, item.sameSite)
    }

    console.log('--- dev server output ---')
    console.log(stripAnsi(devLog).trim())
    console.log('')
    console.log('--- machine-readable summary ---')
    console.log(JSON.stringify({ cases: results }, null, 2))
    console.log('')
  } finally {
    client?.close()
    await closeBrowser(browser)
    if (dev?.pid !== undefined) spawnSync('taskkill', ['/PID', String(dev.pid), '/T', '/F'], { stdio: 'ignore' })
    await site.close()
    await stub.close()
  }

  console.log(failures.length === 0 ? 'ALL C2-2 CHECKS PASSED' : `C2-2 FAILURES:\n${failures.join('\n')}`)
  process.exitCode = failures.length === 0 ? 0 : 1
}

await main()