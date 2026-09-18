import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Development only. Vite implements no business logic and never reads token
// files: it hot-reloads the React app and proxies the formal console endpoints
// (`/api/*` + `/runtime-ws`) to the `synapse-web-console` host, which is the
// same production host. The production path never involves Vite.
//
// The dev proxy is deliberately not an authentication bypass:
//   * it only ever targets a loopback console host - an unusable value fails
//     config load instead of silently proxying somewhere else;
//   * it never talks to the daemon and never injects a credential header. It
//     only rewrites `Origin` to the console origin so the host's strict
//     same-origin check sees the request the way a browser on the console
//     origin would have sent it (the host relaxes nothing for dev);
//   * `server.host` stays at the default loopback bind.
// Pairing is still required in dev: the code is read from the host stderr.
const DEFAULT_CONSOLE_URL = 'http://127.0.0.1:8080'

/** Loopback hosts accepted as the dev proxy target (mirrors the host config). */
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]'])

/** Normalize `SYNAPSE_WEB_CONSOLE_URL` into an explicit origin, or throw. */
function resolveConsoleOrigin(raw: string | undefined): string {
  const value = (raw ?? '').trim() || DEFAULT_CONSOLE_URL
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new Error(`SYNAPSE_WEB_CONSOLE_URL is not a valid URL: ${value}`)
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`SYNAPSE_WEB_CONSOLE_URL must use http or https: ${value}`)
  }
  const host = url.hostname.toLowerCase()
  if (!LOOPBACK_HOSTS.has(host)) {
    throw new Error(
      `SYNAPSE_WEB_CONSOLE_URL must point at a loopback console host (127.0.0.1, localhost, [::1]): ${value}`,
    )
  }
  if (url.username || url.password) {
    throw new Error('SYNAPSE_WEB_CONSOLE_URL must not embed credentials')
  }
  if (url.pathname !== '/' || url.search || url.hash) {
    throw new Error(`SYNAPSE_WEB_CONSOLE_URL must be a bare origin: ${value}`)
  }
  const port = url.port || (url.protocol === 'https:' ? '443' : '80')
  return `${url.protocol}//${host}:${port}`
}

// Evaluated at config load: an unusable target aborts `npm run dev`/`build`
// instead of quietly proxying the console API to the wrong place.
const consoleOrigin = resolveConsoleOrigin(process.env.SYNAPSE_WEB_CONSOLE_URL)

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: consoleOrigin,
        changeOrigin: true,
        headers: { Origin: consoleOrigin },
      },
      '/runtime-ws': {
        target: consoleOrigin,
        ws: true,
        changeOrigin: true,
        headers: { Origin: consoleOrigin },
      },
    },
  },
})
