import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiPost } from '../api/client'
import { Panel } from './ui'
import { num } from '../lib/format'

interface Summary {
  trades: number; win_rate: number | null; total_return_pct: number
  mean_return_pct: number | null; max_drawdown_pct: number
}
interface Result {
  symbol: string; interval: string; status: string; candles: number; required_candles: number
  data_start?: string; data_end?: string; evaluated_folds?: number
  train_candles?: number; test_candles?: number; round_trip_cost_bps?: number
  optimized?: Summary; default?: Summary; scope?: string; method?: string; limitations?: string
  folds: { test_from: string; test_to: string; status: string; params: Record<string, number> | null
    optimized?: Summary; default?: Summary }[]
}

export default function WalkForwardPanel({ symbol, interval, onComplete }: {
  symbol: string; interval: string
  onComplete?: (context: { symbol: string; interval: string; status: string }) => void
}) {
  const cache = useQueryClient()
  const [train, setTrain] = useState('3750')
  const [test, setTest] = useState('750')
  const [cost, setCost] = useState('5')
  const input = { symbol, interval, train_candles: Number(train), test_candles: Number(test), cost_bps: Number(cost) }
  const saved = useQuery<Result>({ queryKey: ['walk-forward-result', input], enabled: false, queryFn: async () => { throw new Error('Run evaluation explicitly') }, gcTime: Infinity })
  const run = useMutation({
    mutationFn: (request: typeof input) => apiPost<Result>('/api/v1/research/walk-forward', request),
    onSuccess: (data, request) => {
      cache.setQueryData(['walk-forward-result', request], data)
      onComplete?.({ symbol: request.symbol, interval: request.interval, status: data.status })
    },
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    run.mutate(input)
  }
  const result = saved.data
  const matches = !run.variables || (run.variables.symbol === symbol && run.variables.interval === interval && run.variables.train_candles === Number(train) && run.variables.test_candles === Number(test) && run.variables.cost_bps === Number(cost))
  return <Panel title="Walk-Forward Optimizer" subtitle="Tune rule thresholds on past candles, then evaluate on the next unseen window. Selected symbol only; never retrains ML, applies parameters or places orders.">
    <p>Selected: {symbol} / {interval}. Uses up to 20,000 completed stored candles.</p>
    <form className="metric-row" onSubmit={submit}>
      <label>Training candles<input type="number" min="250" max="10000" step="1" required value={train} onChange={event => setTrain(event.target.value)} /></label>
      <label>Test candles<input type="number" min="100" max="5000" step="1" required value={test} onChange={event => setTest(event.target.value)} /></label>
      <label>Round-trip cost (bps)<input type="number" min="0" max="100" step="0.1" required value={cost} onChange={event => setCost(event.target.value)} /></label>
      <button className="btn" disabled={run.isPending || !symbol}>{run.isPending ? 'Evaluating walk-forward…' : 'Run walk-forward evaluation'}</button>
    </form>
    <p className="muted small">27 combinations of pattern-confirmation RSI and ADX thresholds; at least 15 training trades. Outcomes stay inside their train/test window (12-bar purge). Next-bar open entries, fixed 12-bar exits, non-overlapping positions and identical costs for optimized and baseline rules.</p>
    {run.error && matches && <p role="alert" className="error">{run.error.message}</p>}
    {run.isPending && <p role="status">Evaluating {run.variables.symbol} / {run.variables.interval}. Changing selection does not retarget this job.</p>}
    {!result && !run.isPending && <p className="muted">No walk-forward result for these exact inputs in this browser session.</p>}
    {result && !run.isPending && <>
      <p role="status">Walk-forward: {result.status} — {result.symbol} / {result.interval}; {result.candles} candles available, {result.required_candles} needed.</p>
      {result.status === 'insufficient_history' && <p>Not enough stored candles. Download more history or choose smaller valid windows; no performance was fabricated.</p>}
      {result.optimized && result.default && <>
        <p>History {result.data_start} to {result.data_end}; {result.evaluated_folds}/{result.folds.length} folds evaluated. Train {result.train_candles}, test {result.test_candles}, assumed round-trip cost {result.round_trip_cost_bps} bps.</p>
        <div className="table-wrap"><table><thead><tr><th>Out-of-sample metric</th><th>Optimized rules</th><th>Default rules</th></tr></thead><tbody>
          <tr><td>Trades</td><td>{result.optimized.trades}</td><td>{result.default.trades}</td></tr>
          <tr><td>Win rate (%)</td><td>{num(result.optimized.win_rate, 1)}</td><td>{num(result.default.win_rate, 1)}</td></tr>
          <tr><td>Mean net trade return (%)</td><td>{num(result.optimized.mean_return_pct, 4)}</td><td>{num(result.default.mean_return_pct, 4)}</td></tr>
          <tr><td>Sum of trade returns (%) — not portfolio return</td><td>{num(result.optimized.total_return_pct, 4)}</td><td>{num(result.default.total_return_pct, 4)}</td></tr>
          <tr><td>Additive drawdown (%)</td><td>{num(result.optimized.max_drawdown_pct, 4)}</td><td>{num(result.default.max_drawdown_pct, 4)}</td></tr>
        </tbody></table></div>
        <details><summary>Fold windows and selected thresholds</summary>{result.folds.map((fold, index) => <p className="small" key={index}>Fold {index + 1}: {fold.test_from} to {fold.test_to} — {fold.status}; {fold.params ? JSON.stringify(fold.params) : 'No qualifying training configuration'}{fold.optimized && `; net return sum ${fold.optimized.total_return_pct}% vs baseline ${fold.default?.total_return_pct}%`}</p>)}</details>
        <p className="warn-text small">{result.limitations}</p>
      </>}
    </>}
    <p className="muted small">This evaluates rules only, not the installed hybrid model or LLM sentiment. Default 5 bps is an editable assumption, not a broker fee quote. Results are kept per symbol, interval and evaluation settings for this page session. No automatic parameter application or promotion: use Trade Outcome Validation to check the scanner's fixed stop/target policy separately.</p>
  </Panel>
}
