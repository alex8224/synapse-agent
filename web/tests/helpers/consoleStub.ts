/**
 * Controlled console-host stub for the phase-5 C2 dev-proxy verification.
 *
 * This is *not* a substitute for the real `synapse-web-console` host: it exists
 * only so that the exact bytes a proxied request carries (`Origin`, `Host`,
 * `Cookie`, `Authorization`, ...) can be observed on the wire. It completes a
 * real RFC6455 server handshake so a real websocket client can drive it.
 */
import crypto from 'node:crypto'
import http from 'node:http'
import type { AddressInfo } from 'node:net'

/** RFC6455 handshake GUID. */
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

/** Headers whose values must never be echoed verbatim into evidence. */
const REDACTED_HEADERS = new Set(['cookie', 'authorization', 'sec-websocket-key'])

export interface ObservedRequest {
  kind: 'http' | 'upgrade'
  method: string
  url: string
  headers: Record<string, string | string[] | undefined>
  /** Request head as received, with credential-shaped header values redacted. */
  rawHead: string
}

export interface ConsoleStub {
  /** Origin of the stub host, e.g. `http://127.0.0.1:53421`. */
  origin: string
  port: number
  /** Every request the stub actually received, in arrival order. */
  observed: ObservedRequest[]
  /** Every TCP connection the stub accepted, with its first bytes. */
  connections: Array<{ remote: string; firstBytes: string }>
  /** Length of the synthetic session cookie value, or 0 when disabled. */
  sessionCookieLength: number
  close(): Promise<void>
}

export interface ConsoleStubOptions {
  /**
   * Mirror the A4 cookie contract on `GET /`: HttpOnly, SameSite=Strict, Path=/,
   * no Domain, no Secure, Max-Age=43200. The value is synthetic and random; it
   * is never printed, only its length is exposed.
   */
  sessionCookie?: boolean
}

/** Render `req.rawHeaders` as a head block, redacting credential-shaped values. */
function rawHeadOf(req: http.IncomingMessage): string {
  const lines: string[] = [`${req.method ?? '?'} ${req.url ?? '/'} HTTP/1.1`]
  for (let i = 0; i < req.rawHeaders.length; i += 2) {
    const name = req.rawHeaders[i] ?? ''
    const value = req.rawHeaders[i + 1] ?? ''
    lines.push(
      REDACTED_HEADERS.has(name.toLowerCase())
        ? `${name}: <redacted ${Buffer.byteLength(value)} bytes>`
        : `${name}: ${value}`,
    )
  }
  return lines.join('\r\n')
}

/** Encode an unmasked (server -> client) text frame. */
export function serverTextFrame(payload: string): Buffer {
  const data = Buffer.from(payload, 'utf8')
  if (data.length < 126) {
    return Buffer.concat([Buffer.from([0x81, data.length]), data])
  }
  const header = Buffer.alloc(4)
  header[0] = 0x81
  header[1] = 126
  header.writeUInt16BE(data.length, 2)
  return Buffer.concat([header, data])
}

/** Start the stub on a kernel-assigned loopback port. */
export async function startConsoleStub(options: ConsoleStubOptions = {}): Promise<ConsoleStub> {
  const observed: ObservedRequest[] = []
  const connections: Array<{ remote: string; firstBytes: string }> = []
  const sessionCookieValue = options.sessionCookie
    ? crypto.randomBytes(32).toString('base64url')
    : ''

  const server = http.createServer((req, res) => {
    observed.push({
      kind: 'http',
      method: req.method ?? '',
      url: req.url ?? '',
      headers: req.headers,
      rawHead: rawHeadOf(req),
    })
    const headers: Record<string, string> = {
      'content-type': 'application/json',
      'cache-control': 'no-store',
    }
    if (sessionCookieValue && req.method === 'GET' && (req.url ?? '/') === '/') {
      headers['set-cookie'] =
        `synapse_web_session=${sessionCookieValue}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200`
    }
    res.writeHead(200, headers)
    res.end(JSON.stringify({ stub: true, path: req.url }))
  })

  server.on('connection', (socket) => {
    socket.on('error', () => {
      /* a killed browser resets its sockets; the stub only observes */
    })
    const record = { remote: `${socket.remoteAddress}:${socket.remotePort}`, firstBytes: '<none>' }
    connections.push(record)
    socket.once('data', (chunk: Buffer) => {
      record.firstBytes = chunk.toString('latin1').slice(0, 400)
    })
  })

  server.on('upgrade', (req, socket) => {
    socket.on('error', () => {
      /* a killed browser resets its sockets; the stub only observes */
    })
    observed.push({
      kind: 'upgrade',
      method: req.method ?? '',
      url: req.url ?? '',
      headers: req.headers,
      rawHead: rawHeadOf(req),
    })
    const key = req.headers['sec-websocket-key']
    if (typeof key !== 'string') {
      socket.destroy()
      return
    }
    const accept = crypto.createHash('sha1').update(key + WS_GUID).digest('base64')
    socket.write(
      [
        'HTTP/1.1 101 Switching Protocols',
        'Upgrade: websocket',
        'Connection: Upgrade',
        `Sec-WebSocket-Accept: ${accept}`,
        '',
        '',
      ].join('\r\n'),
    )
    // One server frame so the client can prove the session is genuinely live.
    socket.write(serverTextFrame('stub-ready'))
  })

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port

  return {
    origin: `http://127.0.0.1:${port}`,
    port,
    observed,
    connections,
    sessionCookieLength: sessionCookieValue.length,
    close: () =>
      new Promise<void>((resolve) => {
        const done = (): void => resolve()
        const timer = setTimeout(done, 2000)
        server.closeAllConnections()
        server.close(() => {
          clearTimeout(timer)
          done()
        })
      }),
  }
}