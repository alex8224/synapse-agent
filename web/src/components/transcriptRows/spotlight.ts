import type React from 'react';

/**
 * Point the Fluent spotlight at the pointer inside the hovered surface.
 *
 * The surface carries `fluent-spotlight`, whose gradient is centred on these two
 * custom properties; the row that owns the pointer writes them, so the highlight
 * follows the cursor instead of the element's own centre.
 */
export function updateSpotlight(event: React.MouseEvent<HTMLElement>): void {
  const rect = event.currentTarget.getBoundingClientRect();
  event.currentTarget.style.setProperty('--mouse-x', `${event.clientX - rect.left}px`);
  event.currentTarget.style.setProperty('--mouse-y', `${event.clientY - rect.top}px`);
}
