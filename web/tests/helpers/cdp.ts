/**
 * Minimal Chrome DevTools Protocol client for the phase-5 C2 browser experiment.
 * It talks to the DevTools endpoint over the raw-socket websocket channel so the
 * sandbox's `NODE_USE_ENV_PROXY` cannot reroute loopback traffic.
 */
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import type { ChildProcess } from 'node:child_process'
import type { AddressInfo } from 'node:net'
import { httpProbe } from './httpProbe.ts'
import { WsChannel } from './wsChannel.ts'

export interface BrowserHandle {
  proc: ChildProcess
  port: number
  profileDir: string
  executable: string
}

const CANDIDATES = [
  process.env.SYNAPSE_CDP_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
]

/** Return the first browser executable present on this machine. */
export function findBrowser(): string | undefined {
  for (const candidate of CANDIDATES) {
    if (candidate && fs.existsSync(candidate)) return candidate
  }
  return undefined
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

/** Launch a headless browser with the DevTools endpoint on a temp profile. */
export async function launchBrowser(): Promise<BrowserHandle> {
  const executable = findBrowser()
  if (!executable) throw new Error('no Chrome/Edge executable found')
  const port = await freePort()
  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'c2-cdp-profile-'))
  const proc = spawn(
    executable,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--no-proxy-server',
      `--remote-debugging-port=${port}`,
      '--remote-allow-origins=*',
      `--user-data-dir=${profileDir}`,
      'about:blank',
    ],
    { stdio: ['ignore', 'ignore', 'pipe'] },
  )
  proc.stderr?.on('data', () => {
    /* DevTools banner only; never surfaced */
  })

  const deadline = Date.now() + 30_000
  while (Date.now() < deadline) {
    try {
      const response = await httpProbe({ url: `http://127.0.0.1:${port}/json/version`, timeoutMs: 1500 })
      if (response.status === 200) {
        return { proc, port, profileDir, executable }
      }
    } catch {
      /* not up yet */
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  proc.kill()
  throw new Error('browser DevTools endpoint did not come up')
}

/**
 * Kill the browser tree and remove its temp profile. Chrome keeps a few files
 * open briefly after termination, so the removal is retried.
 */
export async function closeBrowser(handle: BrowserHandle): Promise<void> {
  try {
    handle.proc.kill()
  } catch {
    /* already gone */
  }
  if (handle.proc.pid !== undefined) {
    spawnSync('taskkill', ['/PID', String(handle.proc.pid), '/T', '/F'], { stdio: 'ignore' })
  }
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      fs.rmSync(handle.profileDir, { recursive: true, force: true })
      return
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 300))
    }
  }
}

interface Pending {
  resolve: (value: unknown) => void
  reject: (error: Error) => void
}

export class CdpClient {
  channel: WsChannel
  nextId: number
  pending: Map<number, Pending>
  events: Array<{ method: string; params: unknown; sessionId?: string }>
  eventWaiters: Array<{ method: string; sessionId?: string; resolve: (params: unknown) => void }>
  pump: Promise<void>

  constructor(channel: WsChannel) {
    this.channel = channel
    this.nextId = 1
    this.pending = new Map()
    this.events = []
    this.eventWaiters = []
    this.pump = this.run()
  }

  static async connect(webSocketDebuggerUrl: string): Promise<CdpClient> {
    let lastError: Error | undefined
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        const channel = await WsChannel.connect(webSocketDebuggerUrl, {}, 4000)
        return new CdpClient(channel)
      } catch (error) {
        lastError = error as Error
        await new Promise((resolve) => setTimeout(resolve, 400))
      }
    }
    throw new Error(`CDP endpoint never completed a handshake: ${lastError?.message ?? 'unknown'}`)
  }

  async run(): Promise<void> {
    while (!this.channel.closed) {
      let frame
      try {
        frame = await this.channel.nextFrame(60_000)
      } catch {
        return
      }
      const message = JSON.parse(frame.payload) as {
        id?: number
        method?: string
        params?: unknown
        sessionId?: string
        error?: { message: string }
      }
      if (message.id !== undefined) {
        const waiter = this.pending.get(message.id)
        if (waiter) {
          this.pending.delete(message.id)
          if (message.error) waiter.reject(new Error(`CDP error: ${message.error.message}`))
          else waiter.resolve(message.result)
        }
        continue
      }
      if (message.method) {
        const index = this.eventWaiters.findIndex(
          (waiter) => waiter.method === message.method && waiter.sessionId === message.sessionId,
        )
        if (index >= 0) {
          const [waiter] = this.eventWaiters.splice(index, 1)
          waiter?.resolve(message.params)
        } else {
          this.events.push({ method: message.method, params: message.params, sessionId: message.sessionId })
        }
      }
    }
  }

  send(method: string, params: Record<string, unknown> = {}, sessionId?: string): Promise<unknown> {
    const id = this.nextId
    this.nextId += 1
    const payload: Record<string, unknown> = { id, method, params }
    if (sessionId) payload.sessionId = sessionId
    this.channel.send(JSON.stringify(payload))
    return new Promise<unknown>((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`CDP ${method} timed out`))
      }, 30_000)
    })
  }

  waitForEvent(method: string, timeoutMs = 15_000, sessionId?: string): Promise<unknown> {
    const queuedIndex = this.events.findIndex(
      (event) => event.method === method && event.sessionId === sessionId,
    )
    if (queuedIndex >= 0) {
      const [event] = this.events.splice(queuedIndex, 1)
      return Promise.resolve(event?.params)
    }
    return new Promise<unknown>((resolve, reject) => {
      const waiter = { method, sessionId, resolve }
      this.eventWaiters.push(waiter)
      setTimeout(() => {
        const index = this.eventWaiters.indexOf(waiter)
        if (index >= 0) {
          this.eventWaiters.splice(index, 1)
          reject(new Error(`CDP event ${method} not observed within ${timeoutMs}ms`))
        }
      }, timeoutMs)
    })
  }

  close(): void {
    this.channel.close()
  }
}

export interface PageHandle {
  targetId: string
  sessionId: string
}

/** Create a tab, attach to it and navigate to `url`. */
export async function openPage(client: CdpClient, url: string): Promise<PageHandle> {
  const created = (await client.send('Target.createTarget', { url: 'about:blank' })) as { targetId: string }
  const attached = (await client.send('Target.attachToTarget', {
    targetId: created.targetId,
    flatten: true,
  })) as { sessionId: string }
  const sessionId = attached.sessionId
  await client.send('Page.enable', {}, sessionId)
  await client.send('Runtime.enable', {}, sessionId)
  await client.send('Network.enable', {}, sessionId)
  const loaded = client.waitForEvent('Page.loadEventFired', 15_000, sessionId)
  await client.send('Page.navigate', { url }, sessionId)
  await loaded
  return { targetId: created.targetId, sessionId }
}

/** Evaluate an expression in a page and return its value. */
export async function evaluate(
  client: CdpClient,
  page: PageHandle,
  expression: string,
  timeoutMs = 20_000,
): Promise<unknown> {
  const result = (await Promise.race([
    client.send(
      'Runtime.evaluate',
      { expression, awaitPromise: true, returnByValue: true, userGesture: true },
      page.sessionId,
    ),
    new Promise((_, reject) => setTimeout(() => reject(new Error('Runtime.evaluate timed out')), timeoutMs)),
  ])) as { result?: { value?: unknown }; exceptionDetails?: { text?: string } }
  if (result.exceptionDetails) {
    throw new Error(`page exception: ${result.exceptionDetails.text ?? 'unknown'}`)
  }
  return result.result?.value
}

/** All cookies the browser holds for `url`. */
export async function cookiesFor(client: CdpClient, page: PageHandle, url: string): Promise<unknown> {
  const result = (await client.send('Network.getCookies', { urls: [url] }, page.sessionId)) as {
    cookies: Array<{ name: string; domain: string; path: string; sameSite?: string; httpOnly?: boolean; secure?: boolean }>
  }
  return result.cookies.map((cookie) => ({
    name: cookie.name,
    domain: cookie.domain,
    path: cookie.path,
    sameSite: cookie.sameSite,
    httpOnly: cookie.httpOnly,
    secure: cookie.secure,
    valueLength: '<redacted>',
  }))
}