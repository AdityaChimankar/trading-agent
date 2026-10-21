import { useMemo, useState } from 'react'
import { useAllNews } from '../api/queries'
import { shortTime } from '../lib/format'

type SortKey = 'symbol' | 'headline' | 'source' | 'score' | 'published_at'

export default function NewsPage({ onSelectSymbol }: { onSelectSymbol: (symbol: string) => void }) {
  const { data: news, isLoading, error, refetch } = useAllNews()
  const [sortKey, setSortKey] = useState<SortKey>('published_at')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  const sortedNews = useMemo(() => {
    if (!news) return []
    return [...news].sort((a, b) => {
      const aVal = a[sortKey]
      const bVal = b[sortKey]
      if (aVal === null || aVal === undefined) return 1
      if (bVal === null || bVal === undefined) return -1
      const cmp = String(aVal).localeCompare(String(bVal), undefined, { numeric: true })
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [news, sortKey, sortDir])

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  function getSentimentColor(score: number | null) {
    if (score === null || score === undefined) return 'var(--muted)'
    if (score > 0.2) return 'var(--success)'
    if (score < -0.2) return 'var(--danger)'
    return 'var(--warning)'
  }

  function getSentimentLabel(score: number | null) {
    if (score === null || score === undefined) return '—'
    if (score > 0.2) return 'Bullish'
    if (score < -0.2) return 'Bearish'
    return 'Neutral'
  }

  if (isLoading) {
    return <div className="page-loading">Loading news...</div>
  }

  if (error) {
    return <div className="page-error">Failed to load news: {String(error)}</div>
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>Market News</h1>
        <button className="btn" onClick={() => refetch()}>Refresh</button>
      </header>

      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th onClick={() => handleSort('symbol')}>
                Symbol {sortKey === 'symbol' && (sortDir === 'asc' ? '▴' : '▾')}
              </th>
              <th onClick={() => handleSort('headline')}>
                Headline {sortKey === 'headline' && (sortDir === 'asc' ? '▴' : '▾')}
              </th>
              <th onClick={() => handleSort('source')}>
                Source {sortKey === 'source' && (sortDir === 'asc' ? '▴' : '▾')}
              </th>
              <th onClick={() => handleSort('score')}>
                Sentiment {sortKey === 'score' && (sortDir === 'asc' ? '▴' : '▾')}
              </th>
              <th onClick={() => handleSort('published_at')}>
                Published {sortKey === 'published_at' && (sortDir === 'asc' ? '▴' : '▾')}
              </th>
            </tr>
          </thead>
          <tbody>
            {sortedNews.length === 0 ? (
              <tr>
                <td colSpan={5} className="table-empty">No news available</td>
              </tr>
            ) : (
              sortedNews.map((item, idx) => (
                <tr key={`${item.symbol}-${item.published_at}-${idx}`}>
                  <td>
                    <button
                      type="button"
                      className="symbol-link"
                      onClick={() => onSelectSymbol(item.symbol)}
                    >
                      {item.symbol}
                    </button>
                  </td>
                  <td className="headline-cell">{item.headline}</td>
                  <td>{item.source ?? '—'}</td>
                  <td>
                    <span
                      className="sentiment-badge"
                      style={{ backgroundColor: getSentimentColor(item.score) }}
                    >
                      {item.score !== null && item.score !== undefined ? item.score.toFixed(2) : '—'}
                      <span className="sentiment-label">{getSentimentLabel(item.score)}</span>
                    </span>
                  </td>
                  <td>{shortTime(item.published_at)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}