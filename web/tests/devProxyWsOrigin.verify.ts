/**
 * C2-1 (phase 5, round 2): does the Vite dev proxy rewrite `Origin` on the
 * `/runtime-ws` *upgrade* request, and does it stay a pure pass-through proxy?
 *
 * Method: start a controlled console-host stub, start the real dev server
 * (`npm run dev`, i.e. `vite --host 127.0.0.1 --port <p> --strictPort`) pointed
 * at that stub through `SYNAPSE_WEB_CONSOLE_URL`, then drive a real websocket
 * client through the dev port and read back the head the stub actually received.
 *
 * NOTE: this sandbox exports `NODE_USE_ENV_PROXY` + `HTTP_PROXY`, which makes
 * Node's own HTTP client send even loopback requests to an external Go proxy.
 * Run with those cleared for the loopback path to be exercised directly:
 *
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/devProxyWsOrigin.verify.ts
 */
import { spawn, spawnSync } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'
import type { AddressInfo } from 'node:net'
import { startConsoleStub } from './helpers/consoleStub.ts'
import { clientTextFrame, readServerFrame, wsHandshake } from './helpers/wsClient.ts'
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
        if (Date.now() > deadline) reject(new Error(`dev server did not listen on ${host}:${port}`))
        else setTimeout(attempt, 200)
      })
    }
    attempt()
  })
}

async function main(): Promise<void> {
  const stub = await startConsoleStub()
  const devPort = await freePort()
  const devOrigin = `http://127.0.0.1:${devPort}`
  const foreignOrigin = 'http://evil.example:1234'

  console.log('=== C2-1 dev proxy Origin rewrite on the /runtime-ws upgrade ===')
  console.log(`console host stub origin : ${stub.origin}`)
  console.log(`vite dev server origin   : ${devOrigin}`)
  console.log(`NODE_USE_ENV_PROXY set   : ${process.env.NODE_USE_ENV_PROXY === undefined ? 'no' : 'yes'}`)
  console.log('')

  const dev = spawn(
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
  let ready = false
  dev.stdout?.on('data', (chunk: Buffer) => {
    devLog += chunk.toString('utf8')
  })
  dev.stderr?.on('data', (chunk: Buffer) => {
    devLog += chunk.toString('utf8')
  })

  const wsCases: Array<{ name: string; origin?: string; viaProxy: boolean }> = [
    { name: 'A  via proxy, client Origin = dev origin (browser case)', origin: devOrigin, viaProxy: true },
    { name: 'B  via proxy, client Origin = foreign origin', origin: foreignOrigin, viaProxy: true },
    { name: 'C  via proxy, client sends no Origin', viaProxy: true },
    { name: 'D  control: direct to stub, no proxy', origin: devOrigin, viaProxy: false },
  ]

  try {
    await waitForPort('127.0.0.1', devPort, 60_000)
    ready = true
    console.log('dev server ready')
    console.log('')

    for (const item of wsCases) {
      const url = item.viaProxy
        ? `ws://127.0.0.1:${devPort}/runtime-ws`
        : `ws://127.0.0.1:${stub.port}/runtime-ws`
      const before = stub.observed.length
      const handshake = await wsHandshake(url, item.origin ? { Origin: item.origin } : {})
      let frame = '<none>'
      if (handshake.status === 101) {
        handshake.socket.write(clientTextFrame('c2-1 ping'))
        const reply = await readServerFrame(handshake.socket, handshake.leftover, 3000)
        frame = `opcode=${reply.opcode} payload=${reply.payload}`
      }
      handshake.socket.destroy()
      const upgrade = stub.observed.slice(before).find((entry) => entry.kind === 'upgrade')

      console.log(`--- case ${item.name} ---`)
      console.log(`client sent   Origin: ${item.origin ?? '<absent>'}`)
      console.log(`handshake          : ${handshake.statusLine} accept-ok=${handshake.acceptOk}`)
      console.log(`server frame       : ${frame}`)
      console.log('stub received head :')
      console.log(upgrade?.rawHead ?? '<no upgrade observed>')
      console.log('')
      check(`case ${item.name}: upgrade reached the stub`, upgrade !== undefined, true)
      check(`case ${item.name}: handshake status`, handshake.status, 101)
      // Through the proxy the stub must see the console origin; the direct
      // control must see the client value untouched.
      check(
        `case ${item.name}: Origin seen by the stub`,
        upgrade?.headers.origin,
        item.viaProxy ? stub.origin : item.origin,
      )
      check(`case ${item.name}: no Authorization injected`, upgrade?.headers.authorization, undefined)
      check(`case ${item.name}: no Cookie injected`, upgrade?.headers.cookie, undefined)
      console.log('')
    }

    const httpCases: Array<{ name: string; url: string; origin?: string; viaProxy: boolean }> = [
      { name: 'E  via proxy, client Origin = foreign origin', url: `${devOrigin}/api/session`, origin: foreignOrigin, viaProxy: true },
      { name: 'F  control: direct to stub, no proxy', url: `${stub.origin}/api/session`, origin: foreignOrigin, viaProxy: false },
    ]

    for (const item of httpCases) {
      const before = stub.observed.length
      const response = await httpProbe({ url: item.url, headers: item.origin ? { Origin: item.origin } : {} })
      const seen = stub.observed.slice(before).find((entry) => entry.kind === 'http')
      console.log(`--- case ${item.name} ---`)
      console.log(`client sent   Origin: ${item.origin ?? '<absent>'}`)
      console.log(`status             : ${response.statusLine}`)
      console.log('stub received head :')
      console.log(seen?.rawHead ?? '<no http request observed>')
      console.log('')
      check(`case ${item.name}: HTTP status`, response.status, 200)
      check(
        `case ${item.name}: Origin seen by the stub`,
        seen?.headers.origin,
        item.viaProxy ? stub.origin : item.origin,
      )
      check(`case ${item.name}: no Authorization injected`, seen?.headers.authorization, undefined)
      check(`case ${item.name}: no Cookie injected`, seen?.headers.cookie, undefined)
      console.log('')
    }

    console.log(`stub TCP connections: ${stub.connections.length}`)
    for (const conn of stub.connections) {
      console.log(`  ${conn.remote} :: ${conn.firstBytes.split('\r\n')[0]}`)
    }
    console.log('')
    console.log('--- full dev server output ---')
    console.log(stripAnsi(devLog).trim())
    console.log('')
  } finally {
    if (dev.pid !== undefined) {
      spawnSync('taskkill', ['/PID', String(dev.pid), '/T', '/F'], { stdio: 'ignore' })
    }
    await stub.close()
    if (!ready) {
      console.log('--- dev server output (startup failed) ---')
      console.log(devLog)
    }
  }

  console.log(failures.length === 0 ? 'ALL C2-1 CHECKS PASSED' : `C2-1 FAILURES:\n${failures.join('\n')}`)
  process.exitCode = failures.length === 0 ? 0 : 1
}

await main()