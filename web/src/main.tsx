// Monkey-patch removeChild/insertBefore against external DOM mutations
// (browser extensions, translation, contenteditable) to avoid reconciliation fatal crash.
installDomMutationGuards()

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// KaTeX's own stylesheet (plus its fonts, which Vite emits next to the build
// assets).  Math is rendered from a bundled engine, so no CDN is contacted and
// the console keeps working with no network beyond the loopback host.
import 'katex/dist/katex.min.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary.tsx'
import { installDomMutationGuards } from './domGuard.ts'
import { initAppearance } from './stores/appearance.ts'

// Before the first render: the stored appearance (and the OS preference when it is
// "system") decides the `data-theme` the document carries, so the console never
// paints one palette for a frame and then swaps.
initAppearance()

createRoot(document.getElementById('root')!, {
  onUncaughtError(error, errorInfo) {
    console.error('[React root uncaught error]', error, errorInfo)
  },
  onRecoverableError(error, errorInfo) {
    console.warn('[React root recoverable error]', error, errorInfo)
  },
}).render(
  <StrictMode>
    <ErrorBoundary level="root">
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
