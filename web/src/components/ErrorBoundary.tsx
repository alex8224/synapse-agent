import React, { Component, type ErrorInfo, type ReactNode } from 'react';

export interface ErrorBoundaryProps {
  children: ReactNode;
  /** Optional custom fallback element or render callback. */
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  /** Optional callback fired when an error is caught. */
  onError?: (error: Error, info: ErrorInfo) => void;
  /**
   * Layout level:
   * - `root`: full-viewport crash screen (used at main.tsx root)
   * - `panel`: contained card/strip for a specific region (e.g. transcript, composer)
   */
  level?: 'root' | 'panel';
  /** Optional title to help the reader identify which section crashed. */
  sectionTitle?: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  showDetails: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
    showDetails: false,
  };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { hasError: true, error };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('[ErrorBoundary caught error]', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  reset = (): void => {
    this.setState({ hasError: false, error: null, showDetails: false });
  };

  toggleDetails = (): void => {
    this.setState((prev) => ({ showDetails: !prev.showDetails }));
  };

  reloadPage = (): void => {
    if (typeof window !== 'undefined') {
      window.location.reload();
    }
  };

  override render(): ReactNode {
    const { hasError, error, showDetails } = this.state;
    const { children, fallback, level = 'panel', sectionTitle } = this.props;

    if (!hasError || error === null) {
      return children;
    }

    if (typeof fallback === 'function') {
      return fallback(error, this.reset);
    }
    if (fallback !== undefined) {
      return fallback;
    }

    const errorMessage = error.message || String(error);

    if (level === 'root') {
      return (
        <div className="min-h-screen w-full flex items-center justify-center bg-sunken p-6 text-gray-900 font-sans">
          <div className="max-w-lg w-full rounded-card border border-line bg-surface p-6 shadow-card">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-3 h-3 rounded-full bg-red-500" />
              <h1 className="text-base font-medium text-gray-900">
                控制台界面发生异常
              </h1>
            </div>
            <p className="text-sm text-gray-600 mb-4 leading-relaxed">
              渲染过程遇到意外错误（如浏览器扩展或外部 DOM 操作干扰）。您可以尝试重试渲染或刷新页面恢复。
            </p>
            <div className="flex items-center gap-3 mb-4">
              <button
                type="button"
                onClick={this.reset}
                className="px-3 py-1.5 text-xs font-medium rounded-control bg-accent text-on-accent hover:opacity-90 transition-opacity cursor-pointer"
              >
                重试渲染
              </button>
              <button
                type="button"
                onClick={this.reloadPage}
                className="px-3 py-1.5 text-xs font-medium rounded-control border border-line bg-surface hover:bg-surface-hover text-gray-800 transition-colors cursor-pointer"
              >
                刷新页面
              </button>
              <button
                type="button"
                onClick={this.toggleDetails}
                className="text-xs text-gray-500 hover:text-gray-700 underline cursor-pointer ml-auto"
              >
                {showDetails ? '隐藏详情' : '查看详情'}
              </button>
            </div>
            {showDetails && (
              <pre className="mt-3 p-3 rounded-control bg-sunken border border-line text-[11px] font-mono text-red-800 overflow-x-auto max-h-48 whitespace-pre-wrap break-all">
                {error.stack || errorMessage}
              </pre>
            )}
          </div>
        </div>
      );
    }

    return (
      <div className="my-2 p-3 rounded-control border border-red-200 bg-red-50/80 text-red-900 text-xs">
        <div className="flex items-center justify-between gap-2 mb-1">
          <span className="font-medium">
            {sectionTitle ? `${sectionTitle}渲染出错` : '部分界面渲染出错'}
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={this.reset}
              className="px-2 py-0.5 text-[11px] font-medium rounded border border-red-300 bg-surface text-red-800 hover:bg-red-50 transition-colors cursor-pointer"
            >
              重试
            </button>
            <button
              type="button"
              onClick={this.toggleDetails}
              className="text-[11px] text-red-700 underline cursor-pointer"
            >
              {showDetails ? '收起' : '详情'}
            </button>
          </div>
        </div>
        <p className="text-[11px] text-red-700/90 leading-tight">
          {errorMessage}
        </p>
        {showDetails && (
          <pre className="mt-2 p-2 rounded bg-red-100/70 text-[10px] font-mono text-red-900 overflow-x-auto max-h-32 whitespace-pre-wrap break-all">
            {error.stack || errorMessage}
          </pre>
        )}
      </div>
    );
  }
}
