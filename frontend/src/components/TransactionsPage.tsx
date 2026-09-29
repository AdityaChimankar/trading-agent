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
  const { data, isLoading, error } = useTransactions()

  const s = data?.summary
  const ledger = data?.ledger ?? []
  const trades = data?.trades ?? []
  const daily = data?.daily ?? []
  const hasAnyBook = Boolean(s && (s.transaction_count > 0 || s.trade_count > 0))

  return (
    <>
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
