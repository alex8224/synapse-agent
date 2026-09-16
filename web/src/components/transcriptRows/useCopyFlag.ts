import { useState } from 'react';

/**
 * Copy a row's text and flag its button for a moment.
 *
 * The flag is per row, so a row that copies does not re-render its neighbours, and
 * every row that offers a copy button shares one behaviour instead of repeating it.
 */
export function useCopyFlag(): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const copy = (text: string) => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    if (!clipboard || !text) return;
    void clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };
  return [copied, copy];
}
