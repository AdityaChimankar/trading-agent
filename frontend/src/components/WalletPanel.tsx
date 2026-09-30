import { useWallet } from '../api/queries'
import { inr, num, signedInr } from '../lib/format'
import { Panel, Metric, StateMessage } from './ui'

export default function WalletPanel() {
  const { data, isLoading, error } = useWallet()

  const f = data?.fixed ?? null
  const t = data?.today ?? null
  const positions = data?.open_positions ?? []
  const prev = data?.prev_days_closed_pnl ?? {}

  return (
    <Panel title="Wallet" subtitle="Paper book — nothing here places a real order. Live mark-to-market, refreshes every 15s." className="wallet-panel">
      {error && (
        <p className="error small">
          Wallet load failed: {error instanceof Error ? error.message : 'unknown error'}
        </p>
      )}

      <StateMessage
        loading={isLoading}
        error={error}
        empty="No wallet activity yet. Add a deposit, or record and close a trade to seed the book."
        isEmpty={f !== null && f.capital === 500000 && f.total_deposits === 0 && f.realized_pnl_total === 0}
      />

      {f && (
        <div className="wallet-fixed">
          <div className="metric-row compact">
            <Metric label="Paper capital" value={inr(f.capital)} hint={f.total_deposits > 0 ? `${inr(f.total_deposits)} deposited` : undefined} />
            <Metric
              label="Book balance"
              value={inr(f.book_balance)}
              tone={f.book_balance >= f.capital ? 'pos' : 'neg'}
              hint="capital + deposits - withdrawals + realized P&L"
            />
            <Metric
              label="Realized P&L"
              value={signedInr(f.realized_pnl_total)}
              tone={f.realized_pnl_total >= 0 ? 'pos' : 'neg'}
              hint="closed trades only"
            />
            <Metric label="Open positions" value={num(positions.length, 0)} />
          </div>
        </div>
      )}

      {t && positions.length > 0 && (
        <div className="metric-row compact">
          <Metric
            label="Today P&L"
            value={signedInr(t.total_today_pnl)}
            tone={t.total_today_pnl >= 0 ? 'pos' : 'neg'}
            hint={`open ${inr(t.open_pnl)} + closed ${inr(t.closed_pnl)}`}
          />
          <Metric
            label="Open P&L"
            value={signedInr(t.open_pnl)}
            tone={t.open_pnl >= 0 ? 'pos' : 'neg'}
          />
          <Metric
            label="Total open P&L"
            value={signedInr(positions.reduce((s, p) => s + p.current_pnl, 0))}
            tone={positions.reduce((s, p) => s + p.current_pnl, 0) >= 0 ? 'pos' : 'neg'}
            hint={`${positions.length} open`}
          />
          <Metric
            label="Used margin"
            value={inr(positions.reduce((s, p) => s + p.current_value, 0))}
            hint={f ? `${num(((positions.reduce((s, p) => s + p.current_value, 0) / f.book_balance) * 100), 1)}% of book` : undefined}
          />
        </div>
      )}

      {positions.length > 0 && (
        <>
          {prev && Object.keys(prev).length > 0 && (
            <div className="metric-row compact">
              {Object.keys(prev)
                .map((d) => ({ d, pnl: prev[d] }))
                .sort((a, b) => a.d.localeCompare(b.d))
                .map((row) => (
                  <Metric
                    key={row.d}
                    label={row.d}
                    value={signedInr(row.pnl)}
                    tone={row.pnl >= 0 ? 'pos' : 'neg'}
                  />
                ))}
            </div>
          )}

          <div className="position-list wallet-list">
            {positions.map((p) => (
              <li key={p.position_id} className="position">
                <div className="position-head">
                  <span className="position-symbol">
                    {p.symbol} <span className="muted small">({p.action})</span>
                  </span>
                  <span className={p.current_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {p.current_pnl >= 0 ? '🟢' : '🔴'} {signedInr(p.current_pnl)} ({num(p.current_pnl_pct, 2)}%)
                  </span>
                </div>

                <div className="position-grid small">
                  <div>
                    <span className="muted">Entry</span> {inr(p.entry_price)} × {num(p.position_size, 0)}
                  </div>
                  <div>
                    <span className="muted">Now</span> {inr(p.current_price)} → {inr(p.current_value)}
                  </div>

                  {p.stop_scenario_pnl !== null && (
                    <div>
                      <span className="muted">Stop scenario</span>
                      {p.stop_scenario_pnl >= 0 ? '🟢' : '🔴'}
                      {signedInr(p.stop_scenario_pnl)} ({num(
                        p.current_pnl_pct === 0 ? 0 : p.stop_scenario_pnl / (p.entry_price * p.position_size) * 100,
                        2,
                      )})%
                    </div>
                  )}
                  {p.target_scenario_pnl !== null && (
                    <div>
                      <span className="muted">Target scenario</span>
                      {p.target_scenario_pnl >= 0 ? '🟢' : '🔴'}
                      {signedInr(p.target_scenario_pnl)} ({num(
                        p.current_pnl_pct === 0 ? 0 : p.target_scenario_pnl / (p.entry_price * p.position_size) * 100,
                        2,
                      )})%
                    </div>
                  )}
                </div>

                <p className="muted small">
                  At risk {inr(p.at_risk_rupees)} · {p.stop_scenario_pnl !== null && p.target_scenario_pnl !== null
                    ? `Stop/profit gap: ${inr(p.target_scenario_pnl - p.stop_scenario_pnl)}`
                    : ''}
                </p>
              </li>
            ))}
          </div>
        </>
      )}

      {positions.length === 0 && f && f.total_deposits === 0 && f.realized_pnl_total === 0 && (
        <p className="muted small">
          No positions and no paper capital yet. Click Record a trade below to
          size and record one, or start with a deposit first.
        </p>
      )}

      {f && f.capital === 500000 && f.total_deposits === 0 && f.realized_pnl_total === 0 && (
        <p className="muted small">
          Wallet not initialized. Add paper capital via a deposit first (or
          start with a trade — closing it will seed the book from zero).
        </p>
      )}
    </Panel>
  )
}
