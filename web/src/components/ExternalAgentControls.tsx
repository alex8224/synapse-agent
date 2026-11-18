import { Checkmark16Regular, ChevronDown20Regular, Bot20Regular, LockClosed20Regular } from '@fluentui/react-icons';
import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import type { ExternalSessionState } from '../stores/useConsoleStore.ts';

/**
 * The composer controls for a session backed by an external ACP agent.
 *
 * The options are the *peer's* snapshot (`runtime.external.session.config`):
 * this console never invents a model list, and a rejected write leaves the
 * previous value in force. `full_access` is a daemon-side launch decision, so it
 * is shown as a badge and never offered as a toggle here.
 */
export const ExternalAgentControls: React.FC<{ session: ExternalSessionState }> = ({ session }) => {
  const setExternalConfig = useConsoleStore((state) => state.setExternalConfig);
  const error = useConsoleStore((state) => state.externalSessionError);
  const [openId, setOpenId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  const choose = async (configId: string, value: string | boolean) => {
    setPendingId(configId);
    const accepted = await setExternalConfig(configId, value);
    setPendingId(null);
    if (accepted) setOpenId(null);
  };

  return (
    <>
      {session.options.map((option) => {
        const values = option.values;
        const label = option.bool_value === null
          ? (values.find((v) => v.value === option.current_value)?.label ?? option.current_value)
          : option.bool_value ? '开启' : '关闭';
        const busy = pendingId === option.id;
        return (
          <div key={option.id} className="relative min-w-0">
            <button
              type="button"
              onClick={() => setOpenId((current) => (current === option.id ? null : option.id))}
              aria-expanded={openId === option.id}
              aria-controls={`external-option-${option.id}`}
              title={`${option.name}（由外部 Agent 提供）`}
              className="ui-button ui-model-trigger"
              disabled={busy}
            >
              <Bot20Regular aria-hidden="true" />
              <span className="max-w-[12rem] truncate">{busy ? '切换中…' : label}</span>
              <ChevronDown20Regular aria-hidden="true" />
            </button>
            {openId === option.id && (
              <div
                id={`external-option-${option.id}`}
                role="group"
                aria-label={option.name}
                className="absolute bottom-full right-0 z-50 mb-2 w-64 rounded-card border border-line/80 material-flyout flyout-in p-2 shadow-flyout"
              >
                <div className="border-b border-line/60 px-2 pb-1.5 text-xs font-semibold text-gray-700">
                  {option.name}
                </div>
                <div className="fluent-scrollbar mt-1 max-h-60 space-y-0.5 overflow-y-auto">
                  {values.map((value) => {
                    const selected = value.value === option.current_value;
                    return (
                      <button
                        key={value.value}
                        type="button"
                        aria-pressed={selected}
                        onClick={() => void choose(option.id, value.value)}
                        className={`ui-menu-item flex w-full items-center justify-between rounded-control px-2.5 py-1.5 text-left text-xs ${
                          selected ? 'bg-blue-50/90 font-medium text-blue-500' : 'text-gray-700 hover:bg-gray-100/70'
                        }`}
                      >
                        <span className="truncate">{value.label}</span>
                        {selected && <Checkmark16Regular aria-hidden="true" className="shrink-0 text-accent-fg" />}
                      </button>
                    );
                  })}
                  {option.bool_value !== null &&
                    ([true, false] as const).map((flag) => {
                      const selected = option.bool_value === flag;
                      return (
                        <button
                          key={String(flag)}
                          type="button"
                          aria-pressed={selected}
                          onClick={() => void choose(option.id, flag)}
                          className={`ui-menu-item flex w-full items-center justify-between rounded-control px-2.5 py-1.5 text-left text-xs ${
                            selected ? 'bg-blue-50/90 font-medium text-blue-500' : 'text-gray-700 hover:bg-gray-100/70'
                          }`}
                        >
                          <span className="truncate">{flag ? '开启' : '关闭'}</span>
                          {selected && <Checkmark16Regular aria-hidden="true" className="shrink-0 text-accent-fg" />}
                        </button>
                      );
                    })}
                  {option.bool_value === null && values.length === 0 && (
                    <div className="px-2 py-1 text-[11px] text-gray-500">该选项由 Agent 管理</div>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}
      {session.options.length === 0 && (
        <span className="px-1 text-[11px] text-gray-500">该 Agent 未提供可切换配置</span>
      )}
      {session.permissionMode === 'full_access' && (
        <span
          title="该 Agent 以本机配置的完全权限启动；审批不在控制台内"
          className="flex items-center gap-1 rounded-control bg-amber-50 px-2 py-1 text-[11px] font-medium text-amber-700"
        >
          <LockClosed20Regular aria-hidden="true" />
          完全权限
        </span>
      )}
      {error !== null && <span className="px-1 text-[11px] text-red-600">{error}</span>}
    </>
  );
};
