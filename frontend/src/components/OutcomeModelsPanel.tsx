import { useQuery } from '@tanstack/react-query'
import { apiGet } from '../api/client'
import { Panel } from './ui'

interface Gate {
  trades: number; sessions: number; win_rate: number | null; win_lower_95: number | null
  mean_net_return_pct: number | null; passed: boolean
}
interface Candidate {
  candidate_id: string; status: string; symbols: string[] | null
  test?: Record<string, Gate>; error?: string
}
interface Status {
  active: { candidate_id: string; expires_at: string } | null; error: string | null
  candidates: Candidate[]; required: string
}
const pct = (v: number | null) => v == null ? 'Unavailable' : `${(100 * v).toFixed(1)}%`

export default function OutcomeModelsPanel({ symbol, interval, mode, rewardRisk, onUseModel }: {
  symbol: string; interval: string; mode: string; rewardRisk: number; onUseModel: () => void
}) {
  const status = useQuery({ queryKey: ['outcome-models'], queryFn: () => apiGet<Status>('/api/v1/research/outcome-models'), refetchInterval: 30000 })
  const active = status.data?.active
  const activeCandidate = status.data?.candidates.find(candidate => candidate.candidate_id === active?.candidate_id)
  const inScope = activeCandidate?.symbols?.includes(symbol)
  const policyMatches = interval === 'minute' && mode === 'ml_only' && rewardRisk === 2
  const usable = Boolean(active && !status.data?.error && Date.parse(active.expires_at) > Date.now())
  return <Panel title="BUY / SELL Outcome Models" subtitle="Separate after-cost outcome models; training does not automatically change the dashboard model.">
    {status.isLoading && <p>Loading outcome-model status…</p>}
    {status.error && <p role="alert" className="error">{status.error.message}</p>}
    {status.data && <>
      <p>{status.data.active ? `Promoted candidate: ${status.data.active.candidate_id} · expires ${status.data.active.expires_at}` : 'No promoted outcome model. The legacy model remains exploratory unless an invalid active pointer is blocking it.'}</p>
      {status.data.error && <p className="warn-text">{status.data.error}</p>}
      <p>Selected context: {symbol} / {interval} / {rewardRisk}:1 / {mode}.</p>
      {usable && <p className={inScope && policyMatches ? 'muted' : 'warn-text'}>
        {inScope === true ? `${symbol} is in the promoted model's training scope.` : inScope === false ? `${symbol} is outside the promoted model's training scope; no outcome prediction is available.` : 'The promoted model’s symbol scope is not present in the recent candidate list; verify the artifact scope.'}
        {!policyMatches && ' Shared settings do not match the promoted minute / ML only / 2:1 policy.'}
      </p>}
      <button className="btn" disabled={!usable} onClick={onUseModel}>Use promoted ML policy and review scan settings</button>
      <p className="small muted">This only changes shared UI settings. It never promotes a model, broadens its training scope or starts a scan.</p>
      <p className="small muted">{status.data.required}</p>
      {status.data.candidates.map(candidate => <details key={candidate.candidate_id}>
        <summary>{candidate.candidate_id.slice(0, 12)} · {candidate.status}</summary>
        <p>Training scope: {candidate.symbols?.join(', ') ?? 'Unavailable'}</p>
        {candidate.error && <p className="error">{candidate.error}</p>}
        {Object.entries(candidate.test ?? {}).map(([side, gate]) => <p key={side}>
          {side}: {gate.passed ? 'historical gate passed' : 'historical gate failed'} · {gate.trades} trades / {gate.sessions} sessions · wins {pct(gate.win_rate)} · Wilson lower bound {pct(gate.win_lower_95)} · mean net return {gate.mean_net_return_pct == null ? 'Unavailable' : `${gate.mean_net_return_pct.toFixed(3)}%`}
        </p>)}
      </details>)}
      {!status.data.candidates.length && <p>No outcome candidates trained yet.</p>}
    </>}
    <p className="small muted">Use the documented outcome CLI to train, observe forward paper and request promotion. No force-promotion option. A promoted candidate applies only to ML only mode, trained symbols, minute bars and the fixed 2:1 target policy. Entry is the next fully future minute open after one bar of decision latency. The entire watchlist is still scanned, but symbols outside the trained scope have no outcome-model prediction.</p>
  </Panel>
}
