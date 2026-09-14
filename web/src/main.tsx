import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
// KaTeX's own stylesheet (plus its fonts, which Vite emits next to the build
// assets).  Math is rendered from a bundled engine, so no CDN is contacted and
// the console keeps working with no network beyond the loopback host.
import 'katex/dist/katex.min.css'
import App from './App.tsx'
import { initAppearance } from './stores/appearance.ts'

// Before the first render: the stored appearance (and the OS preference when it is
// "system") decides the `data-theme` the document carries, so the console never
// paints one palette for a frame and then swaps.
initAppearance()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
