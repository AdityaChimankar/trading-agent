import { useEffect, useState, useEffect, useState } from 'react'
import { useIsFetching, useQueryClient } from '@tanstack/react-query'

import { useWatchlist, useWallet } from './api/queries'
import DigestPanel from './components/DigestPanel'
import OpportunityPanel from './components/OpportunityPanel'
import PositionMonitor from './components/PositionMonitor'
import Sidebar from './components/Sidebar'
import SymbolDetail from './components/SymbolDetail'
import WalletPanel from './components/WalletPanel'

export default function App() {
  const [capital, setCapital] = useState(100_000)
  const [symbol, setSymbol] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const { data: watchlist } = useWatchlist()

  // Default the selected symbol to the first watchlist entry once loaded.
  useEffect(() => {
    if (!symbol && watchlist?.symbols.length) setSymbol(watchlist.symbols[0])
  }, [watchlist, symbol])

  const inFlight = useIsFetching()

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          Intraday Trading Agent
        </div>

        <div className="topbar-right">
          <label className="field inline">
            <span>Symbol</span>
            <select value={symbol ?? ''} onChange={(e) => setSymbol(e.target.value)}>
              {(watchlist?.symbols ?? []).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>

          <span className={inFlight > 0 ? 'pulse is-active' : 'pulse'} title="Auto-refreshing">
            {inFlight > 0 ? '⟳ live' : '● idle'}
          </span>

          <button
            type="button"
            className="btn"
            onClick={() => queryClient.invalidateQueries()}
          >
            Refresh
          </button>
        </div>
      </header>

      <div className="layout">
        <Sidebar
          capital={capital}
          onCapitalChange={setCapital}
          selected={symbol}
          onSelect={setSymbol}
        />
        <main className="main">
          <OpportunityPanel capital={capital} onSelect={setSymbol} />
          <DigestPanel />
          <WalletPanel capital={capital} />
          <PositionMonitor />
          <SymbolDetail symbol={symbol} capital={capital} />
        </main>
      </div>
    </div>
  )
}
