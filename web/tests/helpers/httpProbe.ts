/**
 * Thin real HTTP client for the phase-5 C2 verification: it must be able to send
 * arbitrary `Origin`/`Host`/`Cookie` headers, which the WHATWG `fetch` wrapper
 * does not allow.
 */
import http from 'node:http'

export interface HttpProbeResult {
  status: number
  statusLine: string
  headers: http.IncomingHttpHeaders
  rawHead: string
  body: string
}

export interface HttpProbeOptions {
  url: string
  method?: string
  headers?: Record<string, string>
  body?: string
  timeoutMs?: number
}

/** Perform one HTTP request and return status, headers and body. */
export function httpProbe(options: HttpProbeOptions): Promise<HttpProbeResult> {
  const target = new URL(options.url)
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: target.hostname.replace(/^\[|\]$/g, ''),
        port: Number(target.port || 80),
        path: `${target.pathname}${target.search}`,
        method: options.method ?? 'GET',
        headers: options.headers,
      },
      (response) => {
        const chunks: Buffer[] = []
        response.on('data', (chunk: Buffer) => chunks.push(chunk))
        response.on('end', () => {
          const statusLine = `${response.httpVersion} ${response.statusCode ?? 0} ${response.statusMessage ?? ''}`
          resolve({
            status: response.statusCode ?? 0,
            statusLine,
            headers: response.headers,
            rawHead: JSON.stringify(response.rawHeaders),
            body: Buffer.concat(chunks).toString('utf8'),
          })
        })
      },
    )
    request.setTimeout(options.timeoutMs ?? 5000, () => {
      request.destroy(new Error(`request to ${options.url} timed out`))
    })
    request.on('error', reject)
    if (options.body !== undefined) request.write(options.body)
    request.end()
  })
}