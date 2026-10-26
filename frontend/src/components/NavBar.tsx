import { AppBar, Autocomplete, Box, Button, Chip, Tab, Tabs, TextField, Toolbar, Typography } from '@mui/material'

import { ROUTES, type Route } from '../lib/router'
import { tokens } from '../theme'

export const ROUTE_LABELS: Record<Route, string> = {
  dashboard: 'Dashboard',
  screener: 'Screener',
  research: 'Research',
  paper: 'Paper operations',
  risk: 'Risk & execution',
  analytics: 'Analytics',
  pipeline: 'Signal Pipeline',
  history: 'Transactions',
  news: 'News',
}

export default function NavBar({
  route,
  onNavigate,
  inFlight,
  onRefresh,
  symbol,
  symbols,
  onSymbolChange,
  showSymbolPicker,
}: {
  route: Route
  onNavigate: (route: Route) => void
  inFlight: number
  onRefresh: () => void
  symbol: string | null
  symbols: string[]
  onSymbolChange: (symbol: string) => void
  showSymbolPicker: boolean
}) {
  return (
    <AppBar position="sticky">
      <Toolbar variant="dense" sx={{ gap: 2, flexWrap: { xs: 'wrap', md: 'nowrap' }, py: 1 }}>
        <Typography sx={{ fontWeight: 700, whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 1 }}>
          <Box component="span" sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: tokens.accent }} />
          Intraday Trading Agent
        </Typography>

        {/* Scrollable tabs: seven sections never overflow the page on a phone. */}
        <Tabs
          value={route}
          onChange={(_, next: Route) => onNavigate(next)}
          variant="scrollable"
          scrollButtons="auto"
          allowScrollButtonsMobile
          aria-label="Sections"
          sx={{ flex: { xs: '1 1 100%', md: '1 1 0' }, minWidth: 0, maxWidth: '100%', order: { xs: 3, md: 0 } }}
        >
          {ROUTES.map((r) => <Tab key={r} value={r} label={ROUTE_LABELS[r]} />)}
        </Tabs>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, ml: 'auto' }}>
          {showSymbolPicker && (
            <Autocomplete
              size="small"
              options={symbols}
              value={symbol}
              onChange={(_, next) => next && onSymbolChange(next)}
              // Thousands of symbols: cap the rendered list, search narrows it.
              filterOptions={(options, state) => {
                const q = state.inputValue.toUpperCase()
                return options.filter((s) => s.includes(q)).slice(0, 200)
              }}
              renderInput={(params) => <TextField {...params} label="Symbol" />}
              sx={{ width: 180 }}
            />
          )}
          <Chip
            size="small"
            variant="outlined"
            color={inFlight > 0 ? 'primary' : 'default'}
            label={inFlight > 0 ? '⟳ live' : '● idle'}
            title="Auto-refreshing"
          />
          <Button variant="outlined" onClick={onRefresh}>Refresh</Button>
        </Box>
      </Toolbar>
    </AppBar>
  )
}
