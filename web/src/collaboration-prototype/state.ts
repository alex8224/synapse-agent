export type Session = { id: string; project: string; title: string; busy: boolean; messages: string[] };
export type Entry = { by: 'source' | 'target'; kind: 'request' | 'clarification' | 'supplement' | 'delivery' | 'revision'; text: string };
export type Item = { id: string; source: string; target: string; title: string; expected: string; shared: string[]; phase: 'queued' | 'working' | 'clarifying' | 'delivered' | 'adopted'; deferred: boolean; entries: Entry[] };
export type Model = { sessions: Session[]; items: Item[]; selected: string; active: string | null; next: number };
export function targetOccupied(model: Model, target: string, except?: string): boolean {
  return !!model.sessions.find((s) => s.id === target)?.busy || model.items.some(
    (i) => i.id !== except && i.target === target && (i.phase === 'working' || i.phase === 'clarifying'),
  );
}
export function pendingForSession(model: Model, session: string): number {
  return model.items.filter((i) =>
    (i.source === session && (i.phase === 'clarifying' || i.phase === 'delivered')) ||
    (i.target === session && i.phase === 'queued'),
  ).length;
}
export const initial = (): Model => ({
  sessions: [
    { id: 's1', project: 'Atlas', title: '产品规划讨论', busy: false, messages: ['我们需要梳理下一季度的发布节奏。', '请把风险点整理成可执行的检查清单。'] },
    { id: 's2', project: 'Atlas', title: '设计评审', busy: true, messages: ['正在整理组件规范与交互意见。'] },
    { id: 's3', project: 'Northstar', title: '研究笔记', busy: false, messages: ['调研不同用户群的反馈。'] },
  ],
  items: [{ id: 'c1', source: 's1', target: 's3', title: '验证发布风险', expected: '给出三项优先检查的风险及依据', shared: ['请把风险点整理成可执行的检查清单。'], phase: 'clarifying', deferred: false, entries: [
    { by: 'source', kind: 'request', text: '请验证发布风险；期望交付：给出三项优先检查的风险及依据' },
    { by: 'target', kind: 'clarification', text: '需要覆盖内测用户还是全部用户？' },
  ] }],
  selected: 's1', active: 'c1', next: 2,
});
export function createItem(model: Model, input: { target: string; title: string; expected: string; shared: string[]; newTitle?: string }): Model {
  const id = `c${model.next}`;
  const target = input.target === 'new' ? `s${Math.max(0, ...model.sessions.map((s) => Number(s.id.slice(1)) || 0)) + 1}` : input.target;
  const sessions = input.target === 'new' ? [...model.sessions, { id: target, project: input.newTitle?.split('::')[0] || 'Atlas', title: input.newTitle?.split('::')[1] || '新协作会话', busy: false, messages: [] }] : model.sessions;
  const busy = targetOccupied(model, target);
  const item: Item = { id, source: model.selected, target, title: input.title.trim(), expected: input.expected.trim(), shared: input.shared, phase: busy ? 'queued' : 'working', deferred: false, entries: [{ by: 'source', kind: 'request', text: `${input.title.trim()}；期望交付：${input.expected.trim()}` }] };
  return { ...model, sessions, items: [...model.items, item], active: id, next: model.next + 1 };
}
export function updateItem(model: Model, id: string, action: 'start' | 'clarify' | 'supplement' | 'deliver' | 'revise' | 'adopt' | 'defer', text = ''): Model {
  return { ...model, items: model.items.map((item) => {
    if (item.id !== id) return item;
    const allowed = action === 'start' ? item.phase === 'queued' && !targetOccupied(model, item.target, id)
      : action === 'clarify' ? item.phase === 'working'
      : action === 'supplement' ? item.phase === 'clarifying'
      : action === 'deliver' ? item.phase === 'working' || item.phase === 'clarifying'
      : action === 'revise' ? item.phase === 'delivered' || item.phase === 'adopted'
      : action === 'adopt' || action === 'defer' ? item.phase === 'delivered' && (action === 'defer' || !model.sessions.find((s) => s.id === item.source)?.busy) : false;
    if (!allowed || (['clarify', 'supplement', 'deliver', 'revise'].includes(action) && !text.trim())) return item;
    const phase: Item['phase'] = action === 'start' || action === 'supplement' ? 'working' : action === 'revise' ? (targetOccupied(model, item.target, id) ? 'queued' : 'working') : action === 'clarify' ? 'clarifying' : action === 'deliver' ? 'delivered' : action === 'adopt' ? 'adopted' : item.phase;
    const kind = { clarify: 'clarification', supplement: 'supplement', deliver: 'delivery', revise: 'revision' } as const;
    const entries = action in kind ? [...item.entries, { by: action === 'clarify' || action === 'deliver' ? 'target' : 'source', kind: kind[action as keyof typeof kind], text: text.trim() } satisfies Entry] : item.entries;
    return { ...item, phase, deferred: action === 'defer' ? true : action === 'adopt' || action === 'revise' ? false : item.deferred, entries };
  }) };
}
export function adopt(model: Model, id: string, instruction: string): Model {
  const item = model.items.find((i) => i.id === id);
  if (!item || item.phase !== 'delivered' || model.sessions.find((s) => s.id === item.source)?.busy) return model;
  const updated = updateItem(model, id, 'adopt');
  const result = item.entries.filter((e) => e.kind === 'delivery').at(-1)?.text || '';
  return { ...updated, sessions: updated.sessions.map((s) => s.id === item.source ? { ...s, messages: [...s.messages, `据协作结果继续：${result}${instruction.trim() ? `\n附加指令：${instruction.trim()}` : ''}`] } : s) };
}
export function setBusy(model: Model, id: string, busy: boolean): Model {
  return { ...model, sessions: model.sessions.map((s) => s.id === id ? { ...s, busy } : s) };
}
