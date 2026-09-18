/**
 * A long-lived websocket channel over raw TCP (RFC6455), used by the phase-5 C2
 * browser experiment to speak the Chrome DevTools Protocol. Raw sockets are used
 * deliberately: the sandbox exports `NODE_USE_ENV_PROXY`, which would otherwise
 * send loopback traffic to an external proxy.
 */
import crypto from 'node:crypto'
import net from 'node:net'

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

export interface WsFrame {
  opcode: number
  payload: string
}

interface Waiter {
  resolve: (frame: WsFrame) => void
  reject: (error: Error) => void
  timer: ReturnType<typeof setTimeout>
}

function encodeFrame(opcode: number, payload: string): Buffer {
  const data = Buffer.from(payload, 'utf8')
  const mask = crypto.randomBytes(4)
  let header: Buffer
  if (data.length < 126) {
    header = Buffer.from([0x80 | opcode, 0x80 | data.length])
  } else if (data.length < 65536) {
    header = Buffer.alloc(4)
    header[0] = 0x80 | opcode
    header[1] = 0x80 | 126
    header.writeUInt16BE(data.length, 2)
  } else {
    header = Buffer.alloc(10)
    header[0] = 0x80 | opcode
    header[1] = 0x80 | 127
    header.writeBigUInt64BE(BigInt(data.length), 2)
  }
  const masked = Buffer.alloc(data.length)
  for (let i = 0; i < data.length; i += 1) masked[i] = (data[i] ?? 0) ^ (mask[i % 4] ?? 0)
  return Buffer.concat([header, mask, masked])
}

export class WsChannel {
  socket: net.Socket
  buffer: Buffer
  frames: WsFrame[]
  waiters: Waiter[]
  closed: boolean
  closeReason: string

  constructor(socket: net.Socket, leftover: Buffer) {
    this.socket = socket
    this.buffer = leftover
    this.frames = []
    this.waiters = []
    this.closed = false
    this.closeReason = ''
    socket.on('data', (chunk: Buffer) => {
      this.buffer = Buffer.concat([this.buffer, chunk])
      this.drain()
    })
    socket.on('error', (error: Error) => {
      this.closed = true
      this.closeReason = error.message
      this.rejectAll(new Error(`websocket error: ${error.message}`))
    })
    socket.on('close', () => {
      this.closed = true
      if (!this.closeReason) this.closeReason = 'closed'
      this.rejectAll(new Error('websocket closed'))
    })
    this.drain()
  }

  /** Open a channel and complete the RFC6455 client handshake. */
  static connect(
    url: string,
    headers: Record<string, string> = {},
    timeoutMs = 10_000,
  ): Promise<WsChannel> {
    const target = new URL(url)
    const key = crypto.randomBytes(16).toString('base64')
    const expected = crypto.createHash('sha1').update(key + WS_GUID).digest('base64')
    const lines = [
      `GET ${target.pathname}${target.search} HTTP/1.1`,
      `Host: ${target.host}`,
      'Upgrade: websocket',
      'Connection: Upgrade',
      `Sec-WebSocket-Key: ${key}`,
      'Sec-WebSocket-Version: 13',
    ]
    for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`)
    const request = `${lines.join('\r\n')}\r\n\r\n`
    const socket = net.connect({
      host: target.hostname.replace(/^\[|\]$/g, ''),
      port: Number(target.port || 80),
    })
    socket.setNoDelay(true)

    return new Promise<WsChannel>((resolve, reject) => {
      let buffer = Buffer.alloc(0)
      const timer = setTimeout(() => {
        socket.destroy()
        reject(new Error(`websocket handshake to ${url} timed out`))
      }, timeoutMs)
      const onData = (chunk: Buffer): void => {
        buffer = Buffer.concat([buffer, chunk])
        const end = buffer.indexOf('\r\n\r\n')
        if (end < 0) return
        clearTimeout(timer)
        socket.off('data', onData)
        const head = buffer.subarray(0, end).toString('latin1')
        const status = Number(head.split(' ')[1] ?? 0)
        const accept = /sec-websocket-accept:\s*(\S+)/i.exec(head)?.[1]
        if (status !== 101 || accept !== expected) {
          socket.destroy()
          reject(new Error(`websocket handshake failed: ${head.split('\r\n')[0]}`))
          return
        }
        resolve(new WsChannel(socket, buffer.subarray(end + 4)))
      }
      socket.on('data', onData)
      socket.on('error', (error) => {
        clearTimeout(timer)
        reject(error)
      })
      socket.write(request)
    })
  }

  rejectAll(error: Error): void {
    for (const waiter of this.waiters.splice(0)) {
      clearTimeout(waiter.timer)
      waiter.reject(error)
    }
  }

  drain(): void {
    while (true) {
      if (this.buffer.length < 2) return
      const opcode = (this.buffer[0] ?? 0) & 0x0f
      const masked = ((this.buffer[1] ?? 0) & 0x80) !== 0
      let length = (this.buffer[1] ?? 0) & 0x7f
      let offset = 2
      if (length === 126) {
        if (this.buffer.length < 4) return
        length = this.buffer.readUInt16BE(2)
        offset = 4
      } else if (length === 127) {
        if (this.buffer.length < 10) return
        length = Number(this.buffer.readBigUInt64BE(2))
        offset = 10
      }
      let mask: Buffer | undefined
      if (masked) {
        if (this.buffer.length < offset + 4) return
        mask = this.buffer.subarray(offset, offset + 4)
        offset += 4
      }
      if (this.buffer.length < offset + length) return
      const raw = this.buffer.subarray(offset, offset + length)
      const data = Buffer.alloc(length)
      for (let i = 0; i < length; i += 1) {
        data[i] = mask ? (raw[i] ?? 0) ^ (mask[i % 4] ?? 0) : (raw[i] ?? 0)
      }
      this.buffer = this.buffer.subarray(offset + length)
      const frame: WsFrame = { opcode, payload: data.toString('utf8') }
      if (opcode === 0x9) {
        this.socket.write(encodeFrame(0xa, frame.payload))
        continue
      }
      if (opcode === 0x8) {
        this.closed = true
        this.closeReason = 'peer close'
        this.rejectAll(new Error('websocket closed by peer'))
        this.socket.end()
        continue
      }
      const waiter = this.waiters.shift()
      if (waiter) {
        clearTimeout(waiter.timer)
        waiter.resolve(frame)
      } else {
        this.frames.push(frame)
      }
    }
  }

  send(text: string): void {
    this.socket.write(encodeFrame(0x1, text))
  }

  nextFrame(timeoutMs = 10_000): Promise<WsFrame> {
    const queued = this.frames.shift()
    if (queued) return Promise.resolve(queued)
    if (this.closed) return Promise.reject(new Error(`websocket closed: ${this.closeReason}`))
    return new Promise<WsFrame>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((waiter) => waiter.timer !== timer)
        reject(new Error('no websocket frame within timeout'))
      }, timeoutMs)
      this.waiters.push({ resolve, reject, timer })
    })
  }

  close(): void {
    try {
      this.socket.destroy()
    } catch {
      /* already gone */
    }
  }
}