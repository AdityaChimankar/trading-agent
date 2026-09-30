import { useEffect, useRef, useState } from 'react'
import { useIsFetching, useQueryClient } from '@tanstack/react-query'

import { useWallet, useWatchlist } from './api/queries'
import { useHashRoute } from './lib/router'
import DigestPanel from './components/DigestPanel'
import NavBar from './components/NavBar'
import OpportunityPanel from './components/OpportunityPanel'
import PipelinePage from './components/PipelinePage'
import PositionMonitor from './components/PositionMonitor'
import Sidebar from './components/Sidebar'
import SymbolDetail from './components/SymbolDetail'
import TransactionsPage from './components/TransactionsPage'
import WalletPanel from './components/WalletPanel'
import NewsPage from './components/NewsPage'

export default function App() {
  const [capital, setCapital] = useState(100_000)
  const [symbol, setSymbol] = useState<string | null>(null)
  const [route, navigate] = useHashRoute()
  const queryClient = useQueryClient()
  const { data: watchlist } = useWatchlist()
  const { data: wallet } = useWallet()
  const walletCapitalSynced = useRef(false)

  // Default the selected symbol to the first watchlist entry once loaded.
  useEffect(() => {
    if (!symbol && watchlist?.symbols.length) setSymbol(watchlist.symbols[0])
  }, [watchlist, symbol])

  // Seed the sizing capital from the wallet book balance on first load, so
  // "available capital" tracks the paper wallet instead of a hardcoded
  // number. Manual edits in the sidebar still override it afterwards.
  useEffect(() => {
    if (!walletCapitalSynced.current && wallet?.fixed?.book_balance) {
      walletCapitalSynced.current = true
      setCapital(wallet.fixed.book_balance)
    }
  }, [wallet])

  const inFlight = useIsFetching()

  // Pages that name a symbol (the ledger, the pipeline table) hand you back to
  // the dashboard WITH it selected rather than rendering a second chart in
  // place: there is exactly one symbol view, so a symbol is always read in the
  // same context whichever page you arrived from.
  function openSymbol(next: string) {
    setSymbol(next)
    navigate('dashboard')
  }

  return (
    <div className="app">
      <NavBar
        route={route}
        onNavigate={navigate}
        inFlight={inFlight}
        onRefresh={() => queryClient.invalidateQueries()}
        symbol={symbol}
        symbols={watchlist?.symbols ?? []}
        onSymbolChange={setSymbol}
        showSymbolPicker={route === 'dashboard'}
      />

      {route === 'dashboard' ? (
        <div className="layout">
          <Sidebar
            capital={capital}
            onCapitalChange={setCapital}
            selected={symbol}
            onSelect={setSymbol}
            watchlistSymbols={watchlist?.symbols ?? []}
          />
          <main className="main">
            <OpportunityPanel capital={capital} onSelect={setSymbol} />
            <DigestPanel />
            <WalletPanel />
            <PositionMonitor />
            <SymbolDetail symbol={symbol} capital={capital} />
          </main>
        </div>
      ) : (
        <main className="main main-wide">
          {route === 'history' && <TransactionsPage onSelectSymbol={openSymbol} />}
          {route === 'pipeline' && <PipelinePage capital={capital} onSelectSymbol={openSymbol} />}
          {route === 'news' && <NewsPage onSelectSymbol={openSymbol} />}
        </main>
      )}
    </div>
  )
}
