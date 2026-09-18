import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { ArtifactsPanel } from './ArtifactsPanel.tsx';

/**
 * Hosts the centered file manager opened from a file path in the transcript.
 *
 * Mounted once at the app root; the store holds the requested path, so any
 * transcript row can ask for a file without threading props through the tree.
 * The `key` is the request id, so a repeat click on the same path remounts the
 * panel and re-reads the file instead of showing the previous selection.
 */
export const FileViewerHost: React.FC = () => {
  const fileViewer = useConsoleStore((state) => state.fileViewer);
  const closeFileViewer = useConsoleStore((state) => state.closeFileViewer);
  if (fileViewer === null) return null;
  return (
    <ArtifactsPanel
      key={fileViewer.requestId}
      centered
      initialPath={fileViewer.path}
      onClose={closeFileViewer}
    />
  );
};
