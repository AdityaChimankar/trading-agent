// React Query hooks - the "more dynamic" layer. Each panel polls on its
// own interval and shares one cache, so a panel opened twice fetches once.
// Intervals are grouped here so polling cadence is tunable in one place.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiGet, apiPost } from './client'
import type {
  AgentSignalsResponse,
  ChartResponse,
  DigestResponse,
  LlmSignalRow,
  MlSignalRow,
  NewsRow,
  OpportunitiesResponse,
  PipelineResponse,
  PositionStatus,
  RankingsResponse,
  SignalRow,
  SizingResponse,
  TransactionsResponse,
  WatchlistResponse,
  WalletResponse,
} from './types'

export const REFRESH = {
  positions: 15_000,
  chart: 15_000,
  rankings: 60_000,
  opportunities: 60_000,
  sizing: 60_000,
  patterns: 60_000,
  signals: 60_000,
  agentSignals: 120_000,
  news: 120_000,
  digest: 300_000,
  wallet: 15_000,
  // The book only changes when you deposit/withdraw/close something, so the
  // History page is polled lazily - slow enough to stay out of the way,
  // frequent enough that a close appears while you are still looking at it.
  transactions: 30_000,
  // Pipeline re-runs the watchlist-wide ranking server-side like /api/rankings
  // does, so it is deliberately slower than the panels that poll every 15s.
  pipeline: 120_000,
} as const

export function useWatchlist() {
  return useQuery({
    queryKey: ['watchlist'],
    queryFn: () => apiGet<WatchlistResponse>('/api/watchlist'),
    staleTime: Infinity,
  })
}

export function useRankings(capital: number) {
  return useQuery({
    queryKey: ['rankings', capital],
    queryFn: () => apiGet<RankingsResponse>(`/api/rankings?capital=${capital}`),
    refetchInterval: REFRESH.rankings,
  })
}

export function usePositions() {
  return useQuery({
    queryKey: ['positions'],
    queryFn: () => apiGet<PositionStatus[]>('/api/positions'),
    refetchInterval: REFRESH.positions,
  })
}
export function useWallet() {
  return useQuery({
    queryKey: ['wallet'],
    queryFn: () => apiGet<WalletResponse>('/api/wallet'),
    refetchInterval: REFRESH.wallet,
  })
}
export function useOpportunities(capital: number) {
  return useQuery({
    queryKey: ['opportunities', capital],
    queryFn: () => apiGet<OpportunitiesResponse>(`/api/opportunities?capital=${capital}`),
    refetchInterval: REFRESH.opportunities,
  })
}

export function useTransactions() {
  return useQuery({
    queryKey: ['transactions'],
    queryFn: () => apiGet<TransactionsResponse>('/api/transactions?limit=200'),
    refetchInterval: REFRESH.transactions,
  })
}

export function usePipeline(capital: number) {
  return useQuery({
    queryKey: ['pipeline', capital],
    queryFn: () => apiGet<PipelineResponse>(`/api/pipeline?capital=${capital}`),
    refetchInterval: REFRESH.pipeline,
  })
}

export function useDigest() {
  return useQuery({
    queryKey: ['digest'],
    queryFn: () => apiGet<DigestResponse>('/api/digest'),
    refetchInterval: REFRESH.digest,
  })
}

export function useChart(symbol: string | null) {
  return useQuery({
    queryKey: ['chart', symbol],
    queryFn: () => apiGet<ChartResponse>(`/api/symbols/${symbol}/chart?limit=200`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.chart,
  })
}

export function usePatterns(symbol: string | null) {
  return useQuery({
    queryKey: ['patterns', symbol],
    queryFn: () => apiGet<{ patterns: string[]; bias: string }>(`/api/symbols/${symbol}/patterns`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.patterns,
  })
}

export function useSizing(symbol: string | null, capital: number) {
  return useQuery({
    queryKey: ['sizing', symbol, capital],
    queryFn: () => apiGet<SizingResponse>(`/api/symbols/${symbol}/sizing?capital=${capital}`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.sizing,
  })
}

export function useNews(symbol: string | null) {
  return useQuery({
    queryKey: ['news', symbol],
    queryFn: () => apiGet<NewsRow[]>(`/api/symbols/${symbol}/news?limit=10`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.news,
  })
}

export function useSignals(symbol: string | null) {
  return useQuery({
    queryKey: ['signals', symbol],
    queryFn: () => apiGet<SignalRow[]>(`/api/symbols/${symbol}/signals?limit=20`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.signals,
  })
}

export function useLlmSignals(symbol: string | null) {
  return useQuery({
    queryKey: ['llm-signals', symbol],
    queryFn: () => apiGet<AgentSignalsResponse<LlmSignalRow>>(`/api/symbols/${symbol}/llm-signals?limit=20`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.agentSignals,
  })
}

export function useMlSignals(symbol: string | null) {
  return useQuery({
    queryKey: ['ml-signals', symbol],
    queryFn: () => apiGet<AgentSignalsResponse<MlSignalRow>>(`/api/symbols/${symbol}/ml-signals?limit=20`),
    enabled: Boolean(symbol),
    refetchInterval: REFRESH.agentSignals,
  })
}

// --- Mutations -----------------------------------------------------------
// Opening or closing a position changes the portfolio state, which feeds
// back into rankings, opportunities and sizing - so all three are
// invalidated, not just the positions list.

function usePortfolioMutation<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['positions'] })
      void queryClient.invalidateQueries({ queryKey: ['rankings'] })
      void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
      void queryClient.invalidateQueries({ queryKey: ['sizing'] })
    },
  })
}

export function useOpenPosition() {
  return usePortfolioMutation(
    (vars: { symbol: string; action: 'BUY' | 'SELL'; capital: number }) =>
      apiPost<{ symbol: string; shares: number; value: number }>('/api/positions', vars),
  )
}

export function useClosePosition() {
  return usePortfolioMutation((id: number) =>
    apiPost<{ closed: number }>(`/api/positions/${id}/close`),
  )
}
