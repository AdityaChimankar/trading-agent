import { useMemo } from 'react'
import { useTransactions } from '../api/queries'
import type { TransactionRow } from '../api/types'
import { inr, num, shortTime, signedInr } from '../lib/format'
import { DataTable, Metric, Panel, StateMessage } from './ui'

const TYPE_LABEL: Record<TransactionRow['type'], string> = {
  deposit: 'Deposit',
  withdrawal: 'Withdrawal',
  realized_pnl: 'Realized P&L',
}

// Every figure here is read from the book's own two tables - wallet_transactions
// (money movement) and trades (closed positions). Nothing is recomputed in the
// browser, so the page can never disagree with the Wallet panel.
export default function TransactionsPage({
  onSelectSymbol,
}: {
  onSelectSymbol: (symbol: string) => void
}) {
  const { data, isLoading, error, refetch } = useTransactions()

  const s = data?.summary
  const ledger = data?.ledger ?? []
  const trades = data?.trades ?? []
  const daily = data?.daily ?? []
  const hasAnyBook = Boolean(s && (s.transaction_count > 0 || s.trade_count > 0))

  // Strategy status - rule-based signal has no measured edge
  const strategyStatus = useMemo(() => ({
    label: 'Strategy Status',
    value: 'Rule-based signal: no measured edge',
    tone: 'warn' as const,
    hint: 'Backtest shows no statistical edge. Consider ML enhancement or parameter optimization.',
  }), [])

  // Advanced intraday analytics
  const intradayMetrics = useMemo(() => {
    if (trades.length === 0) return null

    const sortedTrades = [...trades].sort((a, b) => new Date(a.exit_at).getTime() - new Date(b.exit_at).getTime())
    
    // Equity curve for drawdown calculation
    let peak = 0
    let maxDrawdown = 0
    let runningPnl = 0
    const equityCurve: number[] = []

    for (const t of sortedTrades) {
      runningPnl += t.realized_pnl_rupees
      equityCurve.push(runningPnl)
      if (runningPnl > peak) peak = runningPnl
      const dd = peak - runningPnl
      if (dd > maxDrawdown) maxDrawdown = dd
    }

    // Win/loss streaks
    let currentStreak = 0
    let maxWinStreak = 0
    let maxLossStreak = 0
    let streakType: 'win' | 'loss' | null = null

    for (const t of sortedTrades) {
      const isWin = t.realized_pnl_rupees > 0
      if (streakType === null || (isWin && streakType === 'win') || (!isWin && streakType === 'loss')) {
        currentStreak++
        streakType = isWin ? 'win' : 'loss'
      } else {
        if (streakType === 'win') maxWinStreak = Math.max(maxWinStreak, currentStreak)
        else maxLossStreak = Math.max(maxLossStreak, currentStreak)
        currentStreak = 1
        streakType = isWin ? 'win' : 'loss'
      }
    }
    if (streakType === 'win') maxWinStreak = Math.max(maxWinStreak, currentStreak)
    else maxLossStreak = Math.max(maxLossStreak, currentStreak)

    // Risk/Reward ratio
    const wins = sortedTrades.filter(t => t.realized_pnl_rupees > 0)
    const losses = sortedTrades.filter(t => t.realized_pnl_rupees < 0)
    const avgWin = wins.length > 0 ? wins.reduce((a, b) => a + b.realized_pnl_rupees, 0) / wins.length : 0
    const avgLoss = losses.length > 0 ? Math.abs(losses.reduce((a, b) => a + b.realized_pnl_rupees, 0) / losses.length) : 0
    const riskReward = avgLoss > 0 ? avgWin / avgLoss : 0

    // Average hold time (minutes)
    const holdTimes = sortedTrades.map(t => {
      const entry = new Date(t.entry_at).getTime()
      const exit = new Date(t.exit_at).getTime()
      return (exit - entry) / (1000 * 60)
    })
    const avgHoldTime = holdTimes.length > 0 ? holdTimes.reduce((a, b) => a + b, 0) / holdTimes.length : 0
    const medianHoldTime = holdTimes.length > 0 ? holdTimes.sort((a, b) => a - b)[Math.floor(holdTimes.length / 2)] : 0

    // Hourly P&L breakdown (intraday)
    const hourlyPnl: Record<number, { pnl: number; trades: number; wins: number }> = {}
    for (const t of sortedTrades) {
      const hour = new Date(t.exit_at).getHours()
      if (!hourlyPnl[hour]) hourlyPnl[hour] = { pnl: 0, trades: 0, wins: 0 }
      hourlyPnl[hour].pnl += t.realized_pnl_rupees
      hourlyPnl[hour].trades++
      if (t.realized_pnl_rupees > 0) hourlyPnl[hour].wins++
    }

    // Symbol performance
    const symbolStats: Record<string, { pnl: number; trades: number; wins: number; volume: number }> = {}
    for (const t of sortedTrades) {
      if (!symbolStats[t.symbol]) symbolStats[t.symbol] = { pnl: 0, trades: 0, wins: 0, volume: 0 }
      symbolStats[t.symbol].pnl += t.realized_pnl_rupees
      symbolStats[t.symbol].trades++
      symbolStats[t.symbol].volume += t.position_size * t.entry_price
      if (t.realized_pnl_rupees > 0) symbolStats[t.symbol].wins++
    }

    // Exit reason breakdown
    const exitReasons: Record<string, { count: number; pnl: number }> = {}
    for (const t of sortedTrades) {
      const reason = t.exit_reason || 'Unknown'
      if (!exitReasons[reason]) exitReasons[reason] = { count: 0, pnl: 0 }
      exitReasons[reason].count++
      exitReasons[reason].pnl += t.realized_pnl_rupees
    }

    // Consecutive days analysis
    const dailyPnlMap: Record<string, number> = {}
    for (const t of sortedTrades) {
      const date = t.exit_at.split('T')[0]
      if (!dailyPnlMap[date]) dailyPnlMap[date] = 0
      dailyPnlMap[date] += t.realized_pnl_rupees
    }
    const dailyPnls = Object.values(dailyPnlMap).sort((a, b) => b - a)
    const bestDay = dailyPnls[0] || 0
    const worstDay = dailyPnls[dailyPnls.length - 1] || 0
    const profitableDays = dailyPnls.filter(p => p > 0).length
    const totalDays = dailyPnls.length

    return {
      maxDrawdown,
      maxWinStreak,
      maxLossStreak,
      currentStreak: streakType === 'win' ? currentStreak : -currentStreak,
      riskReward,
      avgWin,
      avgLoss,
      avgHoldTime,
      medianHoldTime,
      hourlyPnl,
      symbolStats,
      exitReasons,
      bestDay,
      worstDay,
      profitableDays,
      totalDays,
      equityCurve,
    }
  }, [trades])

  // Format helpers
  const fmtMin = (m: number) => m >= 60 ? `${(m / 60).toFixed(1)}h` : `${m.toFixed(0)}m`
  const fmtHour = (h: number) => `${h.toString().padStart(2, '0')}:00`

  return (
    <>
      {/* Strategy Status Banner */}
      <Panel
        title="⚠️ Strategy Status"
        subtitle="Current status: the rule-based signal has no measured edge"
        className="strategy-status-panel"
      >
        <div className="metric-row">
          <Metric
            label={strategyStatus.label}
            value={strategyStatus.value}
            tone="neg"
            hint={strategyStatus.hint}
          />
          <Metric
            label="Data Freshness"
            value={data?.summary?.last_at ? shortTime(data.summary.last_at) : '—'}
            hint="Last transaction timestamp"
          />
          <div className="metric">
            <div className="metric-label">Auto-refresh</div>
            <div className="metric-value">
              <button 
                type="button" 
                className="link" 
                onClick={() => refetch()}
                disabled={isLoading}
              >
                {isLoading ? '⟳' : '🔄 Refresh'}
              </button>
            </div>
            <div className="metric-hint">30s interval — click to refresh manually</div>
          </div>
        </div>
      </Panel>

      <Panel
        title="🧾 Transaction History"
        subtitle="The paper book's full activity log: every deposit, withdrawal and realized P&L in wallet_transactions, plus the closed trade behind each one. Read-only — use the Wallet panel on the Dashboard to deposit, withdraw or close a position."
      >
        <StateMessage
          loading={isLoading}
          error={error}
          empty="No transactions yet. Deposit paper capital, or record and close a trade from the Dashboard."
          isEmpty={!hasAnyBook}
        />

        {s && hasAnyBook && (
          <>
            <div className="metric-row">
              <Metric
                label="Net money moved"
                value={signedInr(s.net)}
                tone={s.net >= 0 ? 'pos' : 'neg'}
                hint="deposits − withdrawals + realized P&L"
              />
              <Metric label="Deposits" value={inr(s.deposits)} tone={s.deposits > 0 ? 'pos' : undefined} />
              <Metric
                label="Withdrawals"
                value={inr(s.withdrawals)}
                tone={s.withdrawals > 0 ? 'neg' : undefined}
              />
              <Metric
                label="Realized P&L"
                value={signedInr(s.realized_pnl)}
                tone={s.realized_pnl >= 0 ? 'pos' : 'neg'}
                hint="after slippage"
              />
            </div>

            <div className="metric-row">
              <Metric label="Trades closed" value={num(s.trade_count, 0)} />
              <Metric
                label="Win rate"
                value={s.win_rate_pct === null ? '—' : `${num(s.win_rate_pct, 1)}%`}
                tone={s.win_rate_pct !== null && s.win_rate_pct >= 50 ? 'pos' : 'neg'}
                hint={`${s.wins} win · ${s.losses} loss`}
              />
              <Metric
                label="Gross profit / loss"
                value={`${inr(s.gross_profit)} / ${inr(s.gross_loss)}`}
                hint="before slippage"
              />
              <Metric
                label="Slippage paid"
                value={inr(s.slippage_total)}
                tone={s.slippage_total > 0 ? 'neg' : undefined}
                hint="5 bps of position value, per close"
              />
            </div>

            {(s.best || s.worst) && (
              <div className="metric-row">
                {s.best && (
                  <Metric
                    label="Best trade"
                    value={signedInr(s.best.realized_pnl_rupees)}
                    tone="pos"
                    hint={`${s.best.symbol} · ${shortTime(s.best.exit_at)}`}
                  />
                )}
                {s.worst && (
                  <Metric
                    label="Worst trade"
                    value={signedInr(s.worst.realized_pnl_rupees)}
                    tone="neg"
                    hint={`${s.worst.symbol} · ${shortTime(s.worst.exit_at)}`}
                  />
                )}
              </div>
            )}

            <p className="muted small">
              {s.transaction_count} ledger row(s), oldest {shortTime(s.first_at)}, newest {shortTime(s.last_at)}.
            </p>
          </>
        )}
      </Panel>

      {/* Intraday Risk & Performance Analytics */}
      {intradayMetrics && (
        <Panel
          title="📊 Intraday Risk Analytics"
          subtitle="Advanced metrics computed from closed trades. Max drawdown, streaks, risk/reward, and hold-time analysis."
        >
          <div className="metric-row">
            <Metric
              label="Max Drawdown"
              value={inr(intradayMetrics.maxDrawdown)}
              tone="neg"
              hint="Peak-to-trough decline in equity curve"
            />
            <Metric
              label="Risk/Reward Ratio"
              value={intradayMetrics.riskReward.toFixed(2)}
              tone={intradayMetrics.riskReward >= 1.5 ? 'pos' : intradayMetrics.riskReward >= 1 ? 'muted' : 'neg'}
              hint={`Avg win: ${inr(intradayMetrics.avgWin)} · Avg loss: ${inr(intradayMetrics.avgLoss)}`}
            />
            <Metric
              label="Max Win Streak"
              value={num(intradayMetrics.maxWinStreak, 0)}
              tone="pos"
              hint="Consecutive winning trades"
            />
            <Metric
              label="Max Loss Streak"
              value={num(intradayMetrics.maxLossStreak, 0)}
              tone="neg"
              hint="Consecutive losing trades"
            />
          </div>

          <div className="metric-row">
            <Metric
              label="Current Streak"
              value={intradayMetrics.currentStreak > 0 ? `+${intradayMetrics.currentStreak} 🟢` : intradayMetrics.currentStreak < 0 ? `${intradayMetrics.currentStreak} 🔴` : '—'}
              tone={intradayMetrics.currentStreak > 0 ? 'pos' : intradayMetrics.currentStreak < 0 ? 'neg' : undefined}
              hint="Active consecutive wins/losses"
            />
            <Metric
              label="Avg Hold Time"
              value={fmtMin(intradayMetrics.avgHoldTime)}
              hint={`Median: ${fmtMin(intradayMetrics.medianHoldTime)}`}
            />
            <Metric
              label="Best Day"
              value={signedInr(intradayMetrics.bestDay)}
              tone="pos"
              hint={`Profitable days: ${intradayMetrics.profitableDays}/${intradayMetrics.totalDays}`}
            />
            <Metric
              label="Worst Day"
              value={signedInr(intradayMetrics.worstDay)}
              tone="neg"
              hint="Largest single-day loss"
            />
          </div>
        </Panel>
      )}

      {/* Hourly P&L Heatmap - Intraday Session Analysis */}
      {intradayMetrics && Object.keys(intradayMetrics.hourlyPnl).length > 0 && (
        <Panel
          title="🕐 Hourly P&L Heatmap (Intraday)"
          subtitle="P&L distribution by exit hour. Identify your edge hours and avoid toxic sessions."
        >
          <div className="hourly-heatmap">
            {[9, 10, 11, 12, 13, 14, 15].map(hour => {
              const data = intradayMetrics.hourlyPnl[hour]
              if (!data) return null
              const winRate = data.trades > 0 ? (data.wins / data.trades) * 100 : 0
              const intensity = Math.min(Math.abs(data.pnl) / 10000, 1)
              const isPositive = data.pnl >= 0
              return (
                <div key={hour} className="hour-cell" style={{ opacity: 0.3 + intensity * 0.7 }}>
                  <div className="hour-label">{fmtHour(hour)}</div>
                  <div className={`hour-pnl ${isPositive ? 'pos' : 'neg'}`}>{signedInr(data.pnl)}</div>
                  <div className="hour-stats">
                    <span>{data.trades} trades</span>
                    <span className={winRate >= 50 ? 'pos' : 'neg'}>{winRate.toFixed(0)}% WR</span>
                  </div>
                </div>
              )
            })}
          </div>
          <p className="muted small">
            Market hours 09:00–15:00. Green = profitable hour, Red = losing hour. Opacity = magnitude.
          </p>
        </Panel>
      )}

      {/* Symbol Performance Breakdown */}
      {intradayMetrics && Object.keys(intradayMetrics.symbolStats).length > 0 && (
        <Panel
          title="📈 Symbol Performance Breakdown"
          subtitle="Per-symbol P&L, win rate, and volume. Find your alpha sources and cut losers."
        >
          <DataTable
            columns={[
              { header: 'Symbol', cell: (row) => row.symbol },
              { header: 'Trades', cell: (row) => num(row.trades, 0), align: 'right' },
              { header: 'Volume', cell: (row) => inr(row.volume), align: 'right' },
              {
                header: 'Net P&L',
                align: 'right',
                cell: (row) => (
                  <span className={row.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {signedInr(row.pnl)}
                  </span>
                ),
              },
              {
                header: 'Win Rate',
                align: 'right',
                cell: (row) => (
                  <span className={row.winRate >= 50 ? 'pos' : 'neg'}>
                    {row.winRate.toFixed(1)}%
                  </span>
                ),
              },
              {
                header: 'Avg P&L/Trade',
                align: 'right',
                cell: (row) => (
                  <span className={row.avgPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {signedInr(row.avgPnl)}
                  </span>
                ),
              },
            ]}
            rows={Object.entries(intradayMetrics.symbolStats)
              .map(([symbol, stats]) => ({
                symbol,
                trades: stats.trades,
                volume: stats.volume,
                pnl: stats.pnl,
                winRate: stats.trades > 0 ? (stats.wins / stats.trades) * 100 : 0,
                avgPnl: stats.trades > 0 ? stats.pnl / stats.trades : 0,
              }))
              .sort((a, b) => b.pnl - a.pnl)}
          />
        </Panel>
      )}

      {/* Exit Reason Analysis */}
      {intradayMetrics && Object.keys(intradayMetrics.exitReasons).length > 0 && (
        <Panel
          title="🎯 Exit Reason Analysis"
          subtitle="Why trades were closed. Optimize your exit logic by P&L contribution."
        >
          <DataTable
            columns={[
              { header: 'Exit Reason', cell: (row) => row.reason },
              { header: 'Count', cell: (row) => num(row.count, 0), align: 'right' },
              {
                header: 'Total P&L',
                align: 'right',
                cell: (row) => (
                  <span className={row.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {signedInr(row.pnl)}
                  </span>
                ),
              },
              {
                header: 'Avg P&L',
                align: 'right',
                cell: (row) => (
                  <span className={row.avgPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {signedInr(row.avgPnl)}
                  </span>
                ),
              },
              {
                header: 'Contribution',
                align: 'right',
                cell: (row) => {
                  const totalPnl = Object.values(intradayMetrics!.exitReasons).reduce((a, b) => a + b.pnl, 0)
                  return totalPnl !== 0 ? `${((row.pnl / totalPnl) * 100).toFixed(1)}%` : '—'
                },
              },
            ]}
            rows={Object.entries(intradayMetrics.exitReasons)
              .map(([reason, stats]) => ({
                reason,
                count: stats.count,
                pnl: stats.pnl,
                avgPnl: stats.count > 0 ? stats.pnl / stats.count : 0,
              }))
              .sort((a, b) => b.pnl - a.pnl)}
          />
        </Panel>
      )}

      {daily.length > 0 && (
        <Panel
          title="Closed P&L by day"
          subtitle="Realized P&L grouped by the day each trade was closed, newest first."
        >
          <DataTable
            columns={[
              { header: 'Date', cell: (row) => row.date },
              { header: 'Trades', cell: (row) => num(row.trades, 0), align: 'right' },
              {
                header: 'Closed P&L',
                align: 'right',
                cell: (row) => (
                  <span className={row.closed_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {signedInr(row.closed_pnl)}
                  </span>
                ),
              },
            ]}
            rows={daily}
          />
        </Panel>
      )}

      <Panel
        title="Ledger"
        subtitle="One row per money movement, newest first. A realized P&L row always has the trade that produced it linked in the Trades table below."
      >
        <DataTable
          empty="No ledger rows yet."
          columns={[
            { header: 'When', cell: (row) => shortTime(row.transacted_at) },
            { header: 'Type', cell: (row) => TYPE_LABEL[row.type] },
            {
              header: 'Symbol',
              cell: (row) =>
                row.symbol ? (
                  <button type="button" className="link" onClick={() => onSelectSymbol(row.symbol as string)}>
                    {row.symbol}
                  </button>
                ) : (
                  <span className="muted">—</span>
                ),
            },
            {
              header: 'Amount',
              align: 'right',
              cell: (row) => (
                <span className={row.amount >= 0 ? 'pnl-pos' : 'pnl-neg'}>{signedInr(row.amount)}</span>
              ),
            },
            {
              header: 'Trade',
              align: 'right',
              cell: (row) => (row.trade_id === null ? <span className="muted">—</span> : `#${row.trade_id}`),
            },
            { header: 'Note', cell: (row) => row.note ?? <span className="muted">—</span> },
          ]}
          rows={ledger}
        />

        {ledger.length > 0 && (
          <p className="muted small">
            Row tone shows direction only: a withdrawal is a negative movement, not a loss. Whether the
            book is up or down is the Realized P&L figure above.
          </p>
        )}
      </Panel>

      <Panel
        title="Closed trades"
        subtitle="Entry vs exit for every closed paper position, with the slippage deducted and why it was closed."
      >
        <DataTable
          empty="No trades closed yet."
          columns={[
            { header: 'Symbol', cell: (row) => row.symbol },
            { header: 'Side', cell: (row) => <span className={row.action === 'BUY' ? 'action-buy' : 'action-sell'}>{row.action}</span> },
            { header: 'Size', cell: (row) => num(row.position_size, 0), align: 'right' },
            { header: 'Entry', cell: (row) => num(row.entry_price, 2), align: 'right' },
            { header: 'Exit', cell: (row) => num(row.exit_price, 2), align: 'right' },
            {
              header: 'P&L',
              align: 'right',
              cell: (row) => (
                <span className={row.realized_pnl_rupees >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                  {signedInr(row.realized_pnl_rupees)}
                </span>
              ),
            },
            { header: 'Slippage', cell: (row) => inr(row.slippage_deducted), align: 'right' },
            { header: 'Reason', cell: (row) => row.exit_reason },
            { header: 'Closed', cell: (row) => shortTime(row.exit_at) },
          ]}
          rows={trades}
        />
      </Panel>
    </>
  )
}
