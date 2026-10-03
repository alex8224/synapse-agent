import { useEffect, useRef, useState } from 'react';
import { Add20Regular, ArrowUp20Regular, ArrowLeft20Regular, Chat20Regular, ChevronDown16Regular, ChevronRight16Regular, Dismiss20Regular, Folder20Regular, PanelRight20Regular, Search20Regular, Stop20Regular, ArrowClockwise20Regular, Branch20Regular, Wrench20Regular, Link20Regular, Checkmark20Regular } from '@fluentui/react-icons';
import { addSession, advance, initial, needsTick, send, setAuto, stop, type Row, type Scenario } from './scenario.ts';
import './experience.css';

const DEFAULT_DRAFT = '请让对方确认登录接口和错误码，你先完成页面，确认后继续接入。';
const isBusy = (status: string) => ['忙碌', '进行中', '处理中', '原任务运行中', '接续中'].includes(status);

export function Experience() {
  const [model, setModel] = useState<Scenario>(initial);
  const [draft, setDraft] = useState(DEFAULT_DRAFT);
  const [mention, setMention] = useState<string | null>('b');
  const [picker, setPicker] = useState(false);
  const [query, setQuery] = useState('');
  const [choice, setChoice] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [info, setInfo] = useState(false);
  const [nav, setNav] = useState(false);
  const [searchNav, setSearchNav] = useState('');
  const [generation, setGeneration] = useState(0);
  const input = useRef<HTMLTextAreaElement>(null);
  const feed = useRef<HTMLDivElement>(null);
  const previewFeed = useRef<HTMLDivElement>(null);
  const current = model.sessions.find((session) => session.id === model.selected)!;
  const preview = model.sessions.find((session) => session.id === model.preview);
  const reference = model.sessions.find((session) => session.id === mention);
  const partner = model.sessions.find((session) => session.id === model.target);
  const active = ['running', 'waiting', 'received'].includes(model.phase) && !(model.phase === 'received' && !model.autoContinue);
  const options = model.sessions.filter((session) => session.id !== model.selected && `${session.project} ${session.title}`.toLowerCase().includes(query.toLowerCase()));
  const projects = [...new Set(model.sessions.map((session) => session.project))];
  const ticking = needsTick(model);

  useEffect(() => {
    if (!ticking) return;
    const timer = window.setTimeout(() => setModel((value) => advance(value)), 1600);
    return () => window.clearTimeout(timer);
  }, [ticking, model.tick, generation]);
  useEffect(() => { feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: 'smooth' }); }, [current.rows.length, current.id]);
  useEffect(() => { previewFeed.current?.scrollTo({ top: previewFeed.current.scrollHeight, behavior: 'smooth' }); }, [preview?.rows.length, preview?.id]);
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setPicker(false); setInfo(false); setNav(false); setModel((value) => ({ ...value, preview: null })); }
    };
    window.addEventListener('keydown', handle);
    return () => window.removeEventListener('keydown', handle);
  }, []);

  function reset() {
    setModel(initial()); setDraft(DEFAULT_DRAFT); setMention('b'); setPicker(false);
    setQuery(''); setChoice(0); setExpanded(false); setInfo(false); setNav(false);
    setSearchNav(''); setGeneration((value) => value + 1);
  }
  function selectSession(id: string) {
    setModel((value) => ({ ...value, selected: id, preview: null }));
    setDraft(''); setMention(null); setPicker(false); setNav(false); setInfo(false);
  }
  function choose(id: string) {
    setMention(id); setPicker(false); setQuery(''); setChoice(0);
    setDraft((value) => value.replace(/@[^\s@]*$/, ''));
    input.current?.focus();
  }
  function newSession(project: string) {
    const next = addSession(model, project);
    setModel(next); choose(next.sessions.at(-1)!.id);
  }
  function submit() {
    if (!draft.trim()) return;
    setModel((value) => send(value, draft, mention));
    setDraft(''); setMention(null); setPicker(false);
  }
  function openPreview(id: string) { setModel((value) => ({ ...value, preview: id })); }
  function sessionLink(id: string, full = true) {
    const session = model.sessions.find((value) => value.id === id);
    return <button className="xp-session-link" onClick={() => openPreview(id)}><Chat20Regular />{full ? `${session?.project.split(' · ')[0]} / ` : ''}{session?.title}</button>;
  }
  function renderRow(entry: Row, index: number, compact = false) {
    if (entry.type === 'step') return <div className="xp-step" key={index}><Wrench20Regular /><span>{entry.text}</span></div>;
    if (entry.type === 'contact' && entry.target !== 'a') return <div className="xp-contact" key={index}>
      <div className="xp-contact-line"><Link20Regular />{sessionLink(entry.target!)}<span className="xp-muted">{model.delivered ? '已回复' : model.queued ? '等待对方空闲' : '等待回复'}</span><button className="ui-icon-button ui-compact" aria-label="展开联系记录" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? <ChevronDown16Regular /> : <ChevronRight16Regular />}</button></div>
      {expanded && <div className="xp-contact-detail"><strong>已发送的请求</strong><p>请确认 POST /auth/login 的请求字段、成功响应和 401 / 429 错误处理。</p><p className="xp-muted">背景：当前会话正在改造登录页。只共享这次接入所需背景，不发送完整历史。此处仅核实接口，不修改对方项目。</p><label><input type="checkbox" checked={model.autoContinue} disabled={model.phase === 'stopped' || model.phase === 'done'} onChange={(event) => setModel((value) => setAuto(value, event.target.checked))} />结果返回后继续本次任务</label><small>{model.phase === 'stopped' ? '本次已停止，回复仅接收。' : '关闭后只接收回复，可在输入框要求继续。'}</small></div>}
    </div>;
    if (entry.type === 'receipt' || entry.type === 'contact') return <section className="xp-receipt" key={index}>
      <div className="xp-receipt-label"><Link20Regular />{entry.type === 'receipt' ? '收到回复' : '收到协作请求'}{entry.target && sessionLink(entry.target)}</div><p>{entry.text.replace(/^来自 .*? 的回复：/, '')}</p>
      {entry.type === 'receipt' && (!model.autoContinue || model.phase === 'stopped') && <small>仅接收 · 没有自动启动当前会话</small>}
    </section>;
    return <div className={`xp-message xp-${entry.type}${compact ? ' xp-compact-message' : ''}`} key={index}>
      <div className="xp-author">{entry.type === 'user' ? '你' : 'Synapse'}</div>
      {entry.type === 'user' && entry.target && <div className="xp-message-reference">{sessionLink(entry.target)}</div>}
      <p>{entry.text}</p>
    </div>;
  }

  return <div className="console-shell material-canvas xp-shell" data-prototype="v2">
    {nav && <button className="xp-nav-scrim" aria-label="关闭导航" onClick={() => setNav(false)} />}
    <aside className={`xp-nav ${nav ? 'xp-nav-open' : ''}`} aria-label="项目与会话">
      <div className="xp-brand"><span className="xp-brand-mark">S</span><strong>Synapse</strong><span>Web</span></div>
      <button className="ui-button xp-new" onClick={reset}><Add20Regular />新建任务<span className="xp-muted">演示重置</span></button>
      <div className="xp-nav-search"><Search20Regular /><input aria-label="搜索会话" placeholder="搜索会话" value={searchNav} onChange={(event) => setSearchNav(event.target.value)} /></div>
      <div className="xp-tree">{projects.map((project) => <section key={project}><div className="xp-project"><ChevronDown16Regular /><Folder20Regular />{project.split(' · ')[1]}</div>{model.sessions.filter((session) => session.project === project && session.title.includes(searchNav)).map((session) => <button className="ui-nav-row xp-session" data-selected={session.id === model.selected} key={session.id} onClick={() => selectSession(session.id)}><span className={isBusy(session.status) ? 'xp-spinner' : 'xp-session-dot'} /><span>{session.title}</span>{session.status === '等待回复' && <small>等待</small>}</button>)}</section>)}</div>
      <div className="xp-nav-footer"><span className="xp-local-dot" />本地工作区<span className="xp-muted">模拟</span></div>
    </aside>
    <div className="xp-workspace">
      <header className="material-chrome xp-topbar">
        <div className="xp-top-left"><button className="ui-icon-button xp-nav-toggle" aria-label="打开项目导航" onClick={() => setNav((value) => !value)}><Folder20Regular /></button><span className="xp-project-chip">{current.project.split(' · ')[1]}</span><span className="xp-branch"><Branch20Regular />main</span></div>
        <button className="ui-button xp-title" onClick={() => setInfo((value) => !value)} aria-expanded={info}><Chat20Regular />{current.title}</button>
        <div className="xp-top-right"><span className="xp-prototype-tag">原型 v2 · 模拟运行</span><button className="ui-icon-button" aria-label="重置演示" title="重置演示" onClick={reset}><ArrowClockwise20Regular /></button><button className="ui-icon-button" aria-label="打开或关闭会话预览" aria-expanded={!!preview} onClick={() => setModel((value) => ({ ...value, preview: value.preview ? null : value.target ?? 'b' }))}><PanelRight20Regular /></button></div>
        {info && <div className="material-flyout flyout-in xp-info"><strong>{current.title}</strong><p>{current.project}</p><p>状态：{current.status}</p><small>独立交互原型，不连接真实 Agent 或会话。</small></div>}
      </header>
      <div className="xp-body">
        <main className="material-pane xp-main">
          {model.selected !== 'a' && <div className="xp-return"><button className="ui-button" onClick={() => selectSession('a')}><ArrowLeft20Regular />返回登录页改造</button><span className="xp-muted">切换查看不会中断协作</span></div>}
          <div className="xp-feed console-gutter no-scrollbar" ref={feed} data-testid="transcript"><div className="console-column"><div className="xp-date">今天</div>{current.rows.map((entry, index) => renderRow(entry, index))}
            {current.status === '等待回复' && <div className="xp-wait"><span className="xp-wait-dot" />等待 {partner?.title}，回复后{model.autoContinue ? '自动继续' : '仅接收'}。</div>}
          </div></div>
          <div className="xp-composer-wrap console-gutter"><div className="xp-composer material-card">
            {picker && <div className="xp-picker material-flyout flyout-in" role="dialog" aria-label="引用会话"><div className="xp-picker-search"><Search20Regular /><input autoFocus placeholder="搜索项目或会话" aria-label="查找引用会话" value={query} onChange={(event) => { setQuery(event.target.value); setChoice(0); }} onKeyDown={(event) => { if (event.key === 'ArrowDown') { event.preventDefault(); setChoice((value) => Math.min(value + 1, options.length - 1)); } if (event.key === 'ArrowUp') { event.preventDefault(); setChoice((value) => Math.max(value - 1, 0)); } if (event.key === 'Enter' && options[choice]) { event.preventDefault(); choose(options[choice].id); } }} /><button className="ui-icon-button" aria-label="关闭引用选择器" onClick={() => setPicker(false)}><Dismiss20Regular /></button></div><div className="xp-picker-options">{projects.map((project) => <div key={project}><div className="xp-picker-group">{project}</div>{options.filter((session) => session.project === project).map((session) => <button className={`xp-picker-option ${options[choice]?.id === session.id ? 'xp-highlight' : ''}`} key={session.id} onClick={() => choose(session.id)}><Chat20Regular /><span>{session.title}</span><small>{session.status}</small></button>)}<button className="xp-picker-option xp-muted" onClick={() => newSession(project)}><Add20Regular /><span>在此项目新建会话</span></button></div>)}</div><div className="xp-picker-hint">引用只确定对象；是否联系由你的指令决定。</div></div>}
            {reference && <div className="xp-reference"><button onClick={() => openPreview(reference.id)}><Chat20Regular />{reference.project.split(' · ')[0]} / {reference.title}</button><button aria-label="移除会话引用" onClick={() => setMention(null)}><Dismiss20Regular /></button></div>}
            <textarea ref={input} aria-label="消息输入" placeholder="发送消息，@ 引用另一个会话" value={draft} rows={2} onChange={(event) => { setDraft(event.target.value); if (event.target.value.endsWith('@')) { setPicker(true); setQuery(''); setChoice(0); } }} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); submit(); } }} />
            <div className="xp-composer-controls"><button className="ui-icon-button" aria-label="引用会话" title="引用会话" onClick={() => { setPicker((value) => !value); setQuery(''); setChoice(0); }}><Add20Regular /></button><span className="xp-composer-hint">@ 引用会话</span><div className="xp-controls-right"><span className="xp-model">演示 Agent</span>{active && model.selected === 'a' && <button className="ui-icon-button" aria-label="停止本次任务" onClick={() => setModel((value) => stop(value))}><Stop20Regular /></button>}<button className="ui-icon-button ui-primary" aria-label="发送消息" disabled={!draft.trim()} onClick={submit}><ArrowUp20Regular /></button></div></div>
          </div><div className="xp-demo-note">登录接口协作演示 · 点击发送后自动运行，不修改真实文件</div></div>
        </main>
        {preview && <aside className="material-pane xp-preview" aria-label="会话预览"><div className="xp-preview-head"><div><small>会话预览 · 只读</small><strong>{preview.title}</strong><span>{preview.project} · {preview.status}</span></div><button className="ui-icon-button" aria-label="关闭会话预览" onClick={() => setModel((value) => ({ ...value, preview: null }))}><Dismiss20Regular /></button></div><button className="ui-button xp-enter" onClick={() => selectSession(preview.id)}><Chat20Regular />进入完整会话<ChevronRight16Regular /></button><div className="xp-preview-feed no-scrollbar" ref={previewFeed}>{preview.rows.map((entry, index) => renderRow(entry, index, true))}</div></aside>}
      </div>
      <footer className="material-chrome xp-bottom"><span>{isBusy(current.status) ? <span className="xp-spinner" /> : <Checkmark20Regular />}{current.status}</span>{model.contacted && !model.delivered && <span className="xp-bottom-partner">{partner?.title} · {model.queued ? '等待空闲' : '等待回复'}</span>}<span className="xp-bottom-end">本地模拟 · 不消耗模型额度</span></footer>
    </div>
  </div>;
}
