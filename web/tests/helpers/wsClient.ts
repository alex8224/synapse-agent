/**
 * Minimal real websocket client (RFC6455 over raw TCP) used by the phase-5 C2
 * verification. It exists because no websocket client dependency is installed
 * and because the browser `WebSocket` API cannot set an `Origin` header, which
 * is exactly the header under test.
 */
import crypto from 'node:crypto'
import net from 'node:net'

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

export interface WsHandshake {
  status: number
  statusLine: string
  headers: Record<string, string>
  rawHead: string
  /** True when the server echoed a correct `Sec-WebSocket-Accept`. */
  acceptOk: boolean
  socket: net.Socket
  /** Bytes received after the response head (may already hold a frame). */
  leftover: Buffer
}

/** Encode a masked (client -> server) text frame. */
export function clientTextFrame(payload: string): Buffer {
  const data = Buffer.from(payload, 'utf8')
  const mask = crypto.randomBytes(4)
  const header =
    data.length < 126
      ? Buffer.from([0x81, 0x80 | data.length])
      : (() => {
          const buf = Buffer.alloc(4)
          buf[0] = 0x81
          buf[1] = 0x80 | 126
          buf.writeUInt16BE(data.length, 2)
          return buf
        })()
  const masked = Buffer.alloc(data.length)
  for (let i = 0; i < data.length; i += 1) masked[i] = (data[i] ?? 0) ^ (mask[i % 4] ?? 0)
  return Buffer.concat([header, mask, masked])
}

function parseHead(text: string): { statusLine: string; headers: Record<string, string> } {
  const lines = text.split('\r\n')
  const statusLine = lines.shift() ?? ''
  const headers: Record<string, string> = {}
  for (const line of lines) {
    const index = line.indexOf(':')
    if (index <= 0) continue
    headers[line.slice(0, index).trim().toLowerCase()] = line.slice(index + 1).trim()
  }
  return { statusLine, headers }
}

/**
 * Perform a real websocket handshake against `url`, adding `extraHeaders`.
 * Resolves once the response head is complete (101 or an HTTP error status).
 */
export async function wsHandshake(
  url: string,
  extraHeaders: Record<string, string> = {},
  timeoutMs = 5000,
): Promise<WsHandshake> {
  const target = new URL(url)
  const key = crypto.randomBytes(16).toString('base64')
  const lines = [
    `GET ${target.pathname}${target.search} HTTP/1.1`,
    `Host: ${target.host}`,
    'Upgrade: websocket',
    'Connection: Upgrade',
    `Sec-WebSocket-Key: ${key}`,
    'Sec-WebSocket-Version: 13',
  ]
  for (const [name, value] of Object.entries(extraHeaders)) lines.push(`${name}: ${value}`)
  const request = `${lines.join('\r\n')}\r\n\r\n`

  const socket = net.connect({ host: target.hostname.replace(/^\[|\]$/g, ''), port: Number(target.port || 80) })
  socket.setNoDelay(true)

  const expectedAccept = crypto.createHash('sha1').update(key + WS_GUID).digest('base64')

  return new Promise<WsHandshake>((resolve, reject) => {
    let buffer = Buffer.alloc(0)
    const timer = setTimeout(() => {
      socket.destroy()
      reject(new Error(`websocket handshake to ${url} timed out after ${timeoutMs}ms`))
    }, timeoutMs)

    const onData = (chunk: Buffer): void => {
      buffer = Buffer.concat([buffer, chunk])
      const end = buffer.indexOf('\r\n\r\n')
      if (end < 0) return
      clearTimeout(timer)
      socket.off('data', onData)
      const rawHead = buffer.subarray(0, end).toString('latin1')
      const { statusLine, headers } = parseHead(rawHead)
      const status = Number(statusLine.split(' ')[1] ?? 0)
      resolve({
        status,
        statusLine,
        headers,
        rawHead,
        acceptOk: headers['sec-websocket-accept'] === expectedAccept,
        socket,
        leftover: buffer.subarray(end + 4),
      })
    }

    socket.on('data', onData)
    socket.on('error', (error) => {
      clearTimeout(timer)
      reject(error)
    })
    socket.write(request)
  })
}

/** Read one unmasked server frame, using bytes already buffered when possible. */
export function readServerFrame(
  socket: net.Socket,
  leftover: Buffer = Buffer.alloc(0),
  timeoutMs = 5000,
): Promise<{ opcode: number; payload: string }> {
  return new Promise((resolve, reject) => {
    let buffer = leftover
    const timer = setTimeout(() => {
      socket.off('data', onData)
      reject(new Error(`no server frame within ${timeoutMs}ms`))
    }, timeoutMs)

    const tryParse = (): boolean => {
      if (buffer.length < 2) return false
      const first = buffer[0] ?? 0
      const opcode = first & 0x0f
      const masked = ((buffer[1] ?? 0) & 0x80) !== 0
      let length = (buffer[1] ?? 0) & 0x7f
      let offset = 2
      if (length === 126) {
        if (buffer.length < 4) return false
        length = buffer.readUInt16BE(2)
        offset = 4
      } else if (length === 127) {
        if (buffer.length < 10) return false
        length = Number(buffer.readBigUInt64BE(2))
        offset = 10
      }
      if (masked) offset += 4
      if (buffer.length < offset + length) return false
      const payload = buffer.subarray(offset, offset + length).toString('utf8')
      clearTimeout(timer)
      socket.off('data', onData)
      resolve({ opcode, payload })
      return true
    }

    const onData = (chunk: Buffer): void => {
      buffer = Buffer.concat([buffer, chunk])
      tryParse()
    }

    if (!tryParse()) {
      socket.on('data', onData)
      socket.on('error', (error) => {
        clearTimeout(timer)
        reject(error)
      })
    }
  })
}