/**
 * Model maintenance and endpoint configuration section for the settings surface.
 *
 * Implements Fluent Design 2 patterns:
 * - Featured Global Default Model card pinned at the foremost, prominent position
 * - Fluid accordion animation for collapsible provider groups
 * - System standard controls (.ui-button, .ui-field, .ui-primary, .ui-danger)
 * - Acrylic/Mica material flyouts with standard entrance motion
 * - Role-based tokens and elevations
 */
import {
  Add16Regular,
  Bot16Regular,
  Checkmark16Regular,
  ChevronDown16Regular,
  ChevronRight16Regular,
  Delete16Regular,
  Dismiss20Regular,
  Edit16Regular,
  PlugConnected16Regular,
  Star16Filled,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useState } from 'react';
import { Portal } from './Portal.tsx';
import type { ModelListResult, ModelSummary, TestModelResult } from '../runtime-client/types.ts';
import { useConsoleStore } from '../stores/useConsoleStore.ts';

interface ProviderMeta {
  key: string;
  label: string;
  prefix: string;
  defaultBaseUrl: string;
}

const PROVIDER_METAS: ProviderMeta[] = [
  {
    key: 'openai',
    label: 'OpenAI / 兼容网关',
    prefix: 'openai:',
    defaultBaseUrl: 'https://api.openai.com/v1',
  },
  {
    key: 'anthropic',
    label: 'Anthropic',
    prefix: 'anthropic:',
    defaultBaseUrl: 'https://api.anthropic.com',
  },
  {
    key: 'openai_oauth',
    label: 'ChatGPT (Codex OAuth)',
    prefix: 'openai:',
    defaultBaseUrl: 'https://chatgpt.com/backend-api/codex',
  },
  {
    key: 'other',
    label: '其他自定义网关',
    prefix: '',
    defaultBaseUrl: '',
  },
];

interface ModelFormData {
  alias: string;
  modelId: string;
  provider: string;
  baseUrl: string;
  apiKey: string;
  openaiProxy: string;
  websocket: 'auto' | 'yes' | 'no';
  contextWindow: string;
  imageInput: 'auto' | 'yes' | 'no';
  reasoningEffort: string;
  makeDefault: boolean;
  rawExtra: Record<string, unknown>;
}

const EMPTY_FORM: ModelFormData = {
  alias: '',
  modelId: '',
  provider: 'openai',
  baseUrl: '',
  apiKey: '',
  openaiProxy: '',
  websocket: 'auto',
  contextWindow: '',
  imageInput: 'auto',
  reasoningEffort: '',
  makeDefault: false,
  rawExtra: {},
};

function resolveGroupKey(model: ModelSummary): string {
  if (
    model.provider === 'openai_oauth' ||
    model.alias.toLowerCase().startsWith('codex') ||
    model.model.toLowerCase().includes('codex')
  ) {
    return 'openai_oauth';
  }
  if (model.provider === 'anthropic' || model.model.startsWith('anthropic:')) return 'anthropic';
  if (model.provider === 'openai' || model.model.startsWith('openai:')) return 'openai';
  return 'other';
}

function extractModelIdAndProvider(model: ModelSummary): { modelId: string; provider: string } {
  const rawModel = model.model.trim();
  const groupKey = resolveGroupKey(model);
  if (groupKey === 'openai_oauth') {
    return {
      modelId: rawModel.replace(/^openai:/i, ''),
      provider: 'openai_oauth',
    };
  }
  if (rawModel.startsWith('anthropic:')) {
    return {
      modelId: rawModel.slice(10),
      provider: 'anthropic',
    };
  }
  if (rawModel.startsWith('openai:')) {
    return {
      modelId: rawModel.slice(7),
      provider: 'openai',
    };
  }
  if (model.provider && PROVIDER_METAS.some((p) => p.key === model.provider)) {
    return {
      modelId: rawModel,
      provider: model.provider,
    };
  }
  return {
    modelId: rawModel,
    provider: 'other',
  };
}

export const ModelMaintenanceSection: React.FC = () => {
  const client = useConsoleStore((state) => state.client);
  const currentSession = useConsoleStore((state) => state.currentSession);
  const fetchRuntimeConfig = useConsoleStore((state) => state.fetchRuntimeConfig);

  const [loading, setLoading] = useState(true);
  const [modelList, setModelList] = useState<ModelListResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Edit / Add Modal state
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingIsNew, setEditingIsNew] = useState(true);
  const [editingOriginalAlias, setEditingOriginalAlias] = useState<string | null>(null);
  const [formData, setFormData] = useState<ModelFormData>(EMPTY_FORM);
  const [modalError, setModalError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Testing connectivity state (keyed by alias)
  const [testingAlias, setTestingAlias] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, TestModelResult>>({});

  // Confirm delete state (alias of model pending delete)
  const [pendingDeleteAlias, setPendingDeleteAlias] = useState<string | null>(null);

  // Collapsed state for provider groups (false = expanded by default)
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

  const toggleGroupCollapse = (groupKey: string) => {
    setCollapsedGroups((prev) => ({
      ...prev,
      [groupKey]: !prev[groupKey],
    }));
  };

  // Find the global default model
  const defaultModel = modelList?.models.find((m) => m.is_default);

  // Other non-default models grouped by provider
  const otherModels = modelList?.models.filter((m) => !m.is_default) ?? [];
  const activeOtherGroups = PROVIDER_METAS.filter((groupMeta) =>
    otherModels.some((m) => resolveGroupKey(m) === groupMeta.key),
  );

  const allCollapsed =
    activeOtherGroups.length > 0 &&
    activeOtherGroups.every((g) => collapsedGroups[g.key] === true);

  const toggleAllCollapse = () => {
    if (allCollapsed) {
      setCollapsedGroups({});
    } else {
      const next: Record<string, boolean> = {};
      for (const g of activeOtherGroups) {
        next[g.key] = true;
      }
      setCollapsedGroups(next);
    }
  };

  const loadModels = useCallback(async () => {
    if (client === null) return;
    setLoading(true);
    setError(null);
    try {
      const result = await client.modelsList(currentSession);
      setModelList(result);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取模型列表失败');
    } finally {
      setLoading(false);
    }
  }, [client, currentSession]);

  useEffect(() => {
    void loadModels();
  }, [loadModels]);

  const handleOpenAdd = () => {
    setEditingIsNew(true);
    setEditingOriginalAlias(null);
    setFormData({
      ...EMPTY_FORM,
      makeDefault: (modelList?.models.length ?? 0) === 0,
    });
    setModalError(null);
    setIsModalOpen(true);
  };

  const handleOpenEdit = (model: ModelSummary) => {
    const { modelId, provider } = extractModelIdAndProvider(model);
    const extra = (model.extra || {}) as Record<string, unknown>;
    const proxy = typeof extra.openai_proxy === 'string' ? extra.openai_proxy : '';
    const ws = extra.websocket === true ? 'yes' : extra.websocket === false ? 'no' : 'auto';

    setEditingIsNew(false);
    setEditingOriginalAlias(model.alias);
    setFormData({
      alias: model.alias,
      modelId,
      provider,
      baseUrl: model.base_url ?? '',
      apiKey: '',
      openaiProxy: proxy,
      websocket: ws,
      contextWindow: model.context_window ? String(model.context_window) : '',
      imageInput: (model.image_input as 'auto' | 'yes' | 'no') || 'auto',
      reasoningEffort: model.reasoning_effort ?? '',
      makeDefault: model.is_default,
      rawExtra: { ...extra },
    });
    setModalError(null);
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditingOriginalAlias(null);
    setFormData(EMPTY_FORM);
    setModalError(null);
  };

  const handleSaveModel = async (e: React.FormEvent) => {
    e.preventDefault();
    if (client === null) return;

    const trimmedAlias = formData.alias.trim();
    const cleanId = formData.modelId
      .trim()
      .replace(/^(openai|anthropic):/i, '');

    if (!trimmedAlias) {
      setModalError('请输入模型别名');
      return;
    }
    if (!cleanId) {
      setModalError('请输入模型标识 ID');
      return;
    }

    // Check for alias conflict when adding new or renaming
    if (
      (editingIsNew || (editingOriginalAlias && trimmedAlias !== editingOriginalAlias)) &&
      modelList?.models.some((m) => m.alias === trimmedAlias)
    ) {
      setModalError(`已存在别名为「${trimmedAlias}」的模型配置，请使用其他别名`);
      return;
    }

    setSaving(true);
    setModalError(null);

    // 根据选择的 provider 自动合成实际调用 model 名称
    let finalModel = cleanId;
    if (formData.provider === 'openai' || formData.provider === 'openai_oauth') {
      finalModel = `openai:${cleanId}`;
    } else if (formData.provider === 'anthropic') {
      finalModel = `anthropic:${cleanId}`;
    }

    // 从原始 rawExtra 开始构建，保护 models.json 中的未编辑配置（如 streaming, headers 等）不被覆盖
    const profile: Record<string, unknown> = {
      ...formData.rawExtra,
      model: finalModel,
      provider: formData.provider === 'other' ? undefined : formData.provider,
    };
    if (formData.provider === 'openai_oauth') {
      profile.auth = 'openai_oauth';
    } else if (profile.auth === 'openai_oauth') {
      delete profile.auth;
    }

    if (formData.baseUrl.trim()) {
      profile.base_url = formData.baseUrl.trim();
    } else {
      delete profile.base_url;
    }

    if (formData.apiKey.trim()) {
      profile.api_key = formData.apiKey.trim();
    }

    if (formData.openaiProxy.trim()) {
      profile.openai_proxy = formData.openaiProxy.trim();
    } else {
      delete profile.openai_proxy;
    }

    if (formData.websocket === 'yes') {
      profile.websocket = true;
    } else if (formData.websocket === 'no') {
      profile.websocket = false;
    } else {
      delete profile.websocket;
    }

    if (formData.contextWindow.trim()) {
      const parsedWindow = parseInt(formData.contextWindow.trim(), 10);
      if (!isNaN(parsedWindow) && parsedWindow > 0) {
        profile.context_window = parsedWindow;
      }
    } else {
      delete profile.context_window;
    }

    if (formData.imageInput) {
      profile.image_input = formData.imageInput;
    }

    if (formData.reasoningEffort.trim()) {
      profile.reasoning_effort = formData.reasoningEffort.trim();
    } else {
      delete profile.reasoning_effort;
    }

    try {
      let updated = await client.modelsSave(
        currentSession,
        trimmedAlias,
        profile,
        formData.makeDefault,
      );
      if (!editingIsNew && editingOriginalAlias && trimmedAlias !== editingOriginalAlias) {
        updated = await client.modelsDelete(currentSession, editingOriginalAlias);
      }
      setModelList(updated);
      setIsModalOpen(false);
      setEditingOriginalAlias(null);
      setFormData(EMPTY_FORM);
      setNotice(
        editingIsNew
          ? `模型 ${trimmedAlias} 已添加`
          : trimmedAlias !== editingOriginalAlias
            ? `模型已重命名为 ${trimmedAlias} 并保存`
            : `模型 ${trimmedAlias} 已保存`,
      );
      void fetchRuntimeConfig();
    } catch (err: unknown) {
      setModalError(err instanceof Error ? err.message : '保存模型失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteModel = async (alias: string) => {
    if (client === null) return;
    setError(null);
    setNotice(null);
    try {
      const updated = await client.modelsDelete(currentSession, alias);
      setModelList(updated);
      setPendingDeleteAlias(null);
      setNotice(`模型 ${alias} 已删除`);
      void fetchRuntimeConfig();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '删除模型失败');
    }
  };

  const handleSetDefault = async (alias: string) => {
    if (client === null) return;
    setError(null);
    setNotice(null);
    try {
      const updated = await client.modelsSetDefault(currentSession, alias);
      setModelList(updated);
      setNotice(`已将 ${alias} 设为默认模型`);
      void fetchRuntimeConfig();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '设置默认模型失败');
    }
  };

  const handleTestConnection = async (alias: string) => {
    if (client === null) return;
    setTestingAlias(alias);
    setError(null);
    try {
      const result = await client.modelsTest(currentSession, alias);
      setTestResults((prev) => ({ ...prev, [alias]: result }));
    } catch (err: unknown) {
      setTestResults((prev) => ({
        ...prev,
        [alias]: {
          ok: false,
          latency_ms: 0,
          error: err instanceof Error ? err.message : '探测异常',
        },
      }));
    } finally {
      setTestingAlias(null);
    }
  };

  const activeProviderMeta =
    PROVIDER_METAS.find((p) => p.key === formData.provider) ?? PROVIDER_METAS[0];

  return (
    <div className="space-y-4 font-sans text-sm">
      {/* Title & Actions Bar */}
      <div className="flex items-center justify-between border-b border-line pb-2">
        <div>
          <span className="ui-settings-title text-gray-900">模型端点维护</span>
          <span className="ml-2 text-xs text-gray-500">
            {modelList ? `共 ${modelList.models.length} 个配置` : '加载中…'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {activeOtherGroups.length > 1 && (
            <button
              type="button"
              onClick={toggleAllCollapse}
              className="ui-button ui-compact text-xs text-gray-500 hover:text-gray-900 border border-line bg-surface"
            >
              {allCollapsed ? '全部展开' : '全部折叠'}
            </button>
          )}
          <button
            type="button"
            onClick={handleOpenAdd}
            className="ui-button ui-compact flex items-center gap-1.5 border border-accent/40 bg-accent/10 px-3 text-xs text-accent transition-colors hover:bg-accent/20"
          >
            <Add16Regular aria-hidden="true" />
            <span>添加模型</span>
          </button>
        </div>
      </div>

      {notice && (
        <div className="rounded-control border border-green-200 bg-green-50 p-2.5 text-xs text-green-800 shadow-card">
          {notice}
        </div>
      )}

      {error && (
        <div className="rounded-control border border-red-200 bg-red-50 p-2.5 text-xs text-red-700 shadow-card">
          {error}
        </div>
      )}

      {loading && !modelList ? (
        <div className="py-12 text-center text-xs text-gray-400">正在加载模型列表…</div>
      ) : (
        <div className="space-y-5">
          {/* 全局默认模型 (置顶最显眼区域) */}
          {defaultModel && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-xs font-semibold text-gray-900">
                  <Star16Filled aria-hidden="true" className="text-accent shrink-0" />
                  <span>全局默认模型</span>
                </div>
                {modelList && modelList.models.length > 1 && (
                  <div className="flex items-center gap-1.5 text-xs text-gray-500">
                    <span>切换默认：</span>
                    <select
                      value={defaultModel.alias}
                      onChange={(e) => void handleSetDefault(e.target.value)}
                      className="ui-field py-0.5 px-2 text-xs font-sans"
                    >
                      {modelList.models.map((m) => (
                        <option key={m.alias} value={m.alias}>
                          {m.alias} {m.alias === defaultModel.alias ? '（当前默认）' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>

              <div className="rounded-card border-2 border-accent/70 bg-accent/[0.03] p-3.5 shadow-card transition-colors">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-base font-semibold text-gray-900">
                        {defaultModel.alias}
                      </span>
                      <span className="rounded-control bg-accent/20 px-2 py-0.5 text-[11px] font-semibold text-accent flex items-center gap-1">
                        <Star16Filled aria-hidden="true" className="w-3 h-3 text-accent shrink-0" />
                        <span>全局默认</span>
                      </span>
                      <span className="rounded-control bg-gray-100 px-2 py-0.5 text-[10px] text-gray-600 font-mono">
                        {defaultModel.model.replace(/^(openai|anthropic):/i, '')}
                      </span>
                      <span className="rounded-control border border-line bg-surface px-2 py-0.5 text-[10px] text-gray-500">
                        {PROVIDER_METAS.find((p) => p.key === resolveGroupKey(defaultModel))?.label ?? '自定义网关'}
                      </span>
                    </div>

                    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500">
                      {defaultModel.base_url ? (
                        <span className="max-w-[280px] truncate" title={defaultModel.base_url}>
                          接口：{defaultModel.base_url}
                        </span>
                      ) : resolveGroupKey(defaultModel) === 'openai_oauth' ? (
                        <span>接口：内置官方 Codex 网关</span>
                      ) : null}
                      {defaultModel.context_window && (
                        <span>上下文：{Math.round(defaultModel.context_window / 1024)}k</span>
                      )}
                      {typeof (defaultModel.extra as Record<string, unknown> | undefined)?.openai_proxy === 'string' && (
                        <span className="max-w-[220px] truncate" title={String((defaultModel.extra as Record<string, unknown>).openai_proxy)}>
                          代理：{String((defaultModel.extra as Record<string, unknown>).openai_proxy)}
                        </span>
                      )}
                      {(defaultModel.extra as Record<string, unknown> | undefined)?.websocket !== undefined && (
                        <span>WS：{(defaultModel.extra as Record<string, unknown>).websocket ? '开启' : '关闭'}</span>
                      )}
                      {(defaultModel.extra as Record<string, unknown> | undefined)?.streaming !== undefined && (
                        <span>流式：{(defaultModel.extra as Record<string, unknown>).streaming ? '开启' : '关闭'}</span>
                      )}
                      <span>识图：{defaultModel.image_input}</span>
                      {defaultModel.reasoning_effort && (
                        <span>推理：{defaultModel.reasoning_effort}</span>
                      )}
                      <span
                        className={`font-medium ${
                          defaultModel.has_api_key || resolveGroupKey(defaultModel) === 'openai_oauth'
                            ? 'text-green-700'
                            : 'text-gray-400'
                        }`}
                      >
                        {resolveGroupKey(defaultModel) === 'openai_oauth'
                          ? 'Codex OAuth 授权'
                          : defaultModel.has_api_key
                            ? '已配密钥'
                            : '未配密钥'}
                      </span>
                    </div>
                  </div>

                  {/* Top Action buttons */}
                  <div className="flex shrink-0 items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => void handleTestConnection(defaultModel.alias)}
                      disabled={testingAlias === defaultModel.alias}
                      title="测试连通性与响应速度"
                      className="ui-button ui-compact flex items-center gap-1 border border-line bg-surface px-2.5 text-xs text-gray-700 hover:bg-gray-50"
                    >
                      <PlugConnected16Regular aria-hidden="true" />
                      <span>{testingAlias === defaultModel.alias ? '测试中…' : '测试'}</span>
                    </button>

                    <button
                      type="button"
                      onClick={() => handleOpenEdit(defaultModel)}
                      title="编辑模型端点配置"
                      className="ui-icon-button ui-compact border border-line bg-surface text-gray-600 hover:text-gray-900"
                    >
                      <Edit16Regular aria-hidden="true" />
                    </button>

                    <button
                      type="button"
                      onClick={() => setPendingDeleteAlias(defaultModel.alias)}
                      disabled={(modelList?.models.length ?? 0) <= 1}
                      title={
                        (modelList?.models.length ?? 0) <= 1
                          ? '至少保留一个模型配置'
                          : '删除此模型端点'
                      }
                      className="ui-icon-button ui-compact border border-line bg-surface text-gray-400 hover:text-red-600 disabled:opacity-30"
                    >
                      <Delete16Regular aria-hidden="true" />
                    </button>
                  </div>
                </div>

                {/* Connectivity test result MessageBar */}
                {testResults[defaultModel.alias] && (
                  <div
                    className={`mt-2.5 flex items-center gap-2 rounded-control px-2.5 py-1 text-[11px] shadow-card ${
                      testResults[defaultModel.alias].ok
                        ? 'bg-green-50 text-green-800 border border-green-200'
                        : 'bg-red-50 text-red-700 border border-red-200'
                    }`}
                  >
                    <span>
                      {testResults[defaultModel.alias].ok
                        ? `连接成功 (${testResults[defaultModel.alias].latency_ms}ms)`
                        : `连接失败: ${testResults[defaultModel.alias].error || '未知网络错误'}`}
                    </span>
                  </div>
                )}

                {/* Delete confirmation strip */}
                {pendingDeleteAlias === defaultModel.alias && (
                  <div className="mt-2.5 flex items-center justify-between rounded-control border border-red-200 bg-red-50/80 p-2 text-xs shadow-card">
                    <span className="text-red-800">确认删除全局默认模型「{defaultModel.alias}」？</span>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => setPendingDeleteAlias(null)}
                        className="ui-button ui-compact border border-line bg-surface text-xs"
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleDeleteModel(defaultModel.alias)}
                        className="ui-button ui-compact ui-danger text-xs"
                      >
                        确认删除
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* 更多端点按供应商分类列表 */}
          {otherModels.length > 0 && (
            <div className="space-y-3 pt-1">
              <div className="space-y-3">
                {PROVIDER_METAS.map((groupMeta) => {
                  const modelsInGroup = otherModels.filter(
                    (m) => resolveGroupKey(m) === groupMeta.key,
                  );
                  if (modelsInGroup.length === 0) return null;

                  const isExpanded = collapsedGroups[groupMeta.key] !== true;

                  return (
                    <div key={groupMeta.key} className="space-y-2">
                      <button
                        type="button"
                        onClick={() => toggleGroupCollapse(groupMeta.key)}
                        className="flex w-full items-center justify-between rounded-control border border-line/50 bg-surface/50 px-2.5 py-1.5 text-left text-xs font-semibold text-gray-700 hover:bg-surface hover:text-gray-900 transition-colors cursor-pointer"
                        aria-expanded={isExpanded}
                      >
                        <div className="flex items-center gap-2">
                          {isExpanded ? (
                            <ChevronDown16Regular aria-hidden="true" className="text-gray-400 shrink-0" />
                          ) : (
                            <ChevronRight16Regular aria-hidden="true" className="text-gray-400 shrink-0" />
                          )}
                          <Bot16Regular aria-hidden="true" className="text-accent shrink-0" />
                          <span>{groupMeta.label}</span>
                        </div>
                        <span className="rounded-control bg-gray-100 px-2 py-0.5 text-[10px] text-gray-500 font-normal">
                          {modelsInGroup.length} 个端点 {isExpanded ? '' : '（已折叠）'}
                        </span>
                      </button>

                      <div className="fluent-accordion" data-expanded={isExpanded}>
                        <div className="fluent-accordion-content space-y-2 pt-0.5">
                          {modelsInGroup.map((item) => {
                            const isTesting = testingAlias === item.alias;
                            const testRes = testResults[item.alias];
                            const isDeleting = pendingDeleteAlias === item.alias;
                            const cleanDisplayModel = item.model.replace(/^(openai|anthropic):/i, '');

                            return (
                              <div
                                key={item.alias}
                                className="rounded-card border border-line bg-surface p-3 transition-colors shadow-card hover:border-accent/40"
                              >
                                <div className="flex items-start justify-between gap-3">
                                  <div className="min-w-0 flex-1">
                                    <div className="flex flex-wrap items-center gap-1.5">
                                      <span className="font-semibold text-gray-900 text-sm">
                                        {item.alias}
                                      </span>
                                      <span className="rounded-control bg-gray-100 px-2 py-0.5 text-[10px] text-gray-600 font-mono">
                                        {cleanDisplayModel}
                                      </span>
                                    </div>

                                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
                                      {item.base_url ? (
                                        <span className="max-w-[220px] truncate" title={item.base_url}>
                                          接口：{item.base_url}
                                        </span>
                                      ) : resolveGroupKey(item) === 'openai_oauth' ? (
                                        <span>接口：内置官方 Codex 网关</span>
                                      ) : null}
                                      {item.context_window && (
                                        <span>上下文：{Math.round(item.context_window / 1024)}k</span>
                                      )}
                                      {typeof (item.extra as Record<string, unknown> | undefined)?.openai_proxy === 'string' && (
                                        <span className="max-w-[220px] truncate" title={String((item.extra as Record<string, unknown>).openai_proxy)}>
                                          代理：{String((item.extra as Record<string, unknown>).openai_proxy)}
                                        </span>
                                      )}
                                      {(item.extra as Record<string, unknown> | undefined)?.websocket !== undefined && (
                                        <span>WS：{(item.extra as Record<string, unknown>).websocket ? '开启' : '关闭'}</span>
                                      )}
                                      {(item.extra as Record<string, unknown> | undefined)?.streaming !== undefined && (
                                        <span>流式：{(item.extra as Record<string, unknown>).streaming ? '开启' : '关闭'}</span>
                                      )}
                                      <span>识图：{item.image_input}</span>
                                      {item.reasoning_effort && (
                                        <span>推理：{item.reasoning_effort}</span>
                                      )}
                                      <span
                                        className={`font-medium ${
                                          item.has_api_key || resolveGroupKey(item) === 'openai_oauth'
                                            ? 'text-green-700'
                                            : 'text-gray-400'
                                        }`}
                                      >
                                        {resolveGroupKey(item) === 'openai_oauth'
                                          ? 'Codex OAuth 授权'
                                          : item.has_api_key
                                            ? '已配密钥'
                                            : '未配密钥'}
                                      </span>
                                    </div>
                                  </div>

                                  {/* Action buttons (Fluent Compact Controls) */}
                                  <div className="flex shrink-0 items-center gap-1.5">
                                    <button
                                      type="button"
                                      onClick={() => void handleSetDefault(item.alias)}
                                      title="设为项目全局默认生效模型"
                                      className="ui-button ui-compact flex items-center gap-1 border border-line bg-surface px-2.5 text-xs text-gray-700 hover:bg-gray-50"
                                    >
                                      <Checkmark16Regular aria-hidden="true" />
                                      <span>设为默认</span>
                                    </button>

                                    <button
                                      type="button"
                                      onClick={() => void handleTestConnection(item.alias)}
                                      disabled={isTesting}
                                      title="测试连通性与响应速度"
                                      className="ui-button ui-compact flex items-center gap-1 border border-line bg-surface px-2.5 text-xs text-gray-700 hover:bg-gray-50"
                                    >
                                      <PlugConnected16Regular aria-hidden="true" />
                                      <span>{isTesting ? '测试中…' : '测试'}</span>
                                    </button>

                                    <button
                                      type="button"
                                      onClick={() => handleOpenEdit(item)}
                                      title="编辑模型端点配置"
                                      className="ui-icon-button ui-compact border border-line bg-surface text-gray-600 hover:text-gray-900"
                                    >
                                      <Edit16Regular aria-hidden="true" />
                                    </button>

                                    <button
                                      type="button"
                                      onClick={() => setPendingDeleteAlias(item.alias)}
                                      disabled={(modelList?.models.length ?? 0) <= 1}
                                      title={
                                        (modelList?.models.length ?? 0) <= 1
                                          ? '至少保留一个模型配置'
                                          : '删除此模型端点'
                                      }
                                      className="ui-icon-button ui-compact border border-line bg-surface text-gray-400 hover:text-red-600 disabled:opacity-30"
                                    >
                                      <Delete16Regular aria-hidden="true" />
                                    </button>
                                  </div>
                                </div>

                                {/* Connectivity test result MessageBar */}
                                {testRes && (
                                  <div
                                    className={`mt-2 flex items-center gap-2 rounded-control px-2.5 py-1 text-[11px] shadow-card ${
                                      testRes.ok
                                        ? 'bg-green-50 text-green-800 border border-green-200'
                                        : 'bg-red-50 text-red-700 border border-red-200'
                                    }`}
                                  >
                                    <span>
                                      {testRes.ok
                                        ? `连接成功 (${testRes.latency_ms}ms)`
                                        : `连接失败: ${testRes.error || '未知网络错误'}`}
                                    </span>
                                  </div>
                                )}

                                {/* Delete confirmation strip */}
                                {isDeleting && (
                                  <div className="mt-2.5 flex items-center justify-between rounded-control border border-red-200 bg-red-50/80 p-2 text-xs shadow-card">
                                    <span className="text-red-800">确认删除模型「{item.alias}」？</span>
                                    <div className="flex items-center gap-2">
                                      <button
                                        type="button"
                                        onClick={() => setPendingDeleteAlias(null)}
                                        className="ui-button ui-compact border border-line bg-surface text-xs"
                                      >
                                        取消
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => void handleDeleteModel(item.alias)}
                                        className="ui-button ui-compact ui-danger text-xs"
                                      >
                                        确认删除
                                      </button>
                                    </div>
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Standalone Edit / Add Model Modal Dialog (Fluent Flyout) */}
      {isModalOpen && (
        <Portal>
          <div
            className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm p-4 scrim-in"
            onClick={handleCloseModal}
          >
            <div
              role="dialog"
              aria-label={editingIsNew ? '添加模型' : '编辑模型'}
              className="no-scrollbar max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-card border border-line/70 material-flyout flyout-in p-5 font-sans shadow-flyout space-y-3.5"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between border-b border-line pb-2.5">
                <span className="ui-settings-title text-gray-900">
                  {editingIsNew ? '添加新模型端点' : `编辑模型端点 (${formData.alias})`}
                </span>
                <button
                  type="button"
                  onClick={handleCloseModal}
                  title="关闭"
                  className="ui-icon-button"
                >
                  <Dismiss20Regular aria-hidden="true" />
                </button>
              </div>

              {modalError && (
                <div className="rounded-control border border-red-200 bg-red-50 p-2.5 text-xs text-red-700 shadow-card">
                  {modalError}
                </div>
              )}

              <form
                onSubmit={(e) => {
                  void handleSaveModel(e);
                }}
                className="space-y-3 pt-1 text-xs"
              >
                <div className="grid grid-cols-2 gap-3.5">
                  <div>
                    <label className="mb-1 block font-medium text-gray-700">别名 Alias *</label>
                    <input
                      type="text"
                      value={formData.alias}
                      onChange={(e) => setFormData({ ...formData, alias: e.target.value })}
                      placeholder="例如：deepseek-v4-flash"
                      className="ui-field w-full"
                      required
                    />
                    <p className="mt-1 text-[10px] text-gray-500">
                      用于会话选择与命令调用引用
                    </p>
                  </div>

                  <div>
                    <label className="mb-1 block font-medium text-gray-700">
                      供应商 Provider *
                    </label>
                    <select
                      value={formData.provider}
                      onChange={(e) => {
                        const nextProvider = e.target.value;
                        const meta = PROVIDER_METAS.find((p) => p.key === nextProvider);
                        setFormData({
                          ...formData,
                          provider: nextProvider,
                          baseUrl: formData.baseUrl ? formData.baseUrl : (meta?.defaultBaseUrl ?? ''),
                        });
                      }}
                      className="ui-field w-full"
                    >
                      {PROVIDER_METAS.map((p) => (
                        <option key={p.key} value={p.key}>
                          {p.label}
                        </option>
                      ))}
                    </select>
                    <p className="mt-1 text-[10px] text-gray-500">
                      自动关联底层协议与前缀
                    </p>
                  </div>

                  <div className="col-span-2">
                    <label className="mb-1 block font-medium text-gray-700">
                      具体模型 ID (Model) *
                    </label>
                    <div className="flex items-center">
                      {activeProviderMeta.prefix && (
                        <span className="rounded-l border border-r-0 border-line bg-gray-100 px-2.5 py-1.5 text-xs font-mono text-gray-600">
                          {activeProviderMeta.prefix}
                        </span>
                      )}
                      <input
                        type="text"
                        value={formData.modelId}
                        onChange={(e) => setFormData({ ...formData, modelId: e.target.value })}
                        placeholder="例如：deepseek-v4-flash, gpt-4o, claude-3-7-sonnet"
                        className={`ui-field flex-1 ${
                          activeProviderMeta.prefix ? 'rounded-l-none' : ''
                        }`}
                        required
                      />
                    </div>
                    <p className="mt-1 text-[10px] text-gray-500">
                      只填具体的模型名称，前缀由供应商自动补齐
                    </p>
                  </div>

                  <div className="col-span-2">
                    <label className="mb-1 block font-medium text-gray-700">接口地址 Base URL</label>
                    <input
                      type="text"
                      value={formData.baseUrl}
                      onChange={(e) => setFormData({ ...formData, baseUrl: e.target.value })}
                      placeholder={
                        formData.provider === 'openai_oauth'
                          ? '内置官方 Codex 地址（https://chatgpt.com/backend-api/codex），留空即可'
                          : activeProviderMeta.defaultBaseUrl || '服务地址 (如 http://127.0.0.1:8317/v1)'
                      }
                      className="ui-field w-full"
                    />
                    {formData.provider === 'openai_oauth' && (
                      <p className="mt-1 text-[10px] text-gray-500">
                        Codex 为内置官方服务，通常无需配置 URL，留空使用内置默认地址
                      </p>
                    )}
                  </div>

                  <div className="col-span-2">
                    <label className="mb-1 block font-medium text-gray-700">API Key</label>
                    <input
                      type="password"
                      value={formData.apiKey}
                      onChange={(e) => setFormData({ ...formData, apiKey: e.target.value })}
                      placeholder={
                        formData.provider === 'openai_oauth'
                          ? '使用 Codex OAuth 授权登录（无需填写 API Key）'
                          : editingIsNew
                            ? '输入 API Key'
                            : '••••••••（留空保持不变）'
                      }
                      className="ui-field w-full"
                      autoComplete="off"
                    />
                    {formData.provider === 'openai_oauth' && (
                      <p className="mt-1 text-[10px] text-gray-500">
                        无需 API Key，通过 CLI 登录（synapse auth openai login）或已导入的 Codex 凭据自动鉴权
                      </p>
                    )}
                  </div>

                  <div>
                    <label className="mb-1 block font-medium text-gray-700">
                      上下文窗口 Context (tokens)
                    </label>
                    <input
                      type="number"
                      value={formData.contextWindow}
                      onChange={(e) =>
                        setFormData({ ...formData, contextWindow: e.target.value })
                      }
                      placeholder="例如：128000"
                      className="ui-field w-full"
                    />
                  </div>

                  <div>
                    <label className="mb-1 block font-medium text-gray-700">图像支持 Image Input</label>
                    <select
                      value={formData.imageInput}
                      onChange={(e) =>
                        setFormData({
                          ...formData,
                          imageInput: e.target.value as 'auto' | 'yes' | 'no',
                        })
                      }
                      className="ui-field w-full"
                    >
                      <option value="auto">auto (自动识别)</option>
                      <option value="yes">yes (多模态识图)</option>
                      <option value="no">no (纯文本)</option>
                    </select>
                  </div>

                  <div className="col-span-2">
                    <label className="mb-1 block font-medium text-gray-700">推理级别 Reasoning</label>
                    <select
                      value={formData.reasoningEffort}
                      onChange={(e) =>
                        setFormData({ ...formData, reasoningEffort: e.target.value })
                      }
                      className="ui-field w-full"
                    >
                      <option value="">默认 (继承配置)</option>
                      <option value="low">low</option>
                      <option value="medium">medium</option>
                      <option value="high">high</option>
                    </select>
                  </div>

                  <div className="col-span-2 grid grid-cols-2 gap-3.5 border-t border-line/40 pt-2.5">
                    <div>
                      <label className="mb-1 block font-medium text-gray-700">代理地址 Proxy (openai_proxy)</label>
                      <input
                        type="text"
                        value={formData.openaiProxy}
                        onChange={(e) => setFormData({ ...formData, openaiProxy: e.target.value })}
                        placeholder="例如：socks5h://localhost:7991 或 http://127.0.0.1:7890"
                        className="ui-field w-full"
                      />
                      <p className="mt-1 text-[10px] text-gray-500">
                        该端点专用的本地/远程代理（留空直连）
                      </p>
                    </div>

                    <div>
                      <label className="mb-1 block font-medium text-gray-700">WebSocket 传输 (websocket)</label>
                      <select
                        value={formData.websocket}
                        onChange={(e) =>
                          setFormData({
                            ...formData,
                            websocket: e.target.value as 'auto' | 'yes' | 'no',
                          })
                        }
                        className="ui-field w-full"
                      >
                        <option value="auto">默认 (遵循供应商)</option>
                        <option value="yes">启用 (WebSocket 双向流)</option>
                        <option value="no">禁用 (普通 HTTP SSE)</option>
                      </select>
                      <p className="mt-1 text-[10px] text-gray-500">
                        Codex 等支持 WebSocket 长连接通信协议
                      </p>
                    </div>

                    {Object.keys(formData.rawExtra).filter((k) => !['openai_proxy', 'websocket', 'model', 'provider', 'auth', 'base_url', 'api_key', 'context_window', 'image_input', 'reasoning_effort'].includes(k)).length > 0 && (
                      <div className="col-span-2 rounded-control bg-gray-100/70 p-2 text-[11px] text-gray-600 border border-line/50">
                        <span className="font-semibold">已保留 models.json 扩展配置：</span>
                        <span className="font-mono ml-1">{Object.keys(formData.rawExtra).filter((k) => !['openai_proxy', 'websocket', 'model', 'provider', 'auth', 'base_url', 'api_key', 'context_window', 'image_input', 'reasoning_effort'].includes(k)).join(', ')}</span>
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex items-center justify-between pt-3 border-t border-line/60">
                  <label className="flex items-center gap-2 text-xs text-gray-700 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={formData.makeDefault}
                      onChange={(e) =>
                        setFormData({ ...formData, makeDefault: e.target.checked })
                      }
                      className="rounded border-gray-300 text-accent focus:ring-accent"
                    />
                    <span>设为项目默认模型</span>
                  </label>

                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={handleCloseModal}
                      disabled={saving}
                      className="ui-button ui-compact border border-line bg-surface px-3 text-xs"
                    >
                      取消
                    </button>
                    <button
                      type="submit"
                      disabled={saving}
                      className="ui-button ui-compact ui-primary px-3 text-xs"
                    >
                      {saving ? '保存中…' : '保存'}
                    </button>
                  </div>
                </div>
              </form>
            </div>
          </div>
        </Portal>
      )}
    </div>
  );
};
