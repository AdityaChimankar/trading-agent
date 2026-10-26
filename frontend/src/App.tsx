import { useEffect, useRef, useState } from 'react'
import { useIsFetching, useQueryClient } from '@tanstack/react-query'
import { Alert, Box, Button, TextField, Typography } from '@mui/material'

import { setApiToken } from './api/client'
import { useRankings, useWallet, useWatchlist } from './api/queries'
import { useApiAuth } from './api/useApiAuth'
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
import ResearchDashboard from './ResearchDashboard'
import RiskExecutionPage from './components/RiskExecutionPage'
import AnalyticsPage from './components/AnalyticsPage'

export default function App() {
  const [capital, setCapital] = useState(100_000)
  const [symbol, setSymbol] = useState<string | null>(null)
  const [token, setToken] = useState('')
  const [route, navigate] = useHashRoute()
  const queryClient = useQueryClient()
  const auth = useApiAuth()
  const { data: watchlist } = useWatchlist()
  const { data: wallet } = useWallet()
  const walletCapitalSynced = useRef(false)

  const rankings = useRankings(capital)

  // Open on the latest scan's strongest name: the first watchlist entry is just
  // the alphabetical start of the universe, usually without live data.
  useEffect(() => {
    if (symbol) return
    const strongest = [...(rankings.data?.scored ?? [])]
      .sort((a, b) => Math.max(b.bullish_score, b.bearish_score) - Math.max(a.bullish_score, a.bearish_score))[0]
    if (strongest) setSymbol(strongest.symbol)
    else if (rankings.isFetched && watchlist?.symbols.length) setSymbol(watchlist.symbols[0])
  }, [rankings.data, rankings.isFetched, watchlist, symbol])

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

  const workspace = route === 'research' || route === 'paper'

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

      {/* Paper operations carries its own token field; never show two. */}
      {auth.data?.required && route !== 'paper' && (
        <Box
          component="form"
          aria-label="API authentication"
          onSubmit={(event) => {
            event.preventDefault()
            setApiToken(token)
            setToken('')
            void queryClient.invalidateQueries()
          }}
          sx={{ display: 'flex', gap: 1, alignItems: 'center', px: 2.5, py: 1, borderBottom: 1, borderColor: 'divider' }}
        >
          <Typography variant="body2" color="text.secondary">API authentication</Typography>
          <TextField
            type="password"
            label="Session API token"
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
          <Button type="submit" variant="contained">Apply session token</Button>
        </Box>
      )}
      {auth.isError && (
        <Alert severity="error" sx={{ borderRadius: 0 }}>
          Could not check API authentication settings. Check the API connection, then refresh.
        </Alert>
      )}

      {route === 'dashboard' && (
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
      )}

      {/* Research and Paper share one mounted workspace so scan settings,
          the selected stock and validation state survive switching tabs. */}
      {workspace && <ResearchDashboard embedded page={route} onPageChange={navigate} />}

      {!workspace && route !== 'dashboard' && (
        <main className="main main-wide">
          {route === 'history' && <TransactionsPage onSelectSymbol={openSymbol} />}
          {route === 'pipeline' && <PipelinePage capital={capital} onSelectSymbol={openSymbol} />}
          {route === 'news' && <NewsPage onSelectSymbol={openSymbol} />}
          {route === 'risk' && <RiskExecutionPage />}
          {route === 'analytics' && <AnalyticsPage onSelectSymbol={openSymbol} />}
        </main>
      )}
    </div>
  )
}
