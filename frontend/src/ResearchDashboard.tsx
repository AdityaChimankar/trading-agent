import { lazy, Suspense, useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost, setApiToken } from './api/client'
import { useApiAuth } from './api/useApiAuth'
import type { ChartResponse } from './api/types'
import { Metric, Panel } from './components/ui'
import { inr, num } from './lib/format'
import OperatingConsole from './OperatingConsole'
import WalkForwardPanel from './components/WalkForwardPanel'
import OutcomeValidationPanel from './components/OutcomeValidationPanel'
import OutcomeModelsPanel from './components/OutcomeModelsPanel'
import ResearchWorkflow, { type ResearchStage } from './components/ResearchWorkflow'

const PriceChart = lazy(() => import('./components/PriceChart'))
type Action = 'BUY' | 'SELL' | 'HOLD'
interface ResearchRow {
  symbol: string; interval: string; timestamp: string; price: number; atr: number; rsi: number; adx: number
  action: Action; score: number; confidence: number; reasoning: string; stale: boolean; age_minutes: number
  patterns: string[]; pattern_bias: string; agreements: string[]; disagreements: string[]
  rule: { action: Action; confidence: number }
  ml: { action: Action | null; confidence: number | null; status: string }
  sentiment: { score: number; rationale: string } | null
  expires_at?: string
  validation?: { status: string; win_probability: number | null; expected_net_return_pct: number | null; reason: string }
  plan?: { entry_price: number; stop_loss: number | null; take_profit: number | null; quantity: number
    position_value: number; risk_amount: number; reward_risk: number; blocked: boolean; block_reason: string | null
    room_reward_risk: number | null }
}
interface News {
  status: string; refreshed_at?: string
  articles: { headline: string; url: string; source: string; published_at: string }[]
  analysis: { score: number; confidence: number; rationale: string; model: string } | null
}
interface Scan {
  status: string; total: number; processed: number; evaluated?: number; stale?: number; eligible?: number
  rows: ResearchRow[]; errors: { symbol: string | null; reason: string }[]; skipped: { symbol: string; reason: string }[]
  current_symbol?: string; model_status?: string; model_warning?: string; effective_capital?: number; completed_at?: string; started_at?: string
  options?: { capital: number; interval: string; mode: string; risk_pct: number; reward_risk: number; require_validated?: boolean }
}
const pct = (value: number | null) => value == null ? 'Unavailable' : `${(value * 100).toFixed(1)}%`

function ErrorNotice({ error }: { error: Error | null }) {
  return error ? <p role="alert" className="error">{error.message}</p> : null
}

function Pick({ row, onSelect }: { row: ResearchRow; onSelect: (symbol: string) => void }) {
  return <article className="pick">
    <header className="pick-head"><h3>{row.symbol}</h3><span className={`tag ${row.action === 'BUY' ? 'tag-buy' : 'tag-sell'}`}>{row.action}</span></header>
    <div className="pick-score"><span className="pick-score-value">{num(row.score, 1)}</span><span className="muted small">signal score / 100 (not profit probability)</span></div>
    <p>{row.reasoning}</p>
    <p>Historical win estimate: {pct(row.validation?.win_probability ?? null)} · Net expectancy: {row.validation?.expected_net_return_pct == null ? 'Unavailable' : `${row.validation.expected_net_return_pct.toFixed(3)}%`}</p>
    <p className="small muted">{row.validation?.reason ?? 'Not independently validated'}</p>
    <p>Rules: {row.rule.action} · ML: {row.ml.action ?? row.ml.status} ({pct(row.ml.confidence)})</p>
    {row.agreements.map(text => <p className="agree small" key={text}>{text}</p>)}
    {row.disagreements.map(text => <p className="disagree small" key={text}>{text}</p>)}
    <p>Entry {inr(row.price)} · Stop {inr(row.plan?.stop_loss)} · Target {inr(row.plan?.take_profit)}</p>
    <p>{row.plan?.quantity ?? 0} shares · Value {inr(row.plan?.position_value)} · Risk {inr(row.plan?.risk_amount)}</p>
    <p className="muted small">Candle: {row.timestamp} · {row.interval}</p>
    {row.plan?.blocked && <p className="warn-text">{row.plan.block_reason}</p>}
    <button className="btn" onClick={() => onSelect(row.symbol)}>Select {row.symbol}</button>
  </article>
}

export default function ResearchDashboard() {
  const cache = useQueryClient()
  const auth = useApiAuth()
  const [page, setPage] = useState<'research' | 'paper'>('research')
  const [stage, setStage] = useState<ResearchStage>('find')
  const [dependenciesChangedAt, setDependenciesChangedAt] = useState(0)
  const [dependencyNotice, setDependencyNotice] = useState('')
  const [validationActivity, setValidationActivity] = useState<Record<string, string>>({})
  const [optimizerActivity, setOptimizerActivity] = useState<Record<string, string>>({})
  const [token, setToken] = useState('')
  const [symbol, setSymbol] = useState('RELIANCE')
  const [search, setSearch] = useState('')
  const [direction, setDirection] = useState<Action>('BUY')
  const [capital, setCapital] = useState('100000')
  const [interval, setInterval] = useState('minute')
  const [mode, setMode] = useState('rule_ml_combined')
  const [risk, setRisk] = useState('1')
  const [reward, setReward] = useState('2')
  const [useLlm, setUseLlm] = useState(false)
  const [requireValidated, setRequireValidated] = useState(true)
  const [clockNow, setClockNow] = useState(Date.now)
  useEffect(() => {
    const timer = window.setInterval(() => setClockNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  const universe = useQuery({ queryKey: ['research-universe'], queryFn: () => apiGet<{ symbols: string[]; count: number; llm_provider: string }>('/api/v1/research/universe') })
  const scan = useQuery({ queryKey: ['research-scan'], queryFn: () => apiGet<Scan>('/api/v1/research/scan'), refetchInterval: query => query.state.data?.status === 'running' ? 3000 : 30000 })
  const book = useQuery({ queryKey: ['research-book'], queryFn: () => apiGet<{ cash: number; total_value: number; open_positions: number; total_pnl: number }>('/api/v1/wallet/summary'), refetchInterval: 30000 })
  const detail = useQuery({ queryKey: ['research-symbol', symbol, interval, mode], queryFn: () => apiGet<{ analysis: ResearchRow; chart: ChartResponse; news: News }>(`/api/v1/research/symbols/${encodeURIComponent(symbol)}?interval=${interval}&mode=${mode}`), enabled: page === 'research' && Boolean(symbol), staleTime: 30000, retry: false })
  const news = useQuery({ queryKey: ['research-news', symbol], queryFn: () => apiGet<News>(`/api/v1/research/symbols/${encodeURIComponent(symbol)}/news`), enabled: page === 'research' && Boolean(symbol) })
  const start = useMutation({ mutationFn: () => apiPost<Scan>('/api/v1/research/scan', { capital: Number(capital), interval, mode, risk_pct: Number(risk), reward_risk: Number(reward), require_validated: requireValidated }), onSuccess: data => cache.setQueryData(['research-scan'], data) })
  const cancel = useMutation({ mutationFn: () => apiPost('/api/v1/research/scan/cancel'), onSuccess: () => { void cache.invalidateQueries({ queryKey: ['research-scan'] }) } })
  function invalidatePlans(reason: string) {
    setDependenciesChangedAt(Date.now())
    setDependencyNotice(reason)
    void cache.invalidateQueries({ queryKey: ['research-scan'] })
    void cache.invalidateQueries({ queryKey: ['research-symbol'] })
  }
  const refreshNews = useMutation({ mutationFn: (input: { symbol: string; useLlm: boolean }) => apiPost<News>(`/api/v1/research/symbols/${encodeURIComponent(input.symbol)}/news?use_llm=${input.useLlm}`), onSuccess: (data, input) => { cache.setQueryData(['research-news', input.symbol], data); invalidatePlans(`News refreshed for ${input.symbol}. Re-scan to incorporate any new sentiment.`) } })
  function submit(event: FormEvent) { event.preventDefault(); setStage('find'); start.mutate() }
  function inspect(value = symbol) { setSymbol(value); setStage('inspect'); setPage('research') }
  const running = scan.data?.status === 'running'
  const rows = scan.data?.rows ?? []
  const selected = rows.find(row => row.symbol === symbol)
  const dependenciesChanged = dependenciesChangedAt > 0 && !(Date.parse(scan.data?.started_at ?? '') > dependenciesChangedAt)
  const matchesSettings = Boolean(!dependenciesChanged && scan.data?.options && scan.data.options.interval === interval && scan.data.options.mode === mode && scan.data.options.capital === Number(capital) && scan.data.options.risk_pct === Number(risk) && scan.data.options.reward_risk === Number(reward) && scan.data.options.require_validated === requireValidated)
  const row = detail.data?.analysis
  const currentPlan = matchesSettings && scan.data?.status === 'completed' ? selected?.plan : undefined
  const plan = currentPlan && !(selected?.expires_at && Date.parse(selected.expires_at) > clockNow)
    ? { ...currentPlan, blocked: true, block_reason: 'Expired snapshot; re-scan', quantity: 0, position_value: 0, risk_amount: 0 }
    : currentPlan
  const sideRows = rows.filter(entry => entry.action === direction && entry.symbol.includes(search.toUpperCase()))
  const availableSymbols = (universe.data?.symbols ?? []).filter(value => value.includes(search.toUpperCase()))
  const planStatus = !matchesSettings ? 'Re-scan required' : !plan ? 'No completed plan' : plan.blocked ? 'Blocked / expired' : plan.quantity > 0 ? 'Reviewable research plan' : 'No allocation'
  return <div className="app">
    <header className="topbar"><div className="topbar-left"><span className="brand"><span className="brand-dot" />Intraday Trading Agent</span>
      <nav className="nav" aria-label="Workspace"><button className={`nav-link ${page === 'research' ? 'is-active' : ''}`} aria-pressed={page === 'research'} onClick={() => setPage('research')}>Research dashboard</button><button className={`nav-link ${page === 'paper' ? 'is-active' : ''}`} aria-pressed={page === 'paper'} onClick={() => setPage('paper')}>Paper operations</button></nav>
    </div><div className="topbar-right"><label>Symbol <input aria-label="Selected research symbol" list="research-symbols" value={symbol} onChange={event => setSymbol(event.target.value.trim().toUpperCase())} /></label><datalist id="research-symbols">{universe.data?.symbols.map(value => <option key={value} value={value} />)}</datalist>
      <button className="btn" onClick={() => { void cache.invalidateQueries() }}>Refresh views</button></div>
    </header>
    {auth.data?.required && page === 'research' && <details className="session-auth"><summary>API authentication</summary><form onSubmit={event => { event.preventDefault(); setApiToken(token); setToken(''); void cache.invalidateQueries() }}><label>Session API token <input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label><button>Apply session token</button></form></details>}
    {auth.isError && <p className="error" role="alert">Could not check API authentication settings. Check the API connection and update both API and frontend, then refresh views.</p>}
    {page === 'paper' && <OperatingConsole symbol={symbol} interval={interval} onSymbolChange={setSymbol} onIntervalChange={setInterval} onInspect={() => inspect()} onBookChanged={() => invalidatePlans('Paper book changed. Re-scan to recalculate capital, exposure and sizing.')} />}
    <div className="layout" hidden={page !== 'research'}>
      <aside className="sidebar"><div className="side-block"><h2 className="panel-title">Watchlist Bias</h2><p className="muted small">{universe.data?.count ?? '…'} unique configured stocks</p>
        <input aria-label="Search watchlist" placeholder="Search symbol" value={search} onChange={event => setSearch(event.target.value)} />
        <div className="metric-row"><button className="btn" aria-pressed={direction === 'BUY'} onClick={() => setDirection('BUY')}>Bullish</button><button className="btn" aria-pressed={direction === 'SELL'} onClick={() => setDirection('SELL')}>Bearish</button></div>
        <p className="small">{sideRows.length} {direction === 'BUY' ? 'bullish' : 'bearish'} results</p>
        {!matchesSettings && rows.length > 0 && <p className="warn-text small">Previous scan context: {scan.data?.options?.interval} / {scan.data?.options?.mode}. Re-scan before using these scores.</p>}
        <div className="research-watchlist">{sideRows.slice(0, 100).map(entry => <button className={`research-symbol ${symbol === entry.symbol ? 'selected' : ''}`} key={entry.symbol} onClick={() => setSymbol(entry.symbol)}><strong>{entry.symbol}</strong><span>{num(entry.score, 1)}</span><small>Rules {entry.rule.action} · ML {entry.ml.action ?? 'Unavailable'}{entry.stale ? ' · STALE' : ''}</small></button>)}</div>
        <details><summary>Browse configured universe ({availableSymbols.length})</summary>{availableSymbols.slice(0, 100).map(value => <button className="btn" key={value} onClick={() => setSymbol(value)}>{value}</button>)}<p className="small">Showing up to 100 matches; search to narrow. Scans still process the full list.</p></details>
        <ErrorNotice error={universe.error} />
      </div></aside>
      <main className="main">
        <ResearchWorkflow stage={stage} onStageChange={setStage} onPaper={() => setPage('paper')} symbol={symbol} interval={interval} mode={mode} scanStatus={scan.data?.status ?? 'Loading'} planStatus={planStatus} />
        <Panel title="Shared research settings" subtitle="These settings coordinate the scanner, selected-stock analysis, validation target and paper workspace. Only an explicit scan refreshes rankings and sizing.">
          <form className="research-settings" onSubmit={submit}>
            <label>Sizing capital (INR)<input type="number" required min="1" step="any" value={capital} onChange={event => setCapital(event.target.value)} /></label>
            <label>Candle interval<select aria-label="Candle interval" value={interval} onChange={event => setInterval(event.target.value)}><option value="minute">1 minute</option><option value="5minute">5 minutes</option><option value="15minute">15 minutes</option><option value="day">Daily</option></select></label>
            <label>Decision mode<select aria-label="Decision mode" value={mode} onChange={event => setMode(event.target.value)}><option value="rule_ml_combined">Rules + ML hybrid</option><option value="rule_only">Rules only</option><option value="ml_only">ML only</option></select></label>
            <label>Risk per trade (%)<input type="number" min="0.01" max="2" step="0.01" value={risk} onChange={event => setRisk(event.target.value)} required /></label>
            <label>Target reward/risk<input type="number" min="1" max="5" step="0.1" value={reward} onChange={event => setReward(event.target.value)} required /></label>
            <label className="checkbox-field"><input type="checkbox" checked={requireValidated} onChange={event => setRequireValidated(event.target.checked)} /> Require qualified outcome evidence</label>
            <button className="btn btn-primary" disabled={running || start.isPending}>Scan entire watchlist</button>
            {running && <button className="btn" type="button" onClick={() => cancel.mutate()} disabled={cancel.isPending}>Cancel scan</button>}
          </form>
          <p className={requireValidated ? 'small muted' : 'warn-text'}>{requireValidated
            ? 'Evidence gate is ON: missing, expired or failed validation produces no eligible plan. Use Rules only with qualified rule evidence, or ML only after outcome-model promotion. Hybrid remains exploratory.'
            : 'Exploratory mode: plans may have no independent evidence. Rankings are research hypotheses, not predicted winners.'}</p>
          {mode === 'ml_only' && (interval !== 'minute' || Number(reward) !== 2) && <p className="warn-text">Promoted outcome models require 1-minute candles and a 2:1 target. Current settings do not match that policy.</p>}
          {dependenciesChanged && <p className="warn-text" role="status">{dependencyNotice}</p>}
          {!matchesSettings && scan.data?.options && <p role="alert" className="warn-text">Settings or evidence differ from the last scan. Run a new scan before using its rankings or sizing.</p>}
          <ErrorNotice error={start.error} />
          {running && <p role="status">Universe scan running: {scan.data?.processed}/{scan.data?.total} checked. You can inspect or validate without cancelling it.</p>}
        </Panel>
        <div id="workspace-find" className="workspace-section" hidden={stage !== 'find'}>
        <Panel title="Opportunity Finder" subtitle="Scan every configured stock using one completed candle frame for rules, ML and price levels. Research only; no broker orders.">
          <ErrorNotice error={scan.error} /><ErrorNotice error={start.error} /><ErrorNotice error={cancel.error} />
          <p role="status">{scan.data?.status ?? 'Loading'} · {scan.data?.processed ?? 0}/{scan.data?.total ?? 0} checked {running ? `· ${scan.data?.current_symbol ?? ''}` : ''}</p>
          {running && <progress max={scan.data?.total || 1} value={scan.data?.processed || 0} />}
          <div className="metric-row"><Metric label="Evaluated" value={scan.data?.evaluated ?? '—'} /><Metric label="Stale" value={scan.data?.stale ?? '—'} /><Metric label="Eligible plans" value={scan.data?.eligible ?? '—'} /><Metric label="Skipped / errors" value={`${scan.data?.skipped.length ?? 0} / ${scan.data?.errors.length ?? 0}`} /></div>
          <p className="muted small">Full scans run in the background, not on page refresh. Lower-quality/missing data is counted, not given a fake score. An incomplete or cancelled scan is not a full-universe ranking.</p>
          <p className="muted small">Snapshot: {scan.data?.completed_at ?? 'Not completed'} · ML: {scan.data?.model_status ?? 'Not scanned'} · Capital used: {inr(scan.data?.effective_capital)}</p>
          {scan.data?.model_warning && <p className="warn-text small">{scan.data.model_warning}</p>}
          <details><summary>Scan skips and errors</summary>{[...(scan.data?.errors ?? []), ...(scan.data?.skipped ?? [])].map((item, index) => <p className="small" key={index}>{item.symbol}: {item.reason}</p>)}</details>
        </Panel>
        <Panel title="Top Opportunity" subtitle="Highest-ranked eligible bullish and bearish plans from the full scan. A stale or risk-blocked row is not a buy/sell recommendation.">
          <div className="two-col">{(['BUY', 'SELL'] as const).map(action => {
            const picks = rows.filter(item => item.action === action && item.plan && !item.plan.blocked && item.plan.quantity > 0 && item.expires_at && Date.parse(item.expires_at) > clockNow)
            return <div key={action}><h3>{action === 'BUY' ? 'Bullish' : 'Bearish'}</h3>{matchesSettings && scan.data?.status === 'completed' && picks[0] ? <Pick row={picks[0]} onSelect={inspect} /> : <p>No eligible {action === 'BUY' ? 'bullish' : 'bearish'} pick from a completed scan with these settings.</p>}</div>
          })}</div>
          <p className="muted small">The watchlist retains historical/stale candidates for inspection. News sentiment modifies the score only when its analysis predates the candle and is recent.</p>
          <button className="btn" onClick={() => inspect()}>Inspect {symbol}</button>
        </Panel>
        <Panel title="Position Monitor / Paper Book" subtitle="Persistent paper book; entry-cost accounting, before fees and slippage."><ErrorNotice error={book.error} /><div className="metric-row"><Metric label="Book equity" value={inr(book.data?.total_value)} /><Metric label="Available cash" value={inr(book.data?.cash)} /><Metric label="Open positions" value={book.data?.open_positions ?? '—'} /><Metric label="Realized gross P&L" value={inr(book.data?.total_pnl)} /></div><button className="btn" onClick={() => setPage('paper')}>Manage paper positions</button></Panel>
        </div>
        <div id="workspace-inspect" className="workspace-section" hidden={stage !== 'inspect'}>
        <Panel title={`${symbol} — Price, Indicators & Patterns`} subtitle="Plotly candlesticks, volume, RSI, ADX and moving averages. Completed stored candles, not guaranteed live prices.">
          <ErrorNotice error={detail.error} />{detail.isLoading && <p>Loading stored chart…</p>}
          {detail.data && stage === 'inspect' && page === 'research' && <Suspense fallback={<p>Loading Plotly…</p>}><PriceChart data={detail.data.chart} /></Suspense>}
        </Panel>
        <Panel title="Hybrid Decision Maker" subtitle="Rules and ML evaluate the same candle. Conflicting directional signals produce HOLD; an abstention reduces conviction.">
          {row ? <><div className="metric-row"><Metric label="Selected mode decision" value={row.action} /><Metric label="Rules" value={row.rule.action} hint={pct(row.rule.confidence)} /><Metric label="ML" value={row.ml.action ?? 'Unavailable'} hint={`${row.ml.status} · ${pct(row.ml.confidence)}`} /><Metric label="Score" value={row.score} /></div><p>{row.reasoning}</p><p>Candle {row.timestamp} · {row.interval} · {row.age_minutes} minutes old {row.stale ? '— STALE: research only' : ''}</p><p>Patterns: {row.patterns.join(', ') || 'None'} · Bias: {row.pattern_bias} · RSI {num(row.rsi, 1)} · ADX {num(row.adx, 1)}</p><p className="small muted">Confidence and scores are model/rule outputs, not a probability of profit.</p></> : <p>Select a symbol with sufficient stored history.</p>}
        </Panel>
        <Panel title="Stop Loss, Target & Position Sizing" subtitle="ATR × 1.5 stop; selectable reward/risk target. Shared basket reserves, cash, 10% single-name notional, portfolio risk/exposure and position-count caps.">
          {matchesSettings && selected && <p>Outcome evidence: {selected.validation?.status ?? 'Unvalidated'} · Historical win estimate: {pct(selected.validation?.win_probability ?? null)}<br />{selected.validation?.reason ?? 'No independent evidence for this signal'}</p>}
          {plan ? <><div className="metric-row"><Metric label="Entry" value={inr(plan.entry_price)} /><Metric label="Stop loss" value={inr(plan.stop_loss)} /><Metric label="Target" value={inr(plan.take_profit)} /><Metric label="Shares" value={plan.quantity} /><Metric label="Risk" value={inr(plan.risk_amount)} /><Metric label="Position value" value={inr(plan.position_value)} /></div><p>Reward/risk {plan.reward_risk}:1 · Room to nearest opposing swing: {num(plan.room_reward_risk, 2)} R</p>{plan.blocked && <p className="warn-text">Blocked: {plan.block_reason}</p>}<p className="small muted">Plan belongs to scan candle {selected?.timestamp}; it is not refreshed with the chart. Re-scan before acting. Fees, execution slippage and correlation constraints are not modeled here.</p></> : <p>Complete a scan with the current settings to calculate this symbol's plan.</p>}
          <div className="workspace-actions"><button className="btn" onClick={() => setStage('validate')}>Validate {symbol} strategy</button><button className="btn" onClick={() => setPage('paper')}>Open manual paper workspace</button></div>
        </Panel>
        </div>
        <div id="workspace-validate" className="workspace-section" hidden={stage !== 'validate'}>
        <Panel title={`Validation workspace: ${symbol}`} subtitle={`Shared context: ${interval} candles · ${reward}:1 target. These checks have different scopes; passing one does not pass the others.`}>
          <ol className="validation-path">
            <li><strong>Walk-forward:</strong> compare tuned rules with baseline rules. Experimental parameters are not applied to the scanner.</li>
            <li><strong>Rule outcomes:</strong> validate the scanner's fixed stop/target policy, then use Rules only and a fresh scan on later candles.</li>
            <li><strong>ML outcomes:</strong> inspect separate BUY/SELL models. Historical and forward-paper gates must pass before promotion; use ML only afterward.</li>
          </ol>
          <p className="small">Last walk-forward run for {symbol} / {interval}: {optimizerActivity[`${symbol}:${interval}`] ?? 'Not run in this session'}.</p>
          <p className="small">Last rule validation for {symbol} / {interval} / {reward}:1: {validationActivity[`${symbol}:${interval}:${Number(reward)}`] ?? 'Not run in this session'}.</p>
          <p className="warn-text small">Hybrid remains exploratory. Changing views never trains, validates, promotes or places a trade automatically.</p>
          <button className="btn" onClick={() => { setMode('rule_only'); setStage('find') }}>Use Rules only and review scan settings</button>
        </Panel>
        <WalkForwardPanel symbol={symbol} interval={interval} onComplete={context => setOptimizerActivity(previous => ({ ...previous, [`${context.symbol}:${context.interval}`]: context.status }))} />
        <OutcomeValidationPanel symbol={symbol} interval={interval} rewardRisk={Number(reward)} onComplete={context => {
          setValidationActivity(previous => ({ ...previous, [`${context.symbol}:${context.interval}:${context.rewardRisk}`]: context.status }))
          invalidatePlans(`Rule outcome evidence updated for ${context.symbol}. Review the result, wait for later candles and re-scan; qualification is not assumed.`)
        }} />
        <OutcomeModelsPanel symbol={symbol} interval={interval} mode={mode} rewardRisk={Number(reward)} onUseModel={() => { setMode('ml_only'); setInterval('minute'); setReward('2'); setStage('find') }} />
        </div>
        <div className="workspace-section" hidden={stage !== 'inspect'}>
        <Panel title="Recent News + LLM Sentiment" subtitle="Explicit refresh only. Ticker search may contain irrelevant headlines; inspect sources. LLM output is supporting evidence, not trade authorization.">
          <label><input type="checkbox" checked={useLlm} onChange={event => setUseLlm(event.target.checked)} /> Analyze with configured LLM (may incur provider charges)</label>
          <p className="small muted">Configured provider: {universe.data?.llm_provider ?? 'Unknown'}; mock mode never supplies a real sentiment score.</p>
          <button className="btn" disabled={refreshNews.isPending || !symbol} onClick={() => refreshNews.mutate({ symbol, useLlm })}>{refreshNews.isPending ? 'Refreshing news…' : 'Refresh selected symbol news'}</button>
          <ErrorNotice error={news.error} /><ErrorNotice error={refreshNews.variables?.symbol === symbol ? refreshNews.error : null} />
          {refreshNews.isPending && <p role="status">Refreshing news for {refreshNews.variables.symbol}.</p>}
          <p>Status: {news.data?.status ?? 'Not loaded'} · {news.data?.refreshed_at ?? 'No cached refresh'}</p>
          {news.data?.analysis && <p>Sentiment {news.data.analysis.score.toFixed(2)} · confidence {pct(news.data.analysis.confidence)} · {news.data.analysis.model}<br />{news.data.analysis.rationale}</p>}
          {news.data?.articles.map(article => <article key={article.url + article.headline}><p>{/^https?:\/\//i.test(article.url) ? <a href={article.url} target="_blank" rel="noreferrer">{article.headline}</a> : article.headline}</p><small>{article.source} · {article.published_at}</small></article>)}
          {news.data?.articles.length === 0 && <p>No cached headlines for this symbol.</p>}
        </Panel>
        </div>
      </main>
    </div>
  </div>
}
