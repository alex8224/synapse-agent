/**
 * Ad-hoc debugging harness (temporary): attach a headless browser to an already
 * running console host and dump every browser-side error, plus a DOM snapshot.
 *
 * Run:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/_consoleDebug.verify.ts [url]
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { CdpClient, closeBrowser, evaluate, launchBrowser, openPage } from './helpers/cdp.ts'
import type { BrowserHandle, PageHandle } from './helpers/cdp.ts'
import { httpProbe } from './helpers/httpProbe.ts'

const TARGET = process.argv[2] ?? 'http://127.0.0.1:8089'
const OUT_DIR = path.resolve(import.meta.dirname, '..', '..', 'work')

interface CdpEvent {
  method: string
  params: unknown
  sessionId?: string
}

function describeConsole(params: unknown): string {
  const entry = params as {
    type?: string
    args?: Array<{ type?: string; value?: unknown; description?: string; preview?: unknown }>
    stackTrace?: { callFrames?: Array<{ url?: string; lineNumber?: number }> }
  }
  const text = (entry.args ?? [])
    .map((arg) => {
      if (arg.value !== undefined) return JSON.stringify(arg.value)
      if (arg.description !== undefined) return arg.description
      return `<${arg.type ?? 'unknown'}>`
    })
    .join(' ')
  const frame = entry.stackTrace?.callFrames?.[0]
  const where = frame ? ` @ ${frame.url ?? '?'}:${frame.lineNumber ?? -1}` : ''
  return `[${entry.type ?? 'log'}] ${text}${where}`
}

function waitFor(client: CdpClient, page: PageHandle, expression: string, timeoutMs = 20_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  return (async () => {
    while (Date.now() < deadline) {
      if ((await evaluate(client, page, expression)) === true) return
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    throw new Error(`timed out waiting for: ${expression}`)
  })()
}

const SNAPSHOT = `(() => {
  const text = (element) => (element ? (element.innerText || '').slice(0, 400) : null);
  const banner = [...document.querySelectorAll('[role="alert"], .error, [data-error]')].map(text);
  return JSON.stringify({
    title: document.title,
    rootChildren: document.getElementById('root')?.children.length ?? -1,
    pairingGate: document.querySelector('#pairing-code') !== null,
    bodyHead: (document.body.innerText || '').slice(0, 1200),
    alerts: banner,
  });
})()`

async function dumpEvents(client: CdpClient, page: PageHandle): Promise<void> {
  const events = client.events.filter((event) => event.sessionId === page.sessionId)

  // Wire-level RPC trace: the console's own calls plus the daemon's answers.
  const frames = events.filter(
    (event) =>
      event.method === 'Network.webSocketFrameSent' ||
      event.method === 'Network.webSocketFrameReceived',
  )
  console.log('')
  console.log(`--- websocket frames (${frames.length}) ---`)
  for (const event of frames) {
    const params = event.params as { response?: { payloadData?: string } }
    const data = params.response?.payloadData ?? ''
    const direction = event.method === 'Network.webSocketFrameSent' ? '->' : '<-'
    const isError = data.includes('"error"')
    const isCall = data.includes('"method":"runtime.')
    if (!isError && !isCall) continue
    console.log(`  ${direction} ${data.slice(0, 500)}`)
  }

  const consoleEvents = events.filter((event) => event.method === 'Runtime.consoleAPICalled')
  const exceptions = events.filter((event) => event.method === 'Runtime.exceptionThrown')
  const logs = events.filter((event) => event.method === 'Log.entryAdded')
  const failedResponses = events.filter(
    (event) =>
      event.method === 'Network.responseReceived' &&
      ((event.params as { response?: { status?: number } }).response?.status ?? 0) >= 400,
  )
  const failures = events.filter((event) => event.method === 'Network.loadingFailed')

  const errorConsole = consoleEvents.filter((event) => {
    const type = (event.params as { type?: string }).type
    return type === 'error' || type === 'warning'
  })

  console.log('')
  console.log(`--- console messages with error/warning (${errorConsole.length}) ---`)
  for (const event of errorConsole) console.log('  ! ' + describeConsole(event.params))

  console.log(`--- uncaught exceptions (${exceptions.length}) ---`)
  for (const event of exceptions) {
    const detail = event.params as {
      exceptionDetails?: { text?: string; exception?: { description?: string }; url?: string; lineNumber?: number }
    }
    console.log(
      `  X ${detail.exceptionDetails?.text ?? '?'} :: ${detail.exceptionDetails?.exception?.description ?? ''} @ ${detail.exceptionDetails?.url ?? '?'}:${detail.exceptionDetails?.lineNumber ?? -1}`,
    )
  }

  console.log(`--- browser log entries (${logs.length}) ---`)
  for (const event of logs) {
    const entry = event.params as { entry?: { level?: string; text?: string; url?: string } }
    if (entry.entry?.level === 'error' || entry.entry?.level === 'warning') {
      console.log(`  l [${entry.entry.level}] ${entry.entry.text ?? ''} ${entry.entry.url ?? ''}`)
    }
  }

  console.log(`--- network responses >= 400 (${failedResponses.length}) ---`)
  for (const event of failedResponses) {
    const response = (event.params as { response?: { status?: number; url?: string } }).response
    console.log(`  n ${response?.status ?? 0} ${response?.url ?? ''}`)
  }

  console.log(`--- network loading failures (${failures.length}) ---`)
  for (const event of failures) {
    const params = event.params as { errorText?: string; type?: string }
    console.log(`  f ${params.type ?? ''} ${params.errorText ?? ''}`)
  }

  console.log(`--- all console messages (${consoleEvents.length}) ---`)
  for (const event of consoleEvents) {
    const type = (event.params as { type?: string }).type
    if (type === 'error' || type === 'warning') continue
    console.log('  . ' + describeConsole(event.params))
  }
}

async function main(): Promise<void> {
  console.log(`=== console debug against ${TARGET} ===`)
  const probe = await httpProbe({ url: `${TARGET}/api/session`, timeoutMs: 5000 })
  console.log(`/api/session -> ${probe.status}`)

  let browser: BrowserHandle | undefined
  let client: CdpClient | undefined
  try {
    browser = await launchBrowser()
    const version = JSON.parse(
      (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
    ) as { webSocketDebuggerUrl: string }
    client = await CdpClient.connect(version.webSocketDebuggerUrl)

    const page = await openPage(client, `${TARGET}/`)
    await waitFor(client, page, `document.readyState === 'complete'`)
    await new Promise((resolve) => setTimeout(resolve, 6000))

    const snapshot = await evaluate(client, page, `(() => { const raw = ${SNAPSHOT}; return raw; })()`)
    console.log('snapshot: ' + snapshot)

    const shot = (await client.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as {
      data: string
    }
    fs.mkdirSync(OUT_DIR, { recursive: true })
    fs.writeFileSync(path.join(OUT_DIR, 'console-debug.png'), Buffer.from(shot.data, 'base64'))
    console.log(`screenshot -> ${path.join(OUT_DIR, 'console-debug.png')}`)

    await dumpEvents(client, page)
  } finally {
    client?.close()
    if (browser) await closeBrowser(browser)
  }
}

await main()