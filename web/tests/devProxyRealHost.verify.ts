/**
 * C2-3 (phase 5, round 2): the dev proxy is not an authentication bypass.
 *
 * Method: start the *real* `synapse-web-console` host on a temporary loopback
 * port with a synthetic throwaway token file in a temp state dir, start the real
 * dev server pointed at it, and replay the same unpaired / unauthenticated
 * requests twice - once through the dev proxy, once straight at the host - and
 * compare. Identical rejections prove the proxy relaxes nothing and injects no
 * credential. The pairing code printed on the host stderr is never read.
 *
 * Run:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/devProxyRealHost.verify.ts
 */
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import type { AddressInfo } from 'node:net'
import { wsHandshake } from './helpers/wsClient.ts'
import { httpProbe } from './helpers/httpProbe.ts'
import { stripAnsi } from './helpers/stripAnsi.ts'

const WEB_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(WEB_ROOT, '..')

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

function killTree(pid: number | undefined): void {
  if (pid === undefined) return
  spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' })
}

const CSRF_HEADERS = {
  'Content-Type': 'application/json',
  'X-Synapse-Console': '1',
}

async function main(): Promise<void> {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'c2-realhost-'))
  const tokenFile = path.join(tempDir, 'token')
  // Synthetic fixture: never a real credential, never printed, deleted at the end.
  fs.writeFileSync(tokenFile, 'c2-verification-fixture-token', { mode: 0o600 })

  console.log('=== C2-3 dev proxy is not an authentication bypass (real host) ===')
  console.log(`temp state dir           : ${tempDir}`)
  console.log(`NODE_USE_ENV_PROXY set   : ${process.env.NODE_USE_ENV_PROXY === undefined ? 'no' : 'yes'}`)
  console.log('')

  const host = spawn(
    'uv',
    [
      'run', '--no-sync', 'python', '-m', 'synapse.web_console.entry',
      '--workspace', REPO_ROOT,
      '--port', '0',
      '--host', '127.0.0.1',
      '--runtime-host', '127.0.0.1',
      '--runtime-port', '65535',
      '--state-dir', tempDir,
      '--token-file', tokenFile,
    ],
    { cwd: REPO_ROOT, env: { ...process.env, NODE_OPTIONS: '' }, stdio: ['ignore', 'pipe', 'pipe'] },
  )

  let hostStdout = ''
  let hostStderrBytes = 0
  host.stdout?.on('data', (chunk: Buffer) => {
    hostStdout += chunk.toString('utf8')
  })
  // The host prints the pairing code on stderr; its content is deliberately
  // discarded - only the byte count is retained.
  host.stderr?.on('data', (chunk: Buffer) => {
    hostStderrBytes += chunk.length
  })

  let dev: ReturnType<typeof spawn> | undefined
  try {
    const deadline = Date.now() + 60_000
    while (!hostStdout.includes('\n') && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 200))
    }
    const metadataLine = hostStdout.split(/\r?\n/)[0] ?? ''
    if (!metadataLine.startsWith('{')) {
      throw new Error(`host did not announce metadata; stderr bytes=${hostStderrBytes}`)
    }
    const metadata = JSON.parse(metadataLine) as { port: number; url: string; pairing_required: boolean }
    const hostPort = metadata.port
    const consoleOrigin = `http://127.0.0.1:${hostPort}`
    console.log(`real host               : ${metadata.url}`)
    console.log(`pairing_required        : ${metadata.pairing_required}`)
    console.log(`host stderr byte count  : ${hostStderrBytes} (content never read)`)
    console.log('')

    const devPort = await freePort()
    const devOrigin = `http://127.0.0.1:${devPort}`
    dev = spawn(
      'npm',
      ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(devPort), '--strictPort'],
      {
        cwd: WEB_ROOT,
        env: { ...process.env, SYNAPSE_WEB_CONSOLE_URL: consoleOrigin, NODE_OPTIONS: '' },
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
    console.log(`vite dev server         : ${devOrigin}  ->  ${consoleOrigin}`)
    console.log('')

    const viaProxy = `http://127.0.0.1:${devPort}`
    const direct = consoleOrigin

    async function replay(
      name: string,
      pathname: string,
      init: { method?: string; headers?: Record<string, string>; body?: string; origin: string },
      expected: number,
    ): Promise<void> {
      const proxied = await httpProbe({
        url: `${viaProxy}${pathname}`,
        method: init.method,
        headers: { Origin: init.origin, ...init.headers },
        body: init.body,
      })
      const plain = await httpProbe({
        url: `${direct}${pathname}`,
        method: init.method,
        headers: { Origin: init.origin, ...init.headers },
        body: init.body,
      })
      console.log(`--- ${name} ---`)
      console.log(`  via dev proxy : ${proxied.statusLine}  set-cookie=${JSON.stringify(proxied.headers['set-cookie'] ?? null)}  body=${proxied.body.slice(0, 120)}`)
      console.log(`  direct to host: ${plain.statusLine}  set-cookie=${JSON.stringify(plain.headers['set-cookie'] ?? null)}  body=${plain.body.slice(0, 120)}`)
      check(`${name}: proxied status`, proxied.status, expected)
      check(`${name}: direct status (identical)`, plain.status, expected)
      check(`${name}: proxy response carries no Set-Cookie`, proxied.headers['set-cookie'], undefined)
    }

    await replay('session without a cookie', '/api/session', { origin: consoleOrigin }, 401)
    await replay('session with a forged cookie', '/api/session', {
      origin: consoleOrigin,
      headers: { Cookie: 'synapse_web_session=forged-value-not-a-credential' },
    }, 401)
    await replay('removed bootstrap endpoint', '/api/bootstrap', { origin: consoleOrigin }, 405)
    await replay('pair with a wrong code', '/api/pair', {
      method: 'POST',
      headers: CSRF_HEADERS,
      body: JSON.stringify({ code: 'AAAAAAAA' }),
      origin: consoleOrigin,
    }, 401)

    console.log('--- pair rate limiting through the dev proxy ---')
    const sequence: number[] = []
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const response = await httpProbe({
        url: `${viaProxy}/api/pair`,
        method: 'POST',
        headers: { Origin: consoleOrigin, ...CSRF_HEADERS },
        body: JSON.stringify({ code: 'BBBBBBBB' }),
      })
      sequence.push(response.status)
      if (response.status === 429) break
    }
    console.log(`  proxied pair attempt statuses: ${sequence.join(', ')}`)
    check('rate limiting kicks in through the proxy', sequence.includes(429), true)
    console.log('')

    for (const [label, url, origin] of [
      ['via dev proxy', `ws://127.0.0.1:${devPort}/runtime-ws`, devOrigin],
      ['direct to host', `ws://127.0.0.1:${hostPort}/runtime-ws`, consoleOrigin],
    ] as const) {
      const handshake = await wsHandshake(url, { Origin: origin })
      console.log(`--- unpaired websocket upgrade, ${label} ---`)
      console.log(`  ${handshake.statusLine}`)
      console.log(handshake.rawHead)
      console.log('')
      handshake.socket.destroy()
      check(`unpaired websocket ${label}: status`, handshake.status, 403)
    }

    console.log('--- full dev server output ---')
    console.log(stripAnsi(devLog).trim())
    console.log('')
  } finally {
    killTree(dev?.pid)
    killTree(host.pid)
    await new Promise((resolve) => setTimeout(resolve, 500))
    try {
      fs.rmSync(tempDir, { recursive: true, force: true })
    } catch {
      /* best effort cleanup of the temp fixture */
    }
  }

  console.log(failures.length === 0 ? 'ALL C2-3 CHECKS PASSED' : `C2-3 FAILURES:\n${failures.join('\n')}`)
  process.exitCode = failures.length === 0 ? 0 : 1
}

await main()