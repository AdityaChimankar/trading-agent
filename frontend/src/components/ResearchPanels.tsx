import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { apiGet, apiPost } from '../api/client'
import { Panel } from './ui'
import { inr } from '../lib/format'

interface Opportunity {
  symbol: string; timestamp: string; score: number; rank: number
  rule_signal: string; rule_confidence: number; ml_signal: string | null
  ml_confidence: number; llm_signal: string | null; llm_confidence: number
  risk_reward: number; price: number
}
interface Decision {
  symbol: string; interval: string; timestamp: string; mode: string; limitations: string[]
  decision: {
    action: string; confidence: number; reasoning: string
    rule_decision: { action: string; confidence: number; reasoning: string }
    ml_decision: { action: string; confidence: number } | null
    llm_decision: { action: string; confidence: number } | null
  }
}
const percent = (value: number) => `${(value * 100).toFixed(1)}%`

export default function ResearchPanels({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [symbols, setSymbols] = useState('RELIANCE,TCS')
  const [interval, setInterval] = useState('5minute')
  const [minScore, setMinScore] = useState('0.3')
  const [symbol, setSymbol] = useState('RELIANCE')
  const scan = useMutation({
    mutationFn: (params: URLSearchParams) => apiGet<Opportunity[]>(`/api/v1/opportunities/?${params}`),
  })
  const hybrid = useMutation({
    mutationFn: (input: { symbol: string; interval: string }) => apiPost<Decision>(
      `/api/v1/opportunities/${encodeURIComponent(input.symbol)}/decision?interval=${input.interval}`),
  })
  function find(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    scan.reset()
    scan.mutate(new URLSearchParams({ symbols: symbols.trim().toUpperCase(), interval, min_score: minScore, max_results: '20' }))
  }
  function select(value: string) { setSymbol(value); hybrid.reset(); onSelect(value) }
  const top = scan.data?.[0]
  return <>
    <Panel title="Opportunity Finder" subtitle="On-demand ranking from stored candles. No broker download, AI call, or order is triggered. Rankings currently use rule signals, not ML/LLM inference.">
      <form onSubmit={find} className="metric-row">
        <label>Scan symbols (comma-separated; blank scans all stored symbols)
          <input value={symbols} onChange={event => { setSymbols(event.target.value); scan.reset() }} />
        </label>
        <label>Research interval <select value={interval} onChange={event => { setInterval(event.target.value); scan.reset(); hybrid.reset() }}>
          <option value="minute">1 minute</option><option value="5minute">5 minutes</option>
          <option value="15minute">15 minutes</option><option value="day">Daily</option>
        </select></label>
        <label>Minimum score (0–1) <input type="number" min="0" max="1" step="0.01" required value={minScore} onChange={event => { setMinScore(event.target.value); scan.reset() }} /></label>
        <button disabled={scan.isPending} type="submit">{scan.isPending ? 'Scanning…' : 'Find opportunities'}</button>
      </form>
      <p className="muted small">Use the interval you actually ingested. Large scans can take time. Results are snapshots, not live prices, and do not exclude existing positions or apply portfolio approval.</p>
      {scan.error && <p role="alert">{scan.error.message}</p>}
      {scan.isSuccess && scan.data.length === 0 && <p>No qualifying opportunities. Check stored history, interval, volume/ADX filters, and minimum score.</p>}
      {Boolean(scan.data?.length) && <div style={{ overflowX: 'auto' }}><table>
        <thead><tr><th>Rank</th><th>Symbol</th><th>Rule</th><th>Score</th><th>Stored price</th><th>Candle time</th></tr></thead>
        <tbody>{scan.data?.map(row => <tr key={row.symbol}>
          <td>{row.rank}</td><td><button className="link" onClick={() => select(row.symbol)}>{row.symbol}</button></td>
          <td>{row.rule_signal}</td><td>{row.score.toFixed(3)}</td><td>{inr(row.price)}</td><td>{row.timestamp}</td>
        </tr>)}</tbody>
      </table></div>}
    </Panel>
    <Panel title="Top Opportunity" subtitle="Highest score in your last successful scan; a research ranking, not a trade recommendation.">
      {top ? <>
        <h3>{top.symbol} · {top.rule_signal}</h3>
        <p>Score {top.score.toFixed(3)} / 1 · Rule confidence {percent(top.rule_confidence)} · Stored price {inr(top.price)}</p>
        <p>ML: {top.ml_signal ?? 'Unavailable'} · LLM: {top.llm_signal ?? 'Unavailable'}</p>
        <p>Candle timestamp: {top.timestamp}. Confirm freshness before using this result.</p>
        <button onClick={() => select(top.symbol)}>Select {top.symbol} for decision preview</button>
      </> : <p>Run Opportunity Finder to display the top qualifying result.</p>}
    </Panel>
    <Panel title="Hybrid Decision Maker" subtitle="Experimental preview of the existing decision engine. Currently rule-only: ML and LLM are not connected to this API path. No positions are created.">
      <form onSubmit={event => { event.preventDefault(); hybrid.reset(); hybrid.mutate({ symbol: symbol.trim().toUpperCase(), interval }) }} className="metric-row">
        <label>Decision symbol <input required value={symbol} onChange={event => { setSymbol(event.target.value); hybrid.reset() }} /></label>
        <p>Interval: {interval} (set in Opportunity Finder)</p>
        <button type="submit" disabled={hybrid.isPending || !symbol.trim()}>{hybrid.isPending ? 'Evaluating…' : 'Preview hybrid decision'}</button>
      </form>
      {hybrid.error && <p role="alert">{hybrid.error.message}</p>}
      {hybrid.data && <>
        <h3>{hybrid.data.symbol}: {hybrid.data.decision.action} · Rule-only preview</h3>
        <p>Candle timestamp: {hybrid.data.timestamp} · Interval: {hybrid.data.interval}</p>
        <p>Voting share: {percent(hybrid.data.decision.confidence)} (not probability of profit)</p>
        <p>Rule: {hybrid.data.decision.rule_decision.action} · Confidence {percent(hybrid.data.decision.rule_decision.confidence)}</p>
        <p>{hybrid.data.decision.rule_decision.reasoning}</p>
        <p>ML: {hybrid.data.decision.ml_decision?.action ?? 'Unavailable'} · LLM: {hybrid.data.decision.llm_decision?.action ?? 'Unavailable'}</p>
        {hybrid.data.limitations.map(message => <p className="muted small" key={message}>{message}</p>)}
      </>}
    </Panel>
  </>
}
