/**
 * Monochrome 15px mark for the external ACP (Antigravity / `agy`) usage entry.
 *
 * Follows the same two rules as `CodexMark.tsx`:
 * - inherits `currentColor` so the strip can tint it red below 50% remaining,
 * - sized to 15px to match the neighbouring Fluent glyphs in the 28px bar.
 */
import React from 'react';

export const AGY_MARK_PX = 15;

export const AgyMark: React.FC<{ className?: string }> = ({ className }) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 24 24"
    width={AGY_MARK_PX}
    height={AGY_MARK_PX}
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    className={className}
  >
    <path d="M12 2.25c.46 0 .88.26 1.08.68l7.35 15.62c.36.77-.37 1.6-1.18 1.34l-5.32-1.7a5.9 5.9 0 00-3.86 0l-5.32 1.7c-.81.26-1.54-.57-1.18-1.34L10.92 2.93c.2-.42.62-.68 1.08-.68zm0 4.35L7.12 16.97l2.22-.71a8.25 8.25 0 015.32 0l2.22.71L12 6.6z" />
  </svg>
);
