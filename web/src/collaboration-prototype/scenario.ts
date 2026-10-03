export type Row = { type: 'user' | 'agent' | 'step' | 'contact' | 'receipt'; text: string; target?: string };
export type Session = { id: string; project: string; title: string; status: string; rows: Row[] };
export type Scenario = {
  sessions: Session[]; selected: string; target: string | null; preview: string | null;
  phase: 'idle' | 'running' | 'waiting' | 'received' | 'done' | 'stopped';
  tick: number; queued: boolean; contacted: boolean; delivered: boolean; autoContinue: boolean;
};
const row = (type: Row['type'], text: string, target?: string): Row => ({ type, text, target });
export const initial = (): Scenario => ({
  sessions: [
    { id: 'a', project: '前端 · portal-web', title: '登录页改造', status: '空闲', rows: [
      row('user', '登录页需要对齐新设计，接入登录接口时请先确认错误码。'),
      row('agent', '我先整理页面结构和表单状态；接口契约确认后再接入请求。'),
    ] },
    { id: 'b', project: '后端 · auth-service', title: '鉴权升级', status: '空闲', rows: [
      row('user', '升级登录鉴权，保持旧客户端的错误码兼容。'),
      row('agent', '正在核对认证入口与错误码映射。'),
    ] },
    { id: 'busy', project: '后端 · auth-service', title: '兼容性排查', status: '忙碌', rows: [row('step', '正在核查旧客户端兼容性；新请求将排队。')] },
    { id: 'design', project: '前端 · portal-web', title: '组件规范', status: '空闲', rows: [row('agent', '记录输入框、按钮与验证提示规范。')] },
  ], selected: 'a', target: null, preview: null, phase: 'idle', tick: 0,
  queued: false, contacted: false, delivered: false, autoContinue: true,
});
function append(s: Scenario, id: string, entry: Row): Scenario {
  return { ...s, sessions: s.sessions.map((session) => session.id === id ? { ...session, rows: [...session.rows, entry] } : session) };
}
function status(s: Scenario, id: string, value: string): Scenario {
  return { ...s, sessions: s.sessions.map((session) => session.id === id ? { ...session, status: value } : session) };
}
export function addSession(s: Scenario, project: string): Scenario {
  const id = `new-${s.sessions.length}`;
  return { ...s, sessions: [...s.sessions, { id, project, title: '新协作会话', status: '空闲', rows: [row('agent', '新建的本地模拟会话。')] }] };
}
export function send(s: Scenario, text: string, target: string | null): Scenario {
  if (!text.trim()) return s;
  let next = append(s, s.selected, row('user', text.trim(), target ?? undefined));
  if (s.selected === 'a' && s.delivered && !s.autoContinue && /继续/.test(text)) {
    return continueAfterReceipt(next);
  }
  if (s.selected !== 'a' || s.phase !== 'idle') {
    return append(next, s.selected, row('agent', '已记录这条补充。原型按预设的登录接口场景运行，不执行真实任务。'));
  }
  if (!target || /(?:参考|读取|只读|看看历史)/.test(text) && !/(?:联系|请让|询问)/.test(text)) {
    return append(next, s.selected, row('agent', target
      ? '已读取该会话的模拟结论：保留旧客户端兼容性，登录失败需区分凭据错误和频率限制。只读取已有内容，没有启动对方。'
      : '已收到。这是本地模拟回复。引用一个会话并请求确认接口，可体验自动协作。'));
  }
  next = { ...next, target, phase: 'running', tick: 0, autoContinue: true };
  return status(append(next, 'a', row('agent', '我会请对方确认接口约定，同时完成这里的页面和表单状态。结论回来后继续接入，你不需要来回转发。')), 'a', '进行中');
}
export function setAuto(s: Scenario, enabled: boolean): Scenario {
  if (s.phase === 'stopped' || s.phase === 'done') return s;
  return { ...s, autoContinue: enabled };
}
export function stop(s: Scenario): Scenario {
  if (!['running', 'waiting', 'received'].includes(s.phase)) return s;
  let next: Scenario = { ...s, phase: 'stopped', autoContinue: false };
  if (!s.contacted) return status(append(next, 'a', row('step', '本次已停止；尚未发送的联系请求已撤销。')), 'a', '已停止');
  next = append(next, 'a', row('step', '本次已停止，目标回复仅接收，不会自动续跑。'));
  return status(next, 'a', '已停止');
}
export function advance(s: Scenario): Scenario {
  if (!needsTick(s)) return s;
  const tick = s.tick + 1;
  let next = { ...s, tick };
  const target = s.target;
  if (!target) return next;
  const title = s.sessions.find((session) => session.id === target)?.title ?? '目标会话';
  if (tick === 1 && s.phase !== 'stopped') {
    next = append(next, 'a', row('step', '已工作 · 整理登录页字段、校验和错误提示。'));
  }
  if (tick === 2 && s.phase !== 'stopped') {
    next = append(next, 'a', row('contact', `联系会话 → ${title} · 请求已发送`, target));
    next = append(next, target, row('contact', '来自 登录页改造 · 请确认 POST /auth/login 请求字段、成功响应及 401/429 错误码。范围：登录页接入；不共享完整历史。', 'a'));
    next = { ...next, contacted: true, queued: target === 'busy' };
    next = status(next, target, target === 'busy' ? '原任务运行中' : '处理中');
    if (target === 'busy') next = append(next, target, row('step', '请求排队 · 等当前兼容性排查结束后处理，不抢占。'));
  }
  if (tick === 3 && s.phase !== 'stopped') next = append(next, 'a', row('step', '页面完成 · 输入校验、提交态与错误提示已准备。'));
  if (tick === 4 && s.phase !== 'stopped') {
    next = { ...next, phase: 'waiting' };
    next = status(next, 'a', '等待回复');
    next = append(next, 'a', row('agent', '页面和表单状态已经准备好，正在等待接口约定。收到回复后，我会继续接入和验证。'));
  }
  if (tick === 5 && s.queued) {
    next = append(next, target, row('step', '原有兼容性排查结束；开始处理排队请求。'));
    next = status(next, target, '处理中');
    next = { ...next, queued: false };
  }
  if (tick === 6 && s.contacted) next = append(next, target, row('step', '已工作 · 核对鉴权入口、响应结构与错误码兼容性。'));
  if (tick === 8 && s.contacted) {
    next = append(next, target, row('agent', '接口确认：POST /auth/login，提交 email、password；成功返回 access_token 与 expires_in；401 为凭据错误，429 为请求过频。前端不要展示服务端原始错误。'));
    next = status(next, target, '空闲');
    next = append(next, 'a', row('receipt', `来自 ${title} 的回复：POST /auth/login 使用 email、password；成功返回 access_token / expires_in；401 凭据错误，429 请求过频。`, target));
    next = { ...next, delivered: true, phase: s.phase === 'stopped' ? 'stopped' : 'received' };
    next = status(next, 'a', s.phase === 'stopped' ? '已停止' : s.autoContinue ? '接续中' : '已收到回复');
  }
  if (tick === 9 && s.phase === 'received' && s.autoContinue) {
    next = append(next, 'a', row('agent', '接口已确认。我会按这份约定接入请求，并分别处理凭据错误和频率限制。'));
    next = append(next, 'a', row('step', '继续接入 · 映射 401/429 提示，校验成功响应字段。'));
  }
  if (tick === 11 && s.phase === 'received' && s.autoContinue) {
    next = append(next, 'a', row('agent', '登录页已完成模拟接入与验证：提交态、401/429 提示和成功跳转检查完成。此次演示不修改真实文件。'));
    next = status({ ...next, phase: 'done' }, 'a', '空闲');
  }
  return next;
}
/** Keep target delivery alive after source stop, but never restart its work. */
export function needsTick(s: Scenario): boolean {
  if (s.phase === 'running' || s.phase === 'waiting') return true;
  if (s.phase === 'received') return s.autoContinue && s.tick < 11;
  return s.phase === 'stopped' && s.contacted && !s.delivered;
}
export function continueAfterReceipt(s: Scenario): Scenario {
  if (!s.delivered || !['received', 'stopped'].includes(s.phase) || s.autoContinue) return s;
  let next = append(s, 'a', row('step', '按新指令继续接入 · 映射错误码并验证表单状态。'));
  next = append(next, 'a', row('agent', '模拟接入与验证完成；未修改真实文件。'));
  return status({ ...next, phase: 'done' }, 'a', '空闲');
}
