import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiPost } from '../api/client'
import { Metric, Panel } from './ui'

interface Summary {
  trades: number; win_rate: number | null; mean_net_return_pct: number | null
  win_rate_interval: number[] | null; status?: string
  brier?: number; baseline_brier?: number; mean_net_return_lower_95_pct?: number
}
interface Report {
  symbol: string; interval: string; status: string; expires_at: string; data_end: string
  policy: { reward_risk: number }; calibration: Summary; holdout: Summary
  sides: Record<string, Summary>; limitations: string
}
const percent = (x: number | null | undefined) => x == null ? 'Unavailable' : `${(100 * x).toFixed(1)}%`

export default function OutcomeValidationPanel({ symbol, interval, rewardRisk, onComplete }: {
  symbol: string; interval: string; rewardRisk: number
  onComplete?: (context: { symbol: string; interval: string; rewardRisk: number; status: string }) => void
}) {
  const cache = useQueryClient()
  const input = {
    symbol, interval, reward_risk: rewardRisk, cost_bps: 10, slippage_bps: 2, horizon: 24,
  }
  const saved = useQuery<Report>({ queryKey: ['outcome-validation-result', input], enabled: false, queryFn: async () => { throw new Error('Run validation explicitly') }, gcTime: Infinity })
  const run = useMutation({
    mutationFn: (request: typeof input) => apiPost<Report>('/api/v1/research/outcome-validation', request),
    onSuccess: (data, request) => {
      cache.setQueryData(['outcome-validation-result', request], data)
      onComplete?.({ symbol: request.symbol, interval: request.interval, rewardRisk: request.reward_risk, status: data.status })
    },
  })
  const report = saved.data
  const matches = report?.symbol === symbol && report.interval === interval && report.policy.reward_risk === rewardRisk
  return <Panel title="Trade Outcome Validation" subtitle="Independent rule-only calibration and holdout. A model confidence score is not a chance of winning.">
    <p>Next-open entries, 1.5 ATR stops, selected reward/risk, up to 24 bars; 10 bps round-trip costs plus 2 bps adverse slippage per fill. Ambiguous bars hit the stop first.</p>
    <p className="small muted">Uses up to 20,000 stored completed candles. Requires 100 calibration and 100 holdout trades per direction, both calibration classes, at least 10 holdout sessions, positive lower-bound expectancy and a Brier score better than the constant baseline. Most symbols may not qualify.</p>
    <button className="btn" disabled={run.isPending || interval === 'day' || !symbol || !Number.isFinite(rewardRisk)}
      onClick={() => run.mutate(input)}>{run.isPending ? 'Validating stored outcomes…' : `Validate ${symbol} outcomes`}</button>
    {interval === 'day' && <p>Intraday intervals only.</p>}
    {run.error && run.variables?.symbol === symbol && run.variables?.interval === interval && run.variables?.reward_risk === rewardRisk && <p role="alert" className="error">{run.error.message}</p>}
    {run.isPending && <p role="status">Validating {run.variables.symbol} / {run.variables.interval} / {run.variables.reward_risk}:1. The request keeps its original settings.</p>}
    {!report && !run.isPending && <p className="muted">No outcome validation for this symbol, interval and target in this browser session.</p>}
    {report && !matches && <p>Settings changed. Run validation again for the selected symbol and policy.</p>}
    {matches && report && <>
      <p role="status">{report.status} · Data through {report.data_end} · Evidence expires {report.expires_at}</p>
      <div className="metric-row"><Metric label="Calibration trades" value={report.calibration.trades} /><Metric label="Holdout trades" value={report.holdout.trades} /><Metric label="Holdout win rate" value={percent(report.holdout.win_rate)} /></div>
      {Object.entries(report.sides).map(([side, item]) => <article key={side}>
        <h3>{side}: {item.status}</h3>
        <p>{item.trades} holdout trades · Win rate {percent(item.win_rate)}
          {item.win_rate_interval && ` (95% historical interval ${percent(item.win_rate_interval[0])}–${percent(item.win_rate_interval[1])})`}</p>
        <p>Mean net return: {item.mean_net_return_pct == null ? 'Unavailable' : `${item.mean_net_return_pct.toFixed(3)}%`}
          {' · '}95% lower expectancy: {item.mean_net_return_lower_95_pct == null ? 'Unavailable' : `${item.mean_net_return_lower_95_pct.toFixed(3)}%`}</p>
        {item.brier != null && <p>Brier {item.brier.toFixed(4)} / constant baseline {item.baseline_brier?.toFixed(4)} (lower is better)</p>}
      </article>)}
      <p className="small muted">{report.limitations}</p>
    </>}
    <p className="warn-text">Legacy ML/hybrid training dates are unknown, so hybrid win probabilities remain unvalidated. Qualified evidence applies only to later candles in Rules only mode with the same policy. Re-scan after new candles arrive. This does not place orders or retrain the active model.</p>
  </Panel>
}
