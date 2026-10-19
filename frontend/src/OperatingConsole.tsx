import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost, setApiToken } from './api/client'
import { Panel, Metric } from './components/ui'
import { inr } from './lib/format'

interface Position {
  id: number; symbol: string; action: string; entry_price: number; quantity: number
  stop_loss: number; take_profit: number | null; status: string; pnl: number | null
}
interface Book {
  cash: number; invested: number; total_value: number; total_pnl: number; open_positions: number
  accounting_basis: string
}
interface Candle { timestamp: string; open: number; high: number; low: number; close: number; volume: number }

function ErrorMessage({ error }: { error: Error | null }) {
  return error ? <p className="error" role="alert">{error.message}</p> : null
}

export default function OperatingConsole() {
  const cache = useQueryClient()
  const [token, setToken] = useState('')
  const [symbol, setSymbol] = useState('RELIANCE')
  const [interval, setInterval] = useState('minute')
  const [exitPrices, setExitPrices] = useState<Record<number, string>>({})
  const [notice, setNotice] = useState('')
  const ready = useQuery({ queryKey: ['ready'], queryFn: () => apiGet<{ status: string }>('/api/v1/ready'), refetchInterval: 15000 })
  const feed = useQuery({ queryKey: ['feed'], queryFn: () => apiGet<{ connected: boolean; last_tick: string | null }>('/api/v1/liveness/feed'), refetchInterval: 15000 })
  const book = useQuery({ queryKey: ['book'], queryFn: () => apiGet<Book>('/api/v1/wallet/summary'), refetchInterval: 15000 })
  const positions = useQuery({ queryKey: ['paper-positions'], queryFn: () => apiGet<Position[]>('/api/v1/positions/'), refetchInterval: 15000 })
  const closed = useQuery({ queryKey: ['paper-closed'], queryFn: () => apiGet<Position[]>('/api/v1/positions/?status=closed'), refetchInterval: 15000 })
  const symbols = useQuery({ queryKey: ['stored-symbols'], queryFn: () => apiGet<{ symbol: string }[]>('/api/v1/symbols/stored') })
  const history = useQuery({ queryKey: ['candles', symbol, interval], queryFn: () => apiGet<{ candles: Candle[] }>(`/api/v1/history/${encodeURIComponent(symbol)}?interval=${interval}&limit=30`), enabled: Boolean(symbol), refetchInterval: 30000 })
  const pipeline = useQuery({ queryKey: ['pipeline-status'], queryFn: () => apiGet<{ status: string; symbols_processed: number; errors: string[] }>('/api/v1/pipeline/status'), refetchInterval: 15000 })
  function refresh() { void cache.invalidateQueries() }
  const create = useMutation({ mutationFn: (body: object) => apiPost('/api/v1/positions/', body), onSuccess: () => { setNotice('Paper position recorded.'); refresh() } })
  const close = useMutation({ mutationFn: ({ id, price }: { id: number; price: number }) => apiPost(`/api/v1/positions/${id}/close?exit_price=${price}`), onSuccess: () => { setNotice('Paper position closed.'); refresh() } })
  const run = useMutation({ mutationFn: () => apiPost('/api/v1/pipeline/run', { symbols: [symbol], interval }), onSuccess: refresh })
  function record(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const target = form.get('take_profit')
    create.mutate({ symbol: String(form.get('symbol')), action: String(form.get('action')),
      entry_price: Number(form.get('entry_price')), quantity: Number(form.get('quantity')),
      stop_loss: Number(form.get('stop_loss')), take_profit: target ? Number(target) : null, strategy: 'manual' })
  }
  return <div className="app"><main className="main main-wide">
    <Panel title="Trading Agent · Paper operations" subtitle="Research and paper positions. Entries and exits below use prices you supply; no broker orders are sent.">
      <div className="metric-row">
        <Metric label="Database" value={ready.data?.status ?? (ready.error ? 'Unavailable' : 'Checking')} />
        <Metric label="Market feed" value={feed.data?.connected ? 'Connected' : 'Disconnected / unknown'} hint={feed.data?.last_tick ?? 'No tick received'} />
        <Metric label="Pipeline" value={pipeline.data?.status ?? 'Unknown'} />
      </div>
      <button onClick={refresh}>Refresh</button> <a href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API documentation</a>
      <details><summary>API access token (if enabled on the server)</summary>
        <form onSubmit={event => { event.preventDefault(); setApiToken(token); setToken(''); refresh() }}>
          <label>Bearer token <input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
          <button type="submit">Use for this page session</button>
        </form>
      </details>
      <ErrorMessage error={ready.error} /><ErrorMessage error={feed.error} /><ErrorMessage error={pipeline.error} />
      <p role="status">{notice}</p>
    </Panel>
    <Panel title="Persistent paper book" subtitle="Entry-cost accounting. Fees, slippage and unrealized gains/losses are excluded.">
      <ErrorMessage error={book.error} />
      {book.data && <div className="metric-row">
        <Metric label="Book equity" value={inr(book.data.total_value)} />
        <Metric label="Uncommitted cash" value={inr(book.data.cash)} />
        <Metric label="Committed notional" value={inr(book.data.invested)} />
        <Metric label="Realized gross P&L" value={inr(book.data.total_pnl)} />
      </div>}
    </Panel>
    <Panel title="Stored candles" subtitle="Stored timestamps are UTC. Missing data is an error, not a zero price.">
      <label>Symbol <input list="stored-symbols" value={symbol} onChange={event => setSymbol(event.target.value.trim().toUpperCase())} /></label>
      <datalist id="stored-symbols">{symbols.data?.map(row => <option key={row.symbol} value={row.symbol} />)}</datalist>
      <label>Interval <select value={interval} onChange={event => setInterval(event.target.value)}>
        <option value="minute">1 minute</option><option value="5minute">5 minutes</option><option value="15minute">15 minutes</option>
      </select></label>
      <button disabled={!symbol || run.isPending || pipeline.data?.status === 'running'} onClick={() => run.mutate()}>Analyze selected symbol</button>
      <ErrorMessage error={symbols.error} /><ErrorMessage error={history.error} /><ErrorMessage error={run.error} />
      {pipeline.data?.errors.map((error, index) => <p role="alert" key={index}>{error}</p>)}
      <div style={{ overflowX: 'auto' }}><table><thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th></tr></thead>
        <tbody>{history.data?.candles.map(c => <tr key={c.timestamp}><td>{c.timestamp}</td><td>{c.open}</td><td>{c.high}</td><td>{c.low}</td><td>{c.close}</td><td>{c.volume}</td></tr>)}</tbody></table></div>
      {history.data?.candles.length === 0 && <p>No candles available for this interval.</p>}
    </Panel>
    <Panel title="Record a paper position" subtitle="The server checks bracket direction, available capital, duplicate symbols, and exposure/risk limits.">
      <form onSubmit={record} className="metric-row">
        <label>Symbol <input name="symbol" required maxLength={20} defaultValue="RELIANCE" /></label>
        <label>Side <select name="action"><option>BUY</option><option>SELL</option></select></label>
        <label>Entry price <input name="entry_price" type="number" min="0.01" step="any" required /></label>
        <label>Quantity <input name="quantity" type="number" min="1" step="1" required /></label>
        <label>Stop loss <input name="stop_loss" type="number" min="0.01" step="any" required /></label>
        <label>Take profit (optional) <input name="take_profit" type="number" min="0.01" step="any" /></label>
        <button disabled={create.isPending} type="submit">Record paper position</button>
      </form><ErrorMessage error={create.error} />
    </Panel>
    <Panel title="Open paper positions">
      <ErrorMessage error={positions.error} /><ErrorMessage error={close.error} />
      {positions.data?.length === 0 && <p>No open positions.</p>}
      {positions.data?.map(p => <form key={p.id} onSubmit={event => { event.preventDefault(); close.mutate({ id: p.id, price: Number(exitPrices[p.id]) }) }}>
        <p>{p.symbol} · {p.action} · {p.quantity} at {inr(p.entry_price)} · Stop {inr(p.stop_loss)}</p>
        <label>Exit price <input aria-label={`Exit price for ${p.symbol}`} required type="number" min="0.01" step="any" value={exitPrices[p.id] ?? ''} onChange={event => setExitPrices({ ...exitPrices, [p.id]: event.target.value })} /></label>
        <button disabled={close.isPending}>Close paper position</button>
      </form>)}
    </Panel>
    <Panel title="Closed paper positions">
      <ErrorMessage error={closed.error} />
      {closed.data?.length === 0 && <p>No closed trades.</p>}
      {closed.data?.map(p => <p key={p.id}>{p.symbol} · {p.action} · {p.quantity} · Gross P&L {inr(p.pnl ?? 0)}</p>)}
    </Panel>
  </main></div>
}
