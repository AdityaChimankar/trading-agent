import { useEffect, useRef } from 'react'
import Plotly from 'plotly.js-dist-min'

import type { Action, ChartResponse } from '../api/types'

/**
 * Greatest candle index whose timestamp is <= target, or -1 if target
 * predates the window.
 *
 * Signal rows are written by decision_agent with `datetime.now()`, so a
 * signal at 09:17:23 never matches a 09:15:00 candle timestamp. The old
 * Streamlit chart mapped timestamps via a dict lookup, which produced NaN
 * for every non-matching row and silently dropped the marker - so BUY/SELL
 * markers effectively never rendered. Snapping to the containing candle
 * makes them actually appear.
 *
 * Both timestamps are ISO-8601 in the same format, so lexicographic
 * comparison is chronological.
 */
function snapToCandle(timestamps: string[], target: string): number {
  let low = 0
  let high = timestamps.length - 1
  let answer = -1
  while (low <= high) {
    const mid = (low + high) >> 1
    if (timestamps[mid] <= target) {
      answer = mid
      low = mid + 1
    } else {
      high = mid - 1
    }
  }
  return answer
}

export default function PriceChart({ data }: { data: ChartResponse }) {
  const ref = useRef<HTMLDivElement | null>(null)
  // The element Plotly actually drew into. Held separately from `ref`
  // because React detaches `ref` before running passive-effect cleanup,
  // so reading ref.current at cleanup time can yield null and leak.
  const plotElRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el || data.insufficient_data || !data.candles) return
    plotElRef.current = el

    const { candles, indicators, patterns, signal_markers: markers, symbol } = data
    const x = candles.timestamp

    const markerPoints = (action: Action) => {
      const xs: string[] = []
      const ys: number[] = []
      for (const marker of markers) {
        if (marker.action !== action) continue
        const idx = snapToCandle(x, marker.timestamp)
        if (idx < 0) continue
        xs.push(x[idx])
        ys.push(candles.close[idx])
      }
      return { xs, ys }
    }

    const patternPoints = (name: keyof typeof patterns) => {
      const xs: string[] = []
      const ys: number[] = []
      patterns[name].forEach((hit, i) => {
        if (hit) {
          xs.push(x[i])
          ys.push(candles.high[i] * 1.002)
        }
      })
      return { xs, ys }
    }

    const buys = markerPoints('BUY')
    const sells = markerPoints('SELL')

    const patternTraces = (
      [
        ['doji', 'circle', 'gray'],
        ['hammer', 'star', 'deepskyblue'],
        ['bullish_engulfing', 'diamond', 'limegreen'],
        ['bearish_engulfing', 'diamond', 'crimson'],
      ] as const
    ).map(([name, markerSymbol, color]) => {
      const { xs, ys } = patternPoints(name)
      return {
        x: xs,
        y: ys,
        type: 'scatter',
        mode: 'markers',
        name,
        marker: { symbol: markerSymbol, size: 8, color },
        xaxis: 'x',
        yaxis: 'y',
        hovertemplate: `${name} %{x}<extra></extra>`,
      }
    })

    const traceData: unknown[] = [
      {
        x,
        open: candles.open,
        high: candles.high,
        low: candles.low,
        close: candles.close,
        type: 'candlestick',
        name: symbol,
        xaxis: 'x',
        yaxis: 'y',
      },
      {
        x,
        y: indicators.ma20,
        type: 'scatter',
        mode: 'lines',
        name: 'MA20',
        line: { color: 'orange', width: 1 },
        xaxis: 'x',
        yaxis: 'y',
      },
      {
        x,
        y: indicators.ma50,
        type: 'scatter',
        mode: 'lines',
        name: 'MA50',
        line: { color: 'mediumpurple', width: 1 },
        xaxis: 'x',
        yaxis: 'y',
      },
      {
        x: buys.xs,
        y: buys.ys,
        type: 'scatter',
        mode: 'markers',
        name: 'BUY signal',
        marker: { symbol: 'triangle-up', size: 12, color: 'limegreen' },
        xaxis: 'x',
        yaxis: 'y',
      },
      {
        x: sells.xs,
        y: sells.ys,
        type: 'scatter',
        mode: 'markers',
        name: 'SELL signal',
        marker: { symbol: 'triangle-down', size: 12, color: 'crimson' },
        xaxis: 'x',
        yaxis: 'y',
      },
      ...patternTraces,
      {
        x,
        y: candles.volume,
        type: 'bar',
        name: 'Volume',
        marker: { color: 'steelblue' },
        xaxis: 'x2',
        yaxis: 'y2',
      },
      {
        x,
        y: indicators.rsi,
        type: 'scatter',
        mode: 'lines',
        name: 'RSI',
        line: { color: 'teal' },
        xaxis: 'x3',
        yaxis: 'y3',
      },
      {
        x,
        y: indicators.adx,
        type: 'scatter',
        mode: 'lines',
        name: 'ADX',
        line: { color: 'saddlebrown' },
        xaxis: 'x4',
        yaxis: 'y4',
      },
    ]

    // Four stacked panels. Domains are set explicitly on every axis
    // (rather than using layout.grid, which needs domains on all axes
    // anyway and conflicts if you mix the two approaches).
    const axis = (showTicks: boolean) => ({
      domain: [0, 1],
      showticklabels: showTicks,
      gridcolor: '#1e2733',
      zerolinecolor: '#1e2733',
    })

    const layout: Record<string, unknown> = {
      height: 850,
      margin: { l: 55, r: 20, t: 30, b: 30 },
      paper_bgcolor: '#0f141a',
      plot_bgcolor: '#0f141a',
      font: { color: '#c9d4e0', size: 11 },
      showlegend: true,
      legend: { orientation: 'h', yanchor: 'bottom', y: 1.02, x: 0 },
      dragmode: 'pan',
      xaxis: { ...axis(false), anchor: 'y', rangeslider: { visible: false } },
      yaxis: { title: { text: 'Price' }, domain: [0.46, 1], gridcolor: '#1e2733' },
      xaxis2: { ...axis(false), anchor: 'y2' },
      yaxis2: { title: { text: 'Vol' }, domain: [0.34, 0.44], gridcolor: '#1e2733' },
      xaxis3: { ...axis(false), anchor: 'y3' },
      yaxis3: {
        title: { text: 'RSI' },
        domain: [0.21, 0.32],
        range: [0, 100],
        gridcolor: '#1e2733',
      },
      xaxis4: { ...axis(true), anchor: 'y4' },
      yaxis4: { title: { text: 'ADX' }, domain: [0, 0.19], gridcolor: '#1e2733' },
      shapes: [
        // RSI 30/70 oversold-overbought and the ADX 20 trend gate.
        hline(70, 'y3', 'crimson'),
        hline(30, 'y3', 'seagreen'),
        hline(20, 'y4', 'gray'),
      ],
    }

    void Plotly.react(el, traceData, layout, { responsive: true, displaylogo: false })

    // Deliberately NO purge here. This effect re-runs on every poll
    // (15s); purging in its cleanup would tear the plot down and rebuild
    // it from scratch each time, which visibly flickers. Plotly.react
    // already diffs and updates in place. Purge happens on unmount only,
    // in the effect below.
  }, [data])

  useEffect(() => {
    return () => {
      if (plotElRef.current) {
        Plotly.purge(plotElRef.current)
        plotElRef.current = null
      }
    }
  }, [])

  if (data.insufficient_data || !data.candles) {
    return <p className="muted small">Not enough candle data yet — run fetch_historical.py first.</p>
  }

  return <div className="chart" ref={ref} />
}

// Full-width dotted reference line on a specific y-axis. 'paper' xref
// spans the whole plot regardless of which subplot the axis belongs to.
function hline(y: number, yref: string, color: string) {
  return {
    type: 'line',
    xref: 'paper',
    yref,
    x0: 0,
    x1: 1,
    y0: y,
    y1: y,
    line: { color, width: 1, dash: 'dot' },
  }
}
