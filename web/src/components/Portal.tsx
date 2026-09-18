/**
 * Renders its children into `document.body`.
 *
 * Modals (and anything else positioned `fixed`) must not live inside the shell's
 * subtrees: an ancestor with `backdrop-filter` -- which the acrylic themes put on
 * the sidebar and the header -- becomes the containing block for `fixed`
 * descendants, so a dialog rendered inside the sidebar is positioned *relative to
 * the sidebar* (it looked docked to the rail) instead of centred in the window.
 * Portalling to the body keeps that decision independent of what the chrome's
 * material does.
 */
import React from 'react';
import { createPortal } from 'react-dom';

export interface PortalProps {
  children: React.ReactNode;
}

export const Portal: React.FC<PortalProps> = ({ children }) =>
  typeof document === 'undefined' ? <>{children}</> : createPortal(children, document.body);