import React, { useEffect, useRef, useState } from 'react'
import { useMsal, AuthenticatedTemplate, UnauthenticatedTemplate } from '@azure/msal-react'
import { InteractionStatus } from '@azure/msal-browser'
import { loginRequest } from './authConfig.js'
import {
  getStatus, listIssues, analyzeIssue, sendChat, listNamespaces,
  setNamespace, matchCode, fixIt, searchKey, searchSummary,
  createAdhocIssue, getNamespaceSummary, getDashboard, getVibeUsage,
  getSnowStatus, getSnowIncident,
  getSnowGroupIncidents, approveSnowSummary,
} from './api'

const REFRESH_MS = 5000

// ─── Utilities ───────────────────────────────────────────────────────────────

function timeAgo(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

function renderInline(text) {
  const parts = []
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g
  let last = 0, m, k = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    if (m[1] !== undefined) parts.push(<strong key={k++}>{m[1]}</strong>)
    else parts.push(<code key={k++}>{m[2]}</code>)
    last = re.lastIndex
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function Markdown({ text }) {
  const lines = (text || '').split('\n')
  const blocks = []
  let list = null, k = 0
  const flush = () => { if (list) { blocks.push(<ul key={`u${k++}`}>{list}</ul>); list = null } }
  lines.forEach((ln, idx) => {
    const bullet = ln.match(/^\s*[-*]\s+(.*)/)
    if (bullet) {
      if (!list) list = []
      list.push(<li key={idx}>{renderInline(bullet[1])}</li>)
    } else if (ln.trim() === '') {
      flush()
    } else {
      flush()
      blocks.push(<div key={idx} className="md-line">{renderInline(ln.replace(/^#{1,6}\s*/, ''))}</div>)
    }
  })
  flush()
  return <>{blocks}</>
}

// ─── MDS helpers ─────────────────────────────────────────────────────────────

/** Map internal level strings → MDS tag appearance */
function levelAppearance(level) {
  const l = (level || '').toLowerCase()
  if (l === 'fatal' || l === 'error') return 'error'
  if (l === 'warn' || l === 'warning') return 'warning'
  if (l === 'info') return 'info'
  return 'neutral'
}

/** CSS-animated ring spinner (no web-component dependency) */
function Spinner() {
  return <span className="mds-spinner" role="status" aria-label="loading" />
}

/** Button — maps variant/appearance to MDS token classes */
function Btn({ variant = 'filled', appearance = 'neutral', fit = 'medium', disabled, onClick, children, type = 'button', style }) {
  const cls = [
    'mds-btn',
    `mds-btn--${variant}`,
    `mds-btn--${appearance}`,
    fit === 'small' ? 'mds-btn--sm' : '',
  ].filter(Boolean).join(' ')
  return (
    <button type={type} className={cls} disabled={!!disabled} onClick={onClick} style={style}>
      {children}
    </button>
  )
}

/** Count badge */
function Badge({ children, fit }) {
  return <span className={`mds-badge${fit === 'small' ? ' mds-badge--sm' : ''}`}>{children}</span>
}

/** Inline status/level tag */
function Tag({ appearance = 'neutral', fit, children }) {
  return (
    <span className={`mds-tag mds-tag--${appearance}${fit === 'small' ? ' mds-tag--sm' : ''}`}>
      {children}
    </span>
  )
}

/** Notification / alert banner */
function Notification({ appearance = 'info', heading, children }) {
  const iconMap = { error: '✕', warning: '⚠', success: '✓', info: 'ℹ' }
  return (
    <div className={`mds-notification mds-notification--${appearance}`}>
      <span className="mds-notification__icon">{iconMap[appearance] || 'ℹ'}</span>
      <div className="mds-notification__body">
        {heading && <strong className="mds-notification__heading">{heading}</strong>}
        <div>{children}</div>
      </div>
    </div>
  )
}

/** Typing dots for chat */
function TypingDots() {
  return (
    <span className="dots" aria-label="thinking">
      <i /><i /><i />
    </span>
  )
}

/** Freshness indicator tag next to status */
function FreshnessPill({ ts }) {
  if (!ts) return null
  const age = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  const stale = age > 45
  return (
    <Tag appearance={stale ? 'error' : 'success'} fit="small">
      {stale ? `stalled · ${age}s` : `updated ${age}s ago`}
    </Tag>
  )
}

// ─── Tab bar (styled with MDS tokens, native React click handlers) ───────────

const TABS = [
  { value: 'search',   label: 'Search by Key', icon: '⌕' },
  { value: 'incident', label: 'Incident Search', icon: '◎' },
  { value: 'dashboard',label: 'Dashboard', icon: '▦' },
]

function TabBar({ value, onChange, tabs = TABS }) {
  return (
    <div className="mds-tab-bar" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={value === t.value}
          className={`mds-tab${value === t.value ? ' mds-tab--active' : ''}`}
          onClick={() => onChange(t.value)}
        >
          <span className="mds-tab__icon" aria-hidden="true">{t.icon}</span>
          {t.label}
        </button>
      ))}
    </div>
  )
}

function PageIntro({ eyebrow, title, description, children }) {
  return (
    <div className="mds-page-intro">
      <div>
        <span className="mds-page-intro__eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {children && <div className="mds-page-intro__aside">{children}</div>}
    </div>
  )
}

// ─── Header ──────────────────────────────────────────────────────────────────

function UserWidget() {
  const { instance, accounts } = useMsal()
  const account = accounts[0] || null
  if (!account) return null
  return (
    <div className="mds-header__user">
      <span className="mds-header__user-name" title={account.username}>
        {account.name || account.username}
      </span>
      <button
        className="mds-header__logout-btn"
        onClick={() => instance.logoutRedirect({ postLogoutRedirectUri: window.location.origin })}
      >
        Sign out
      </button>
    </div>
  )
}

function useIsAdmin() {
  // useMsal() is safe here because useIsAdmin is only ever called inside AppShell,
  // which is always rendered inside MsalProvider when ssoEnabled=true.
  // When ssoEnabled=false, AppShell passes isAdmin=true directly (see below).
  const { accounts } = useMsal()
  const roles = accounts[0]?.idTokenClaims?.roles || []
  return roles.includes('TFR_Admin')
}

// Thin wrapper so hooks are never called conditionally.
function AdminGate({ children, ssoEnabled }) {
  // Always calls useMsal() — only render this component inside MsalProvider.
  const { accounts } = useMsal()
  const roles = accounts[0]?.idTokenClaims?.roles || []
  const isAdmin = !ssoEnabled || roles.includes('TFR_Admin')
  return children(isAdmin)
}

function Header({ status, namespaces, onChangeNs, switching, ssoEnabled }) {
  return (
    <header className="mds-header">
      <div className="mds-header__brand">
        <svg className="mds-header__star" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
          <polygon points="12,1 14.2,7.5 20.6,5.1 16.9,10.9 22.7,14.5 15.9,15.1 16.8,21.9 12,17 7.2,21.9 8.1,15.1 1.3,14.5 7.1,10.9 3.4,5.1 9.8,7.5" />
        </svg>
        <span className="mds-header__wordmark">MAERSK</span>
        <span className="mds-header__product">Incident Handler</span>
      </div>

      {status && (
        <div className="mds-header__controls">
          <div className="mds-header__ns-picker">
            <label className="mds-label" htmlFor="ns-select">Namespace</label>
            <select
              id="ns-select"
              className="mds-native-select"
              value={status.namespace || ''}
              disabled={switching || namespaces.length === 0}
              onChange={(e) => onChangeNs(e.target.value)}
            >
              {!namespaces.includes(status.namespace) && status.namespace && (
                <option value={status.namespace}>{status.namespace}</option>
              )}
              {namespaces.map((ns) => (
                <option key={ns} value={ns}>{ns}</option>
              ))}
            </select>
          </div>

          <div className="mds-header__status-tags">
            {switching && (
              <Tag appearance="neutral" fit="small">
                <Spinner /> switching…
              </Tag>
            )}
            <FreshnessPill ts={status.last_poll_ts} />
            <Tag appearance={status.mock ? 'warning' : 'success'} fit="small">
              {status.mock ? 'MOCK DATA' : 'LIVE'}
            </Tag>

            {status.last_error && (
              <Tag appearance="error" fit="small">poll error</Tag>
            )}
          </div>
        </div>
      )}

      {ssoEnabled && <UserWidget />}
    </header>
  )
}

// ─── Issue list ───────────────────────────────────────────────────────────────

function IssueList({ issues, selectedId, onSelect, services, serviceFilter, onChangeService, booted, loading }) {
  return (
    <div className="mds-issue-list">
      <div className="mds-issue-list__head">
        <span className="mds-issue-list__title">Live issues ({issues.length})</span>
        <select
          className="mds-native-select mds-native-select--sm"
          value={serviceFilter}
          onChange={(e) => onChangeService(e.target.value)}
        >
          <option value="all">All services</option>
          {services.map((s) => (
            <option key={s.name} value={s.name}>{s.name} ({s.count})</option>
          ))}
        </select>
      </div>

      {issues.length === 0 && (
        loading || !booted
          ? <div className="mds-empty"><Spinner /> Loading issues…</div>
          : <div className="mds-empty">No issues{serviceFilter !== 'all' ? ' for this service' : ' right now'}.</div>
      )}

      {issues.map((i) => (
        <button
          key={i.id}
          className={`mds-issue-item ${i.id === selectedId ? 'mds-issue-item--selected' : ''}`}
          onClick={() => onSelect(i.id)}
        >
          <div className="mds-issue-item__top">
            <Tag appearance={levelAppearance(i.level)} fit="small">{i.level}</Tag>
            <span className="mds-issue-item__svc">{i.service}</span>
            <Badge fit="small">{i.count}</Badge>
          </div>
          <div className="mds-issue-item__title">{i.title}</div>
          <div className="mds-issue-item__meta">last seen {timeAgo(i.last_seen)}</div>
        </button>
      ))}
    </div>
  )
}

// ─── Analysis ─────────────────────────────────────────────────────────────────

function Analysis({ issue }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => { setData(null) }, [issue.id])

  const run = (refresh) => {
    setLoading(true)
    analyzeIssue(issue.id, refresh)
      .then(setData)
      .catch((e) => setData({ summary: `Error: ${e.message}`, likely_causes: [], suggested_fixes: [] }))
      .finally(() => setLoading(false))
  }

  return (
    <div className="mds-section">
      <div className="mds-section__head">
        <span className="mds-section__title">AI analysis</span>
        <Btn
          variant={loading ? 'plain' : 'outlined'}
          appearance="neutral"
          fit="small"
          disabled={loading}
          onClick={() => run(!!data)}
        >
          {loading ? <><Spinner /> Analyzing…</> : data ? 'Re-analyze' : 'Analyze'}
        </Btn>
      </div>
      {!data && !loading && (
        <p className="mds-hint">Click Analyze to get an AI explanation and suggested fixes.</p>
      )}
      {data && (
        <div className="mds-analysis-body">
          {data.severity && (
            <Tag appearance={levelAppearance(data.severity)} fit="small">{data.severity}</Tag>
          )}
          <p className="mds-body-text">{data.summary}</p>
          {data.likely_causes?.length > 0 && (
            <>
              <h4 className="mds-subheading">Likely causes</h4>
              <ul className="mds-list">{data.likely_causes.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>
          )}
          {data.suggested_fixes?.length > 0 && (
            <>
              <h4 className="mds-subheading">Suggested fixes</h4>
              <ol className="mds-list">{data.suggested_fixes.map((c, i) => <li key={i}>{c}</li>)}</ol>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Code match ───────────────────────────────────────────────────────────────

function CodeMatch({ issue }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [fix, setFix] = useState(null)
  const [fixing, setFixing] = useState(false)

  useEffect(() => { setData(null); setFix(null) }, [issue.id])

  const run = (refresh) => {
    setLoading(true)
    setFix(null)
    matchCode(issue.id, refresh)
      .then(setData)
      .catch((e) => setData({ located: false, summary: `Error: ${e.message}`, files: [] }))
      .finally(() => setLoading(false))
  }

  const runFix = () => {
    const repo = data?.repo || issue.service
    if (!window.confirm(`Open a DRAFT pull request with an AI-generated fix on Maersk-Global/${repo} (targeting develop)?\n\nIt won't be merged automatically — you'll review it on GitHub.`)) return
    setFixing(true)
    setFix(null)
    fixIt(issue.id)
      .then(setFix)
      .catch((e) => setFix({ created: false, error: e.message }))
      .finally(() => setFixing(false))
  }

  return (
    <div className="mds-section">
      <div className="mds-section__head">
        <span className="mds-section__title">Match in code (GitHub)</span>
        <Btn variant="outlined" appearance="neutral" fit="small" disabled={loading} onClick={() => run(!!data)}>
          {loading ? <><Spinner /> Scanning…</> : data ? 'Re-scan' : 'Find in code'}
        </Btn>
      </div>
      {!data && !loading && (
        <p className="mds-hint">Search this service's GitHub repo for the code behind this error.</p>
      )}
      {data && !loading && (
        <div className="mds-analysis-body">
          <div className="mds-repo-line">
            {data.repo_url
              ? <><span className="mds-muted">repo: </span><a href={data.repo_url} target="_blank" rel="noreferrer">{data.repo}</a></>
              : <><span className="mds-muted">repo: </span>{data.repo}</>}
            {data.located && data.confidence && (
              <Tag appearance="info" fit="small">confidence: {data.confidence}</Tag>
            )}
          </div>
          {!data.located && <p className="mds-body-text">{data.summary}</p>}
          {data.located && (
            <>
              {data.files?.length > 0 && (
                <>
                  <h4 className="mds-subheading">Relevant files</h4>
                  <ul className="mds-list mds-list--files">
                    {data.files.map((f, i) => (
                      <li key={i}><a href={f.url} target="_blank" rel="noreferrer">{f.path}</a>{f.reason ? ` — ${f.reason}` : ''}</li>
                    ))}
                  </ul>
                </>
              )}
              <h4 className="mds-subheading">Root cause</h4>
              <p className="mds-body-text">{data.root_cause}</p>
              <h4 className="mds-subheading">Suggested fix</h4>
              <pre className="mds-code-block">{data.suggested_fix}</pre>
              <div className="mds-fix-actions">
                <Btn variant="filled" appearance="primary" fit="small" disabled={fixing} onClick={runFix}>
                  {fixing ? <><Spinner /> Generating fix &amp; opening PR…</> : '🔧 Fix it → draft PR'}
                </Btn>
              </div>
              {fix && !fixing && (
                fix.created ? (
                  <Notification appearance="success" heading="Draft PR opened">
                    <a href={fix.pr_url} target="_blank" rel="noreferrer">#{fix.pr_number} {fix.title}</a>
                    <div>branch <code>{fix.branch}</code> → <code>{fix.base}</code></div>
                    {fix.edits?.length > 0 && (
                      <ul className="mds-list mds-list--files">
                        {fix.edits.map((e, i) => <li key={i}><code>{e.path}</code>{e.explanation ? ` — ${e.explanation}` : ''}</li>)}
                      </ul>
                    )}
                  </Notification>
                ) : (
                  <Notification appearance="error" heading="PR creation failed">{fix.error}</Notification>
                )
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Chat ─────────────────────────────────────────────────────────────────────

function Chat({ issue }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const endRef = useRef(null)

  useEffect(() => { setMessages([]) }, [issue.id])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const submit = async (e) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || busy) return
    const next = [...messages, { role: 'user', content: text }]
    setMessages(next)
    setInput('')
    setBusy(true)
    try {
      const { reply } = await sendChat(issue.id, next)
      setMessages([...next, { role: 'assistant', content: reply }])
    } catch (err) {
      setMessages([...next, { role: 'assistant', content: `Error: ${err.message}` }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mds-section mds-chat">
      <div className="mds-section__head">
        <span className="mds-section__title">Ask about this issue</span>
      </div>
      <div className="mds-chat__log">
        {messages.length === 0 && (
          <p className="mds-hint">e.g. "What's the root cause?" or "How do I reproduce this?"</p>
        )}
        {messages.map((m, idx) => (
          <div key={idx} className={`mds-chat__msg mds-chat__msg--${m.role}`}>
            {m.role === 'assistant' ? <Markdown text={m.content} /> : m.content}
          </div>
        ))}
        {busy && <div className="mds-chat__msg mds-chat__msg--assistant"><TypingDots /></div>}
        <div ref={endRef} />
      </div>
      <form className="mds-chat__form" onSubmit={submit}>
        <textarea
          className="mds-text-input mds-text-input--grow"
          value={input}
          rows={3}
          placeholder="Ask Claude about this error…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(e) } }}
        />
        <Btn variant="filled" appearance="primary" fit="small" disabled={busy} type="submit">
          Send
        </Btn>
      </form>
    </div>
  )
}

// ─── Detail panel ─────────────────────────────────────────────────────────────

function Detail({ issue }) {
  if (!issue) {
    return (
      <div className="mds-detail mds-detail--empty">
        <div className="mds-detail__placeholder">
          <mc-icon icon="mi-info-circle" />
          <p>Select an issue from the list to inspect it.</p>
        </div>
      </div>
    )
  }
  return (
    <div className="mds-detail">
      <div className="mds-detail__service-header">
        <Tag appearance={levelAppearance(issue.level)} fit="small">{issue.level}</Tag>
        <h2 className="mds-detail__service-name">{issue.service}</h2>
        <Badge>{issue.count}</Badge>
      </div>
      <div className="mds-samples">
        {issue.samples.map((s, idx) => <pre key={idx} className="mds-code-block">{s}</pre>)}
      </div>
      <Analysis issue={issue} />
      <CodeMatch issue={issue} />
      <Chat issue={issue} />
    </div>
  )
}

// ─── Problem investigation (search tab) ───────────────────────────────────────

function ProblemInvestigation({ match }) {
  const [issue, setIssue] = useState(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)

  const toggle = () => {
    if (open) { setOpen(false); return }
    setOpen(true)
    if (!issue) {
      setLoading(true)
      createAdhocIssue(match.service, match.message, match.namespace)
        .then(setIssue).catch(() => {}).finally(() => setLoading(false))
    }
  }

  return (
    <div className="mds-investigate">
      <Btn variant="plain" appearance="neutral" fit="small" onClick={toggle}>
        {open ? '▾ Hide investigation' : '▸ Investigate (Analyze · Find in code · Chat)'}
      </Btn>
      {open && (
        loading
          ? <div className="mds-loading-row"><Spinner /> Preparing…</div>
          : issue
            ? <div className="mds-investigate__body"><Analysis issue={issue} /><CodeMatch issue={issue} /><Chat issue={issue} /></div>
            : <p className="mds-hint">Couldn't prepare this match for investigation.</p>
      )}
    </div>
  )
}

// ─── Summary issue inline analyze ─────────────────────────────────────────────

function SummaryIssueAnalysis({ service, liveIssues }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)

  const matchedIssue = liveIssues
    .filter((i) => i.service === service)
    .sort((a, b) => (b.count || 0) - (a.count || 0))[0] || null

  const run = () => {
    if (!matchedIssue) return
    setOpen(true)
    if (data) return
    setLoading(true)
    analyzeIssue(matchedIssue.id, false)
      .then(setData)
      .catch((e) => setData({ summary: `Error: ${e.message}`, likely_causes: [], suggested_fixes: [] }))
      .finally(() => setLoading(false))
  }

  const rerun = () => {
    if (!matchedIssue) return
    setLoading(true)
    setData(null)
    analyzeIssue(matchedIssue.id, true)
      .then(setData)
      .catch((e) => setData({ summary: `Error: ${e.message}`, likely_causes: [], suggested_fixes: [] }))
      .finally(() => setLoading(false))
  }

  if (!matchedIssue) return null

  return (
    <div className="mds-si-analysis">
      {!open ? (
        <Btn variant="outlined" appearance="neutral" fit="small" onClick={run}>🔍 Analyze</Btn>
      ) : (
        <>
          <div className="mds-si-analysis__head">
            <span className="mds-si-analysis__label">AI analysis</span>
            <Btn variant="outlined" appearance="neutral" fit="small" disabled={loading} onClick={rerun}>
              {loading ? <><Spinner /> Analyzing…</> : 'Re-analyze'}
            </Btn>
            <Btn variant="plain" appearance="neutral" fit="small" onClick={() => setOpen(false)}>✕ Close</Btn>
          </div>
          {data && (
            <div className="mds-si-analysis__body">
              {data.severity && <Tag appearance={levelAppearance(data.severity)} fit="small">{data.severity}</Tag>}
              <p className="mds-body-text">{data.summary}</p>
              {data.likely_causes?.length > 0 && (
                <>
                  <h5 className="mds-subheading mds-subheading--xs">Likely causes</h5>
                  <ul className="mds-list">{data.likely_causes.map((c, i) => <li key={i}>{c}</li>)}</ul>
                </>
              )}
              {data.suggested_fixes?.length > 0 && (
                <>
                  <h5 className="mds-subheading mds-subheading--xs">Suggested fixes</h5>
                  <ol className="mds-list">{data.suggested_fixes.map((c, i) => <li key={i}>{c}</li>)}</ol>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ─── Namespace summary tab ────────────────────────────────────────────────────

function NamespaceSummary({ namespace, issues: liveIssues = [] }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => { setData(null); setError(null) }, [namespace])

  const run = (refresh) => {
    setLoading(true)
    setError(null)
    getNamespaceSummary(refresh)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  // Auto-generate when tab is opened or namespace changes
  useEffect(() => { run(false) }, [namespace])

  const healthAppearance = { healthy: 'success', degraded: 'warning', critical: 'error' }

  return (
    <div className="mds-ns-summary">
      <div className="mds-section__head">
        <span className="mds-section__title">Namespace summary — <strong>{namespace}</strong></span>
        <Btn
          variant={data ? 'outlined' : 'filled'}
          appearance="primary"
          fit="small"
          disabled={loading}
          onClick={() => run(!!data)}
        >
          {loading ? <><Spinner /> Generating…</> : data ? 'Refresh' : 'Generate Summary'}
        </Btn>
      </div>

      {error && (
        <Notification appearance="error" heading="Summary failed">{error}</Notification>
      )}

      {!data && !loading && !error && (
        <p className="mds-hint">Generating summary…</p>
      )}

      {data && (
        <div className="mds-summary-body">
          <div className="mds-summary-meta">
            <Tag appearance={healthAppearance[data.overall_health] || 'warning'} fit="small">
              {data.overall_health}
            </Tag>
            <Tag appearance="neutral" fit="small">{data.total_issues} issues</Tag>
            <Tag appearance="neutral" fit="small">{data.affected_services} services</Tag>
            <Tag appearance="neutral" fit="small">
              last {data.minutes >= 60 ? `${data.minutes / 60}h` : `${data.minutes}m`}
            </Tag>
            {data.cached && <Tag appearance="warning" fit="small">cached</Tag>}
          </div>

          <p className="mds-summary-headline">{data.headline}</p>

          {data.top_concern && (
            <Notification appearance="warning" heading="⚠ Top concern">
              {data.top_concern}
            </Notification>
          )}

          {data.issues?.length > 0 && (
            <div className="mds-summary-issues">
              <h4 className="mds-subheading">Affected services</h4>
              {data.issues.map((i, idx) => (
                <div key={idx} className="mds-summary-issue-card">
                  <div className="mds-summary-issue-card__head">
                    <Tag appearance={levelAppearance(i.level)} fit="small">{i.level}</Tag>
                    <span className="mds-summary-issue-card__svc">{i.service}</span>
                    <Badge fit="small">{i.count}</Badge>
                  </div>
                  <p className="mds-summary-issue-card__text">{i.text}</p>
                  <SummaryIssueAnalysis service={i.service} liveIssues={liveIssues} />
                </div>
              ))}
            </div>
          )}

          {data.remote_api_failures?.length > 0 && (
            <div className="mds-summary-remote-failures">
              <h4 className="mds-subheading">🔌 Remote API failures</h4>
              {data.remote_api_failures.map((f, idx) => (
                <div key={idx} className="mds-remote-failure-card">
                  <div className="mds-remote-failure-card__head">
                    <span className="mds-remote-failure-card__caller">{f.caller}</span>
                    <span className="mds-remote-failure-card__arrow">→</span>
                    <span className="mds-remote-failure-card__target">{f.target}</span>
                    <Badge fit="small">{f.count}</Badge>
                  </div>
                  <span className="mds-remote-failure-card__error">{f.error}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Search tab ───────────────────────────────────────────────────────────────

function SearchView() {
  const [key, setKey] = useState('')
  const [minutes] = useState(1440)
  const [res, setRes] = useState(null)
  const [loading, setLoading] = useState(false)
  const [summary, setSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)

  const doSearch = (k) => {
    const q = (k ?? key).trim()
    if (q.length < 3) return
    setKey(q)
    setLoading(true)
    setRes(null)
    setSummary(null)
    searchKey(q, minutes)
      .then((r) => {
        setRes(r)
        if (r && !r.error) {
          setSummaryLoading(true)
          searchSummary(q, minutes).then(setSummary).catch(() => {}).finally(() => setSummaryLoading(false))
        }
      })
      .catch((e) => setRes({ error: e.message, services: [] }))
      .finally(() => setLoading(false))
  }

  const withProblems = res?.services?.filter((s) => s.problem_count > 0) || []

  return (
    <div className="mds-search-view">
      <PageIntro
        eyebrow="Evidence discovery"
        title="Follow an incident across services"
        description="Start with a booking, container, bill of lading, invoice or trace ID. Incident Handler connects the matching logs and highlights the failures that matter."
      >
        <div className="mds-page-intro__signal"><span /> Searches all monitored services</div>
        <div className="mds-page-intro__signal"><span /> Correlates downstream traces</div>
      </PageIntro>
      <form className="mds-search-bar" onSubmit={(e) => { e.preventDefault(); doSearch() }}>
        <span className="mds-search-bar__icon" aria-hidden="true">⌕</span>
        <input
          className="mds-text-input mds-text-input--grow"
          value={key}
          autoFocus
          placeholder="Search a key — booking #, container #, BOL #, or trace id…"
          onChange={(e) => setKey(e.target.value)}
        />
        <Btn variant="filled" appearance="primary" disabled={loading || key.trim().length < 3} type="submit">
          {loading ? <><Spinner /> Searching</> : 'Find evidence'}
        </Btn>
      </form>

      {loading && (
        <div className="mds-loading-row mds-loading-row--big"><Spinner /> Searching logs across all services…</div>
      )}

      {res && !loading && (
        res.error ? (
          <Notification appearance="error" heading="Search error">{res.error}</Notification>
        ) : (
          <div className="mds-search-results">
            {summaryLoading && (
              <div className="mds-loading-row mds-loading-row--big"><Spinner /> Summarizing the issues in plain English…</div>
            )}
            {summary && (
              <div className="mds-summary-card">
                <p className="mds-summary-headline">{summary.headline}</p>
                {summary.issues?.length > 0 && (
                  <ul className="mds-list">
                    {summary.issues.map((it, idx) => (
                      <li key={idx}>
                        {it.text}
                        {(it.service || it.namespace) && (
                          <Tag appearance="neutral" fit="small" style={{ marginLeft: 6 }}>
                            {it.namespace}{it.namespace && it.service ? ' / ' : ''}{it.service}
                          </Tag>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            <div className="mds-search-stats">
              <strong>"{res.key}"</strong> — {res.problem_count} problem(s) across {withProblems.length} service(s);
              {' '}{res.total_matches} total log match(es) in the last {res.minutes >= 1440 ? `${res.minutes/60}h` : res.minutes >= 60 ? `${res.minutes/60}h` : `${res.minutes}m`}.
              <div className="mds-hint">{res.total_matches} log line(s) matched across {res.namespaces?.length || 1} namespace(s)</div>
              {res.note && <div className="mds-hint">{res.note}</div>}
            </div>



            {res.services.length === 0 && (
              <p className="mds-empty">No log lines contain this key in the selected window.</p>
            )}

            {res.services.map((s) => (
              <div key={s.service} className="mds-svc-card">
                <div className="mds-svc-card__head">
                  <span className="mds-svc-card__name">{s.service}</span>
                  {s.namespace && <Tag appearance="info" fit="small">{s.namespace}</Tag>}
                  {s.problem_count > 0 && (
                    <Tag appearance="error" fit="small">{s.problem_count} problem{s.problem_count > 1 ? 's' : ''}</Tag>
                  )}
                  <Tag appearance="neutral" fit="small">{s.total} match{s.total > 1 ? 'es' : ''}</Tag>
                </div>
                {s.problems.length === 0 ? (
                  <p className="mds-hint">Key seen here, but no error/warn lines.</p>
                ) : (
                  s.problems.map((p, i) => (
                    <div key={i} className="mds-match">
                      <div className="mds-match__top">
                        <Tag appearance={levelAppearance(p.level)} fit="small">{p.level}</Tag>
                        <span className="mds-match__msg">{p.message}</span>
                      </div>
                      <div className="mds-match__meta">
                        {p.ts?.slice(0, 19).replace('T', ' ')}
                        {p.trace_id && (
                          <Btn variant="plain" appearance="neutral" fit="small" onClick={() => doSearch(p.trace_id)}>
                            {p.trace_id}
                          </Btn>
                        )}
                      </div>
                      <ProblemInvestigation match={p} />
                    </div>
                  ))
                )}
              </div>
            ))}

            {res.trace_issues?.length > 0 && (
              <div className="mds-trace-issues-section">
                <div className="mds-trace-issues-header">
                  <span className="mds-trace-issues-title">🔗 Related failures found via trace propagation</span>
                  <span className="mds-hint">
                    These errors were not triggered by "{res.key}" directly, but share the same trace ID —
                    downstream services failed as part of the same request chain.
                  </span>
                </div>
                {res.trace_issues.map((s) => (
                  <div key={`trace-${s.service}-${s.namespace}`} className="mds-svc-card mds-svc-card--trace">
                    <div className="mds-svc-card__head">
                      <span className="mds-svc-card__name">{s.service}</span>
                      {s.namespace && <Tag appearance="info" fit="small">{s.namespace}</Tag>}
                      <Tag appearance="warning" fit="small">via trace</Tag>
                      <Tag appearance="error" fit="small">{s.problem_count} error{s.problem_count > 1 ? 's' : ''}</Tag>
                    </div>
                    {s.problems.map((p, i) => (
                      <div key={i} className="mds-match">
                        <div className="mds-match__top">
                          <Tag appearance={levelAppearance(p.level)} fit="small">{p.level}</Tag>
                          <span className="mds-match__msg">{p.message}</span>
                        </div>
                        <div className="mds-match__meta">
                          {p.ts?.slice(0, 19).replace('T', ' ')}
                          {p.trace_id && (
                            <Btn variant="plain" appearance="neutral" fit="small" onClick={() => doSearch(p.trace_id)}>
                              {p.trace_id}
                            </Btn>
                          )}
                        </div>
                        <ProblemInvestigation match={p} />
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      )}
    </div>
  )
}

// ─── Login page ──────────────────────────────────────────────────────────────

function LoginPage() {
  const { instance, inProgress } = useMsal()
  const [error, setError] = useState(null)

  const handleLogin = async () => {
    setError(null)
    try {
      // Use redirect flow — more reliable than popup (no popup blocker issues)
      await instance.loginRedirect(loginRequest)
    } catch (e) {
      setError(e.message)
    }
  }

  const busy = inProgress !== InteractionStatus.None

  return (
    <div className="mds-login-page">
      <div className="mds-login-card">
        <div className="mds-login-brand">
          <svg viewBox="0 0 24 24" width="36" height="36" aria-hidden="true" fill="currentColor">
            <polygon points="12,1 14.2,7.5 20.6,5.1 16.9,10.9 22.7,14.5 15.9,15.1 16.8,21.9 12,17 7.2,21.9 8.1,15.1 1.3,14.5 7.1,10.9 3.4,5.1 9.8,7.5" />
          </svg>
          <span>MAERSK</span>
        </div>
        <h1 className="mds-login-title">Incident Handler</h1>
        <p className="mds-login-subtitle">Sign in with your Maersk account to continue</p>
        {error && <p className="mds-login-error">{error}</p>}
        <Btn
          variant="filled"
          appearance="primary"
          onClick={handleLogin}
          disabled={busy}
        >
          {busy ? <><Spinner /> Redirecting…</> : 'Sign in with Microsoft'}
        </Btn>
      </div>
    </div>
  )
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

const DASHBOARD_HOURS = [
  { label: 'Last 24 hours', value: 24 },
  { label: 'Last 48 hours', value: 48 },
  { label: 'Last 7 days',   value: 168 },
  { label: 'Last 30 days',  value: 720 },
]

function StatCard({ label, value, icon }) {
  return (
    <div className="mds-stat-card">
      <span className="mds-stat-card__icon">{icon}</span>
      <span className="mds-stat-card__value">{value}</span>
      <span className="mds-stat-card__label">{label}</span>
    </div>
  )
}

function ActivityBar({ hourly }) {
  if (!hourly || !hourly.length) return <p className="mds-hint">No hourly data yet.</p>
  const max = Math.max(...hourly.map((h) => h.count), 1)
  return (
    <div className="mds-activity-bars">
      {hourly.map((h) => (
        <div key={h.hour} className="mds-activity-bar-col" title={`${h.count} events`}>
          <div
            className="mds-activity-bar-fill"
            style={{ height: `${Math.round((h.count / max) * 100)}%` }}
          />
          <span className="mds-activity-bar-label">{h.hour}h</span>
        </div>
      ))}
    </div>
  )
}

// ─── Incident Search (ServiceNow) ────────────────────────────────────────────

const ID_TYPE_LABELS = {
  booking: '📦 Booking',
  bol: '📄 Bill of Lading',
  container: '🚢 Container',
  invoice: '🧾 Invoice',
  shipment: '📮 Shipment',
  po: '📋 Purchase Order',
}

const SNOW_STATE_COLOUR = {
  New: 'var(--mds-color-feedback-warning, #e65100)',
  'In Progress': 'var(--mds-color-feedback-warning, #e65100)',
  Resolved: 'var(--mds-color-feedback-success, #2e7d32)',
  Closed: 'var(--mds-color-text-secondary, #888)',
  'On Hold': '#6366f1',
  Cancelled: 'var(--mds-color-text-secondary, #888)',
}

function pipelineTag(status) {
  if (status === 'done' || status === 'hit' || status === 'l1') return 'success'
  if (status === 'l2' || status === 'pending_approval') return 'warning'
  if (status === 'miss' || status === 'failed') return 'error'
  return 'neutral'
}

function ResolverPipeline({ steps }) {
  if (!steps?.length) return null
  return (
    <ol className="mds-resolver-pipeline">
      {steps.map((step, index) => (
        <li key={step.step || index} className={`mds-resolver-pipeline__step mds-resolver-pipeline__step--${step.status}`}>
          <span className="mds-resolver-pipeline__index">{index + 1}</span>
          <div>
            <div className="mds-resolver-pipeline__head">
              <strong>{step.title}</strong>
              <Tag appearance={pipelineTag(step.status)} fit="small">{String(step.status).replace('_', ' ')}</Tag>
            </div>
            <p>{step.detail}</p>
            {step.basis && <small>{step.basis}</small>}
          </div>
        </li>
      ))}
    </ol>
  )
}

function BookingLifecycle({ lifecycle }) {
  if (!lifecycle) return null
  const pending = (lifecycle.transport_orders || []).filter(o => o.acknowledgement !== 'ACCEPTED').length
  const tagAppearance = lifecycle.stuck_at ? 'warning' : lifecycle.booking_status === 'FAILED' ? 'error' : 'success'
  const cancel = lifecycle.cancellation
  const cancelAppearance = cancel?.allowed === true ? 'success' : cancel?.allowed === false ? 'error' : 'warning'
  const cancelLabel = cancel?.allowed === true ? 'Cancel allowed' : cancel?.allowed === false ? 'Cancel not allowed' : 'Cancel: insufficient data'
  return (
    <section className="mds-tms-delivery">
      <div className="mds-tms-delivery__heading">
        <div>
          <span className="mds-eyebrow">Booking lifecycle</span>
          <h3>Where is {lifecycle.booking_id} in the workflow?</h3>
          <p>{lifecycle.summary}</p>
        </div>
        <Tag appearance={tagAppearance}>{lifecycle.headline_tag || lifecycle.work_process_status}</Tag>
      </div>
      <div className="mds-tms-timeline">
        {(lifecycle.steps || []).map(step => (
          <div key={step.label} className={`mds-tms-step${step.state === 'done' ? ' mds-tms-step--done' : ''}${step.state === 'active' ? ' mds-tms-step--active' : ''}${step.state === 'failed' ? ' mds-tms-step--failed' : ''}`}>
            <span>{step.mark}</span>
            <strong>{step.label}</strong>
            <small>{step.detail}</small>
          </div>
        ))}
      </div>
      {lifecycle.transport_orders?.length > 0 && (
        <div className="mds-tms-order-grid">
          {lifecycle.transport_orders.map(order => (
            <article key={order.number}>
              <div>
                <code>{order.number}</code>
                <Tag appearance={order.acknowledgement === 'ACCEPTED' ? 'success' : 'warning'} fit="small">{order.acknowledgement}</Tag>
              </div>
              <small>Version {order.version} · {order.received_at ? `Received ${new Date(order.received_at).toLocaleTimeString()}` : 'No acknowledgement received'}</small>
            </article>
          ))}
        </div>
      )}
      {lifecycle.transport_orders?.length > 0 && pending > 0 && (
        <p className="mds-hint" style={{ marginTop: '0.75rem' }}>{pending} transport order{pending === 1 ? '' : 's'} still waiting on TMS feedback.</p>
      )}
      {cancel && (
        <div className="mds-tms-cancel">
          <div className="mds-tms-cancel__heading">
            <strong>Cancellation check (P6 + P13)</strong>
            <Tag appearance={cancelAppearance} fit="small">{cancelLabel}</Tag>
          </div>
          <ul>
            <li>P6 executed: {cancel.p6?.executed == null ? 'unknown' : cancel.p6.executed ? 'yes — blocked' : 'no'}</li>
            <li>P6 cargo facility date reached: {cancel.p6?.cargo_facility_date_reached == null ? 'unknown' : cancel.p6.cargo_facility_date_reached ? 'yes — blocked' : 'no'}</li>
            <li>P13 any container executed: {cancel.p13?.any_container_executed == null ? 'unknown' : cancel.p13.any_container_executed ? 'yes — blocked' : 'no'}</li>
            <li>P13 ETA passed: {cancel.p13?.eta_passed == null ? 'unknown' : cancel.p13.eta_passed ? 'yes — blocked' : 'no'}</li>
          </ul>
          {(cancel.notes || []).map(note => <p key={note} className="mds-hint">{note}</p>)}
        </div>
      )}
    </section>
  )
}

function ApproveSummary({ incidentNumber, recommendation, patternText, service }) {
  const rec = recommendation || {}
  const [summary, setSummary] = useState(rec.summary || '')
  const [rootCause, setRootCause] = useState(rec.root_cause || '')
  const [fix, setFix] = useState(rec.suggested_fix || '')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    setSummary(rec.summary || '')
    setRootCause(rec.root_cause || '')
    setFix(rec.suggested_fix || '')
    setSaved(null)
    setError(null)
  }, [incidentNumber, rec.summary, rec.root_cause, rec.suggested_fix])

  const approve = async () => {
    if (!rootCause.trim() || !fix.trim()) return
    setSaving(true)
    setError(null)
    try {
      const result = await approveSnowSummary(incidentNumber, {
        summary: summary.trim() || rootCause.trim(),
        root_cause: rootCause.trim(),
        suggested_fix: fix.trim(),
        pattern_text: patternText || '',
        service: service || '',
        notes: 'Approved in incident workspace',
      })
      setSaved(result)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="mds-assignment-panel">
      <div className="mds-assignment-panel__head">
        <div>
          <span className="mds-eyebrow">Resolver summary</span>
          <h3>Approve for the knowledge bank</h3>
        </div>
        <Tag appearance="info" fit="small">Human approval required</Tag>
      </div>
      <label htmlFor={`sum-${incidentNumber}`}>Headline</label>
      <textarea id={`sum-${incidentNumber}`} value={summary} rows={2} onChange={e => { setSummary(e.target.value); setSaved(null) }} />
      <label htmlFor={`rc-${incidentNumber}`}>Root cause</label>
      <textarea id={`rc-${incidentNumber}`} value={rootCause} rows={3} onChange={e => { setRootCause(e.target.value); setSaved(null) }} />
      <label htmlFor={`fix-${incidentNumber}`}>Suggested fix</label>
      <textarea id={`fix-${incidentNumber}`} value={fix} rows={3} onChange={e => { setFix(e.target.value); setSaved(null) }} />
      <div className="mds-assignment-panel__actions">
        <Btn appearance="primary" fit="small" disabled={saving || !rootCause.trim() || !fix.trim() || !!saved} onClick={approve}>
          {saving ? <><Spinner /> Saving…</> : saved ? 'Approved & stored' : 'Approve summary'}
        </Btn>
        <small>Stores the approved wording as a verified RAG entry for the next similar incident.</small>
      </div>
      {error && <Notification appearance="error" heading="Could not save">{error}</Notification>}
      {saved && (
        <Notification appearance="success" heading="Saved to knowledge bank">
          {saved.kb_entry_id ? `Entry ${saved.kb_entry_id} is now searchable by L1.` : 'Recorded. Embeddings were not available, so the entry was not indexed.'}
        </Notification>
      )}
    </section>
  )
}

function SuggestedAssignment({ incidentNumber, assignment, mockMode }) {
  const [reason, setReason] = useState(assignment.reason)
  const [updated, setUpdated] = useState(false)

  const applyAssignment = () => {
    if (!reason.trim()) return
    setUpdated(true)
  }

  return (
    <section className="mds-assignment-panel">
      <div className="mds-assignment-panel__head">
        <div>
          <span className="mds-eyebrow">Suggested ownership</span>
          <h3>Assign to {assignment.team}</h3>
        </div>
        <Tag appearance="info" fit="small">Human approval required</Tag>
      </div>
      <label htmlFor={`assignment-reason-${incidentNumber}`}>Reason to add to the incident</label>
      <textarea
        id={`assignment-reason-${incidentNumber}`}
        value={reason}
        onChange={event => { setReason(event.target.value); setUpdated(false) }}
        rows={3}
      />
      <div className="mds-assignment-panel__actions">
        <Btn appearance="primary" fit="small" disabled={!reason.trim() || updated} onClick={applyAssignment}>
          {updated ? 'Assignment recorded' : 'Assign & update incident'}
        </Btn>
        <small>{mockMode ? 'Demo mode — preview recorded locally; ServiceNow was not changed.' : `This will assign ${incidentNumber} and add the reason as a work note.`}</small>
      </div>
      {updated && <Notification appearance="success" heading={`Suggested assignment: ${assignment.team}`}>{reason}</Notification>}
    </section>
  )
}

function IncidentView() {
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState('group')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [snowStatus, setSnowStatus] = useState(null)

  useEffect(() => {
    getSnowStatus().then(setSnowStatus).catch(() => {})
  }, [])

  const search = async (queryOverride = null) => {
    const searchQuery = typeof queryOverride === 'string' ? queryOverride : query.trim()
    if (!searchQuery) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = mode === 'group'
        ? await getSnowGroupIncidents(searchQuery, 1440, 20)
        : await getSnowIncident(searchQuery, 43200)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleKey = (e) => { if (e.key === 'Enter') search() }

  const inc = result?.incident
  const groupIncidents = result?.incidents || []
  const identifiers = result?.identifiers || {}
  const lokiResults = result?.loki_results || {}
  const hasIdentifiers = Object.keys(identifiers).length > 0

  return (
    <div className="mds-incident-view">
      <PageIntro
        eyebrow="Incident workspace"
        title="Move from alert to action"
        description="Bring together ServiceNow context, business identifiers, related logs and feasible response actions in one investigation."
      >
        <div className="mds-page-intro__signal"><span /> Evidence-backed relevance</div>
        <div className="mds-page-intro__signal"><span /> Human-reviewed actions</div>
      </PageIntro>
      <div className="mds-incident-search-bar">
        <div className="mds-incident-search-bar__title">
          <span className="mds-incident-search-bar__icon">◎</span>
          <div><strong>Open an investigation</strong><small>Search a team queue or a specific incident</small></div>
          {snowStatus && !snowStatus.configured && (
            <Tag appearance="warning" fit="small" style={{ marginLeft: 8 }}>SNOW not configured</Tag>
          )}
          {snowStatus?.auth_mode === 'mock' && (
            <Tag appearance="warning" fit="small">Demo data</Tag>
          )}
          {snowStatus?.configured && snowStatus?.auth_mode === 'client_credentials' && (
            <Tag appearance="success" fit="small">ServiceNow connected</Tag>
          )}
        </div>
        <div className="mds-incident-search-bar__inputs">
          <select className="mds-native-select mds-native-select--sm" value={mode} onChange={e => { setMode(e.target.value); setResult(null) }}>
            <option value="group">Assignment group</option>
            <option value="incident">Incident number</option>
          </select>
          <input
            className="mds-search-input"
            placeholder={mode === 'group' ? 'Group name e.g. Booking Platform' : 'Incident number e.g. INC0012345'}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleKey}
            style={{ width: 260 }}
          />
          <Btn variant="filled" appearance="primary" fit="small" disabled={loading || !query.trim()} onClick={search}>
            {loading ? <><Spinner /> Searching…</> : 'Search'}
          </Btn>
        </div>
        {mode === 'group' && (
          <div className="mds-group-shortcuts">
            <span>Assignment groups</span>
            {['Booking Platform', 'Billing Platform'].map(group => (
              <button key={group} onClick={() => { setQuery(group); search(group) }}>{group}</button>
            ))}
          </div>
        )}
      </div>

      {error && (
        <Notification appearance="error" heading="Search failed">
          {error.includes('503') || error.includes('not configured')
            ? 'ServiceNow credentials are not configured. Ask your admin to set SNOW_USERNAME and SNOW_PASSWORD in the ConfigMap.'
            : error}
        </Notification>
      )}

      {!result && !loading && !error && (
        <p className="mds-hint" style={{ padding: '2rem' }}>
          Search an assignment group to see its active incidents, relevant log evidence and feasible response actions. You can also investigate one incident number.
        </p>
      )}

      {result?.group && (
        <div className="mds-group-results">
          <div className="mds-group-overview">
            <div>
              <span className="mds-eyebrow">Assignment group</span>
              <h2>{result.group}</h2>
              <p>Active incidents correlated against the last {result.minutes / 60} hours of logs.</p>
            </div>
            <div className="mds-group-metrics">
              <div><strong>{result.incident_count}</strong><span>Active incidents</span></div>
              <div><strong>{result.relevant_count}</strong><span>With evidence</span></div>
              <div><strong>{result.incident_count - result.relevant_count}</strong><span>Need triage</span></div>
            </div>
          </div>

          {groupIncidents.length === 0 && <Notification appearance="info" heading="No active incidents">No active incidents were found for this assignment group.</Notification>}
          <div className="mds-group-incident-list">
            {groupIncidents.map(item => (
              <details className="mds-group-incident" key={item.incident.number} open={item.relevance === 'high'}>
                <summary>
                  <div className="mds-group-incident__identity">
                    <code>{item.incident.number}</code>
                    <Tag appearance={item.relevance === 'high' ? 'error' : 'neutral'} fit="small">
                      {item.relevance === 'high' ? 'Relevant evidence' : 'Unconfirmed'}
                    </Tag>
                    <Tag appearance="neutral" fit="small">{item.incident.priority}</Tag>
                  </div>
                  <strong>{item.incident.short_description}</strong>
                  <span>{item.evidence.length} correlated service group{item.evidence.length === 1 ? '' : 's'}</span>
                </summary>
                <div className="mds-group-incident__body">
                  <ResolverPipeline steps={item.pipeline} />
                  {item.agent_solution && (
                    <section className="mds-agent-solution">
                      <div className="mds-agent-solution__top">
                        <div>
                          <span className="mds-eyebrow">Recommended solution</span>
                          <h3>{item.agent_solution.headline}</h3>
                        </div>
                        <div className="mds-confidence-score">
                          <strong>{Math.round(item.agent_solution.final_confidence * 100)}%</strong>
                          <span>confidence</span>
                        </div>
                      </div>
                      <div className={`mds-agent-path${item.agent_solution.agents.length === 1 ? ' mds-agent-path--single' : ''}`}>
                        {item.agent_solution.agents.map((agent, index) => (
                          <React.Fragment key={agent.level}>
                            {index > 0 && <span className="mds-agent-path__arrow">→</span>}
                            <article className={`mds-agent-card mds-agent-card--${agent.decision.toLowerCase()}`}>
                              <div className="mds-agent-card__head">
                                <span>{agent.level}</span>
                                <strong>{agent.name}</strong>
                                <Tag appearance={agent.decision === 'RECOMMENDED' ? 'success' : 'warning'} fit="small">{agent.decision}</Tag>
                              </div>
                              <div className="mds-agent-card__confidence">
                                <div><span style={{ width: `${agent.confidence * 100}%` }} /></div>
                                <strong>{Math.round(agent.confidence * 100)}%</strong>
                              </div>
                              <p>{agent.summary}</p>
                              <small>{agent.basis}</small>
                            </article>
                          </React.Fragment>
                        ))}
                      </div>
                      <div className="mds-agent-conclusion">
                        <div><strong>Root cause</strong><p>{item.agent_solution.root_cause}</p></div>
                        <div><strong>Recommended steps</strong><p>{item.agent_solution.recommended_solution}</p></div>
                      </div>
                      {item.agent_solution.code_change && (
                        <div className="mds-code-fix">
                          <div className="mds-code-fix__title"><span>⌘</span><strong>Exact code change from L2</strong><Tag appearance="info" fit="small">Line {item.agent_solution.code_change.line}</Tag></div>
                          <code>{item.agent_solution.code_change.repository}/{item.agent_solution.code_change.file}:{item.agent_solution.code_change.line}</code>
                          <dl>
                            <div><dt>Symbol</dt><dd>{item.agent_solution.code_change.symbol}</dd></div>
                            <div><dt>Problem</dt><dd>{item.agent_solution.code_change.problem}</dd></div>
                            <div><dt>Proposed fix</dt><dd>{item.agent_solution.code_change.fix}</dd></div>
                          </dl>
                        </div>
                      )}
                    </section>
                  )}
                  <ApproveSummary
                    incidentNumber={item.incident.number}
                    recommendation={item.recommendation}
                    patternText={item.pattern_text}
                    service={item.resolver_service}
                  />
                  {item.suggested_assignment && (
                    <SuggestedAssignment incidentNumber={item.incident.number} assignment={item.suggested_assignment} mockMode={snowStatus?.auth_mode === 'mock'} />
                  )}
                  <BookingLifecycle lifecycle={item.booking_lifecycle || item.tms_delivery} />
                  <section>
                    <h3>What is relevant</h3>
                    <div className="mds-relevance-strip">
                      {Object.entries(item.identifiers).flatMap(([type, values]) => values.map(value => (
                        <span key={`${type}-${value}`}><small>{ID_TYPE_LABELS[type] || type}</small><code>{value}</code></span>
                      )))}
                      {Object.keys(item.identifiers).length === 0 && <p className="mds-hint">No business identifiers were extracted from this incident.</p>}
                    </div>
                  </section>
                  <section>
                    <h3>Related log evidence</h3>
                    {item.evidence.length === 0 && <p className="mds-hint">No related error logs found in this window.</p>}
                    {item.evidence.map((group, index) => (
                      <details className="mds-evidence-group" key={`${group.matched_identifier}-${group.service}-${index}`}>
                        <summary>
                          <strong>{group.service}</strong>
                          <span>{group.namespace}</span>
                          <Badge fit="small">{group.count}</Badge>
                          <code>{group.matched_identifier}</code>
                        </summary>
                        <div className="mds-evidence-logs">
                          {group.logs.map((log, logIndex) => (
                            <div key={logIndex}><span>{log.ts || 'time unavailable'}</span><code>{log.message}</code></div>
                          ))}
                        </div>
                      </details>
                    ))}
                  </section>
                  <section>
                    <h3>Feasible actions</h3>
                    <div className="mds-action-grid">
                      {item.actions.map((action, index) => (
                        <article key={index}>
                          <span className={`mds-action-kind mds-action-kind--${action.kind}`}>{action.kind}</span>
                          <strong>{action.title}</strong>
                          <p>{action.detail}</p>
                        </article>
                      ))}
                    </div>
                  </section>
                </div>
              </details>
            ))}
          </div>
        </div>
      )}

      {inc && (
        <div className="mds-incident-details">
          <div className="mds-incident-header">
            <div className="mds-incident-header__left">
              <span className="mds-incident-number">{inc.number}</span>
              <span className="mds-incident-state" style={{ color: SNOW_STATE_COLOUR[inc.state] || 'inherit' }}>
                ● {inc.state}
              </span>
              {inc.priority && <Tag appearance="neutral" fit="small">{inc.priority}</Tag>}
            </div>
            <div className="mds-incident-header__right mds-hint">
              {inc.assignment_group && <span>👥 {inc.assignment_group}</span>}
              {inc.opened_at && <span>🕐 {new Date(inc.opened_at).toLocaleString()}</span>}
            </div>
          </div>
          <p className="mds-incident-description">{inc.short_description}</p>
          {inc.description && inc.description !== inc.short_description && (
            <details className="mds-incident-detail-block">
              <summary className="mds-hint">Full description</summary>
              <pre className="mds-incident-pre">{inc.description}</pre>
            </details>
          )}
          {inc.close_notes && (
            <details className="mds-incident-detail-block">
              <summary className="mds-hint">Resolution notes</summary>
              <pre className="mds-incident-pre">{inc.close_notes}</pre>
            </details>
          )}
        </div>
      )}

      {inc && result?.pipeline && <ResolverPipeline steps={result.pipeline} />}

      {inc && result?.booking_lifecycle && <BookingLifecycle lifecycle={result.booking_lifecycle} />}

      {inc && result?.agent_solution && (
        <div className="mds-direct-solution">
          <div>
            <span className="mds-eyebrow">{result.agent_solution.status.replace('_', ' ')}</span>
            <h3>{result.agent_solution.headline}</h3>
            <p><strong>Root cause:</strong> {result.agent_solution.root_cause}</p>
            <p><strong>Recommended solution:</strong> {result.agent_solution.recommended_solution}</p>
            {result.agent_solution.code_change && (
              <code>{result.agent_solution.code_change.repository}/{result.agent_solution.code_change.file}:{result.agent_solution.code_change.line}</code>
            )}
          </div>
          <div className="mds-confidence-score"><strong>{Math.round(result.agent_solution.final_confidence * 100)}%</strong><span>confidence</span></div>
        </div>
      )}

      {inc && result?.recommendation && (
        <ApproveSummary
          incidentNumber={inc.number}
          recommendation={result.recommendation}
          patternText={result.pattern_text}
          service={result.resolver_service}
        />
      )}

      {inc && result?.suggested_assignment && (
        <SuggestedAssignment incidentNumber={inc.number} assignment={result.suggested_assignment} mockMode={snowStatus?.auth_mode === 'mock'} />
      )}

      {inc && !hasIdentifiers && (
        <Notification appearance="warning" heading="No identifiers found">
          Could not extract any booking, container, BOL or invoice numbers from this incident. You can use Search by Key tab to search manually.
        </Notification>
      )}

      {hasIdentifiers && (
        <div className="mds-incident-identifiers">
          <h3 className="mds-section__title" style={{ padding: '0 0 .5rem' }}>
          Extracted identifiers — searching all available logs in Grafana
          </h3>
          {Object.entries(identifiers).map(([type, values]) =>
            values.map(val => {
              const res = lokiResults[val]
              const serviceGroups = [...(res?.services || []), ...(res?.trace_issues || [])]
              const issues = serviceGroups.flatMap(group =>
                (group.problems || []).map(log => ({
                  namespace: log.namespace || group.namespace,
                  service: log.service || group.service,
                  text: log.message || '',
                }))
              )
              const nsCount = new Set(issues.map(issue => issue.namespace).filter(Boolean)).size
              const hasError = !!res?.error
              return (
                <div key={`${type}-${val}`} className="mds-incident-id-block">
                  <div className="mds-incident-id-block__header">
                    <span className="mds-incident-id-type">{ID_TYPE_LABELS[type] || type}</span>
                    <code className="mds-incident-id-value">{val}</code>
                    {hasError
                      ? <Tag appearance="error" fit="small">Search error</Tag>
                      : issues.length === 0
                        ? <Tag appearance="neutral" fit="small">No issues found</Tag>
                        : <Tag appearance="error" fit="small">{issues.length} issue{issues.length !== 1 ? 's' : ''} across {nsCount} namespace{nsCount !== 1 ? 's' : ''}</Tag>
                    }
                  </div>
                  {hasError && <p className="mds-error" style={{ marginTop: 4, fontSize: '0.8rem' }}>{res.error}</p>}
                  {issues.length > 0 && (
                    <div className="mds-incident-issues">
                      {issues.map((issue, i) => (
                        <div key={i} className="mds-incident-issue-row">
                          <span className="mds-incident-issue-ns">{issue.namespace}</span>
                          <span className="mds-incident-issue-svc">{issue.service}</span>
                          <span className="mds-incident-issue-text">{issue.text}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}

function DashboardView() {
  const [hours, setHours] = useState(24)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [vibeUsage, setVibeUsage] = useState(null)

  const load = (h) => {
    setLoading(true)
    setError(null)
    getDashboard(h)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load(hours) }, [hours])
  useEffect(() => { getVibeUsage().then(setVibeUsage).catch(() => {}) }, [])

  const ACTION_LABELS = {
    login: '🔑 Logins',
    search: '🔎 Searches',
    analyze: '🤖 Analyses',
    namespace_change: '🗂 NS Changes',
  }

  return (
    <div className="mds-dashboard">
      <div className="mds-dashboard__toolbar">
        <div className="mds-dashboard__heading">
          <span className="mds-page-intro__eyebrow">Incident operations</span>
          <h2 className="mds-dashboard__title">Handling dashboard</h2>
          <p>Investigation activity, evidence queries and AI-assisted analyses.</p>
        </div>
        <select
          className="mds-native-select mds-native-select--sm"
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
        >
          {DASHBOARD_HOURS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <button className="mds-btn mds-btn--ghost mds-btn--sm" onClick={() => load(hours)} disabled={loading}>
          {loading ? '…' : '↻ Refresh'}
        </button>
      </div>

      {error && <p className="mds-error">{error}</p>}

      {vibeUsage && !vibeUsage.error && (
        <div className="mds-vibe-usage-card">
          <div className="mds-vibe-usage-card__header">
            <span className="mds-vibe-usage-card__title">🔑 Vibe Proxy API Key — {vibeUsage.key_alias}</span>
            <span className="mds-vibe-usage-card__models">{vibeUsage.models?.join(', ')}</span>
          </div>
          <div className="mds-vibe-usage-card__body">
            <div className="mds-vibe-usage-metric">
              <span className="mds-vibe-usage-metric__label">Spent</span>
              <span className="mds-vibe-usage-metric__value mds-vibe-usage-metric__value--spend">
                ${vibeUsage.spend?.toFixed(4)}
              </span>
            </div>
            <div className="mds-vibe-usage-metric">
              <span className="mds-vibe-usage-metric__label">Budget</span>
              <span className="mds-vibe-usage-metric__value">${vibeUsage.max_budget}</span>
            </div>
            <div className="mds-vibe-usage-metric">
              <span className="mds-vibe-usage-metric__label">Remaining</span>
              <span className="mds-vibe-usage-metric__value mds-vibe-usage-metric__value--remaining">
                ${(vibeUsage.max_budget - vibeUsage.spend).toFixed(4)}
              </span>
            </div>
            <div className="mds-vibe-usage-metric">
              <span className="mds-vibe-usage-metric__label">Resets</span>
              <span className="mds-vibe-usage-metric__value mds-hint">
                {vibeUsage.budget_reset_at ? new Date(vibeUsage.budget_reset_at).toLocaleDateString() : '—'}
              </span>
            </div>
            <div className="mds-vibe-usage-metric">
              <span className="mds-vibe-usage-metric__label">Expires</span>
              <span className="mds-vibe-usage-metric__value mds-hint">
                {vibeUsage.expires ? new Date(vibeUsage.expires).toLocaleDateString() : '—'}
              </span>
            </div>
          </div>
          {vibeUsage.max_budget && (
            <div className="mds-vibe-usage-bar">
              <div
                className="mds-vibe-usage-bar__fill"
                style={{ width: `${Math.min(100, (vibeUsage.spend / vibeUsage.max_budget) * 100).toFixed(1)}%` }}
              />
            </div>
          )}
        </div>
      )}

      {data && (
        <>
          <div className="mds-stat-cards">
            <StatCard label="Evidence Searches" value={data.searches} icon="⌕" />
            <StatCard label="Incident Analyses" value={data.analyses} icon="◎" />
            <StatCard label="Active Handlers" value={data.unique_users} icon="◉" />
            <StatCard label="Handling Actions" value={data.searches + data.analyses} icon="↗" />
          </div>

          <div className="mds-dashboard__row">
            <div className="mds-dashboard__panel">
              <h3 className="mds-dashboard__panel-title">Handling activity · last 24 hours</h3>
              <ActivityBar hourly={data.hourly_activity} />
            </div>

            <div className="mds-dashboard__panel">
              <h3 className="mds-dashboard__panel-title">Active incident handlers</h3>
              {data.top_users.length === 0
                ? <p className="mds-hint">No data yet.</p>
                : (
                  <table className="mds-table">
                    <thead><tr><th>User</th><th>Events</th></tr></thead>
                    <tbody>
                      {data.top_users.map((u) => (
                        <tr key={u.user_email}>
                          <td>
                            <span className="mds-user-email">{u.user_email}</span>
                            {u.user_name && <span className="mds-hint"> ({u.user_name})</span>}
                          </td>
                          <td>{u.event_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
            </div>

            <div className="mds-dashboard__panel">
              <h3 className="mds-dashboard__panel-title">Investigation actions</h3>
              {data.action_counts.length === 0
                ? <p className="mds-hint">No data yet.</p>
                : (
                  <table className="mds-table">
                    <thead><tr><th>Action</th><th>Count</th></tr></thead>
                    <tbody>
                      {data.action_counts.filter((a) => ['search', 'analyze'].includes(a.action)).map((a) => (
                        <tr key={a.action}>
                          <td>{ACTION_LABELS[a.action] || a.action}</td>
                          <td>{a.cnt}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
            </div>
          </div>

          <div className="mds-dashboard__row">
            <div className="mds-dashboard__panel">
              <h3 className="mds-dashboard__panel-title">🤖 OpenAI Token Usage</h3>
              {!data.api_usage?.filter(u => u.api === 'openai').length
                ? <p className="mds-hint">No OpenAI calls yet.</p>
                : (() => {
                    const rows = data.api_usage.filter(u => u.api === 'openai')
                    const totalTokens = rows.reduce((s, r) => s + (r.total || 0), 0)
                    const totalCalls = rows.reduce((s, r) => s + (r.calls || 0), 0)
                    return (
                      <>
                        <div className="mds-api-usage-summary">
                          <span className="mds-api-usage-stat"><strong>{totalCalls}</strong> calls</span>
                          <span className="mds-api-usage-stat"><strong>{totalTokens.toLocaleString()}</strong> total tokens</span>
                        </div>
                        <table className="mds-table">
                          <thead><tr><th>Model</th><th>Calls</th><th>Prompt</th><th>Completion</th><th>Total</th></tr></thead>
                          <tbody>
                            {rows.map((r, i) => (
                              <tr key={i}>
                                <td className="mds-hint">{r.model || '—'}</td>
                                <td>{r.calls}</td>
                                <td>{(r.prompt || 0).toLocaleString()}</td>
                                <td>{(r.completion || 0).toLocaleString()}</td>
                                <td><strong>{(r.total || 0).toLocaleString()}</strong></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </>
                    )
                  })()
              }
            </div>

            <div className="mds-dashboard__panel">
              <h3 className="mds-dashboard__panel-title">📡 Grafana API Calls</h3>
              {!data.api_usage?.filter(u => u.api === 'grafana').length
                ? <p className="mds-hint">No Grafana calls recorded yet.</p>
                : (() => {
                    const grafanaRows = data.api_usage.filter(u => u.api === 'grafana')
                    const totalCalls = grafanaRows.reduce((s, r) => s + (r.calls || 0), 0)
                    return (
                      <>
                        <div className="mds-api-usage-summary">
                          <span className="mds-api-usage-stat"><strong>{totalCalls}</strong> Loki queries</span>
                        </div>
                        {data.grafana_hourly?.length > 0 && (
                          <table className="mds-table">
                            <thead><tr><th>Hour (ago)</th><th>Queries</th></tr></thead>
                            <tbody>
                              {data.grafana_hourly.slice(-8).map((r, i) => (
                                <tr key={i}>
                                  <td className="mds-hint">{r.hour}h ago</td>
                                  <td>{r.calls}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                      </>
                    )
                  })()
              }
            </div>
          </div>

          <div className="mds-dashboard__panel mds-dashboard__panel--full">
            <h3 className="mds-dashboard__panel-title">Recent incident-handling activity</h3>
            {data.recent_events.filter((e) => ['search', 'analyze'].includes(e.action)).length === 0
              ? <p className="mds-hint">No events yet.</p>
              : (
                <table className="mds-table">
                  <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Detail</th></tr></thead>
                  <tbody>
                    {data.recent_events.filter((e) => ['search', 'analyze'].includes(e.action)).map((e, i) => (
                      <tr key={i}>
                        <td className="mds-hint">{new Date(e.ts * 1000).toLocaleTimeString()}</td>
                        <td>{e.user_email}</td>
                        <td>{ACTION_LABELS[e.action] || e.action}</td>
                        <td className="mds-hint">{e.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
          </div>
        </>
      )}
    </div>
  )
}

// ─── App root ─────────────────────────────────────────────────────────────────

function AppShell({ ssoEnabled }) {
  const [status, setStatus] = useState(null)
  const [namespaces, setNamespaces] = useState([])
  const [switching, setSwitching] = useState(false)
  const [tab, setTab] = useState('search')

  useEffect(() => {
    getStatus().then(setStatus).catch((e) => console.error('getStatus failed:', e))
    listNamespaces().then(async (nsList) => {
      setNamespaces(nsList)
      const saved = localStorage.getItem('tfr_namespace')
      if (saved && nsList.includes(saved)) {
        try {
          await setNamespace(saved)
          getStatus().then(setStatus).catch(() => {})
        } catch { /* keep the default namespace */ }
      }
    }).catch((e) => console.error('listNamespaces failed:', e))
  }, [])

  const onChangeNs = async (ns) => {
    setSwitching(true)
    try {
      await setNamespace(ns)
      localStorage.setItem('tfr_namespace', ns)
      setStatus((s) => (s ? { ...s, namespace: ns } : s))
      getStatus().then(setStatus).catch(() => {})
    } catch { /* keep current view */ } finally {
      setSwitching(false)
    }
  }

  const workspace = (isAdmin) => (
    <div className="mds-app">
      <Header status={status} namespaces={namespaces} onChangeNs={onChangeNs} switching={switching} ssoEnabled={ssoEnabled} />
      <TabBar value={tab} onChange={setTab} tabs={isAdmin ? TABS : TABS.filter((item) => item.value === 'search')} />
      <main className="mds-app__content mds-workspace">
        {tab === 'search' && <SearchView />}
        {tab === 'incident' && isAdmin && <IncidentView />}
        {tab === 'dashboard' && isAdmin && <DashboardView />}
      </main>
    </div>
  )

  if (!ssoEnabled) {
    return workspace(true)
  }

  return (
    <AdminGate ssoEnabled={ssoEnabled}>
      {(isAdmin) => workspace(isAdmin)}
    </AdminGate>
  )
}

export default function App({ ssoEnabled = false }) {
  if (!ssoEnabled) {
    return <AppShell ssoEnabled={false} />
  }

  return (
    <>
      <AuthenticatedTemplate>
        <AppShell ssoEnabled={true} />
      </AuthenticatedTemplate>
      <UnauthenticatedTemplate>
        <LoginPage />
      </UnauthenticatedTemplate>
    </>
  )
}
