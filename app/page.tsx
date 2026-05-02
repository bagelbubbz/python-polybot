'use client'

import { useState, useCallback } from 'react'
import { RefreshCw, TrendingUp, Activity, BarChart2, AlertCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'
import { scoreTier } from '@/lib/metrics'
import type { ScanResult, StrategyType } from '@/lib/filters'

// ─── Types ────────────────────────────────────────────────────────────────────

interface ScanResponse {
  scannedAt: string
  count: number
  results: ScanResult[]
  errors: { symbol: string; reason: string }[]
}

// ─── Constants ────────────────────────────────────────────────────────────────

const STRATEGY_LABELS: Record<StrategyType, string> = {
  IRON_CONDOR: 'Iron Condor',
  STRANGLE: 'Strangle',
  CASH_SECURED_PUT: 'Cash Secured Put',
  COVERED_CALL: 'Covered Call',
}

const STRATEGY_ICONS: Record<StrategyType, React.ReactNode> = {
  IRON_CONDOR: <BarChart2 className="h-3.5 w-3.5" />,
  STRANGLE: <Activity className="h-3.5 w-3.5" />,
  CASH_SECURED_PUT: <TrendingUp className="h-3.5 w-3.5" />,
  COVERED_CALL: <TrendingUp className="h-3.5 w-3.5 rotate-180" />,
}

const ALL_STRATEGIES: (StrategyType | 'ALL')[] = [
  'ALL',
  'IRON_CONDOR',
  'STRANGLE',
  'CASH_SECURED_PUT',
  'COVERED_CALL',
]

// ─── Score Badge ──────────────────────────────────────────────────────────────

function ScoreBadge({ score }: { score: number }) {
  const tier = scoreTier(score)
  const variant = tier === 'green' ? 'green' : tier === 'yellow' ? 'yellow' : 'grey'
  return (
    <Badge variant={variant} className="text-sm font-bold px-3 py-1 tabular-nums">
      {score.toFixed(1)}
    </Badge>
  )
}

// ─── Metric Row ───────────────────────────────────────────────────────────────

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-sm">
      <span className="text-zinc-400">{label}</span>
      <span className="font-medium text-zinc-100 tabular-nums">{value}</span>
    </div>
  )
}

// ─── Result Card ──────────────────────────────────────────────────────────────

function ResultCard({ result }: { result: ScanResult }) {
  const tier = scoreTier(result.score)
  const borderColor =
    tier === 'green'
      ? 'border-emerald-500/40'
      : tier === 'yellow'
        ? 'border-yellow-500/40'
        : 'border-zinc-700'

  return (
    <Card
      className={cn(
        'bg-zinc-900 border transition-all hover:shadow-lg hover:shadow-black/40 hover:-translate-y-0.5',
        borderColor
      )}
    >
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-2xl font-bold text-white tracking-wide">
              {result.symbol}
            </CardTitle>
            <CardDescription className="flex items-center gap-1.5 mt-1 text-zinc-400">
              {STRATEGY_ICONS[result.strategy]}
              {STRATEGY_LABELS[result.strategy]}
            </CardDescription>
          </div>
          <ScoreBadge score={result.score} />
        </div>
      </CardHeader>

      <CardContent className="space-y-2.5">
        <Separator className="bg-zinc-800" />

        <div className="grid grid-cols-2 gap-x-4 gap-y-2 pt-1">
          <MetricRow label="IV Rank" value={`${result.ivr.toFixed(1)}%`} />
          <MetricRow label="PoP" value={`${result.pop.toFixed(1)}%`} />
          <MetricRow label="Impl. Move" value={`$${result.impliedMove.toFixed(2)}`} />
          <MetricRow label="γ/θ Ratio" value={result.gammaThetaRatio.toFixed(4)} />
          <MetricRow label="ATM IV" value={`${(result.currentIV * 100).toFixed(1)}%`} />
          <MetricRow label="DTE" value={`${result.dte}d`} />
        </div>

        <Separator className="bg-zinc-800" />

        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-zinc-500">
          {result.details.shortPutStrike != null && (
            <span>Put strike: {result.details.shortPutStrike}</span>
          )}
          {result.details.shortCallStrike != null && (
            <span>Call strike: {result.details.shortCallStrike}</span>
          )}
          {result.details.volume != null && (
            <span>Vol: {result.details.volume.toLocaleString()}</span>
          )}
          {result.details.openInterest != null && (
            <span>OI: {result.details.openInterest.toLocaleString()}</span>
          )}
        </div>

        <p className="text-xs text-zinc-600">
          Exp: {result.expirationDate} · ${result.underlyingPrice.toFixed(2)}
        </p>
      </CardContent>
    </Card>
  )
}

// ─── Filter Pill ──────────────────────────────────────────────────────────────

function FilterPill({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'px-4 py-1.5 rounded-full text-sm font-medium transition-all',
        active
          ? 'bg-zinc-100 text-zinc-950 shadow'
          : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200'
      )}
    >
      {label}
    </button>
  )
}

// ─── Empty State ──────────────────────────────────────────────────────────────

function EmptyState({ hasScanned }: { hasScanned: boolean }) {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center text-zinc-500 gap-4">
      <Activity className="h-12 w-12 opacity-30" />
      <div>
        <p className="text-lg font-medium text-zinc-400">
          {hasScanned ? 'No opportunities found' : 'Ready to scan'}
        </p>
        <p className="text-sm mt-1">
          {hasScanned
            ? 'No tickers passed Tier 1 filters. Try again during market hours.'
            : 'Click "Scan Now" to screen the default watchlist for premium-selling setups.'}
        </p>
      </div>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ScannerPage() {
  const [scanning, setScanning] = useState(false)
  const [scanData, setScanData] = useState<ScanResponse | null>(null)
  const [activeFilter, setActiveFilter] = useState<StrategyType | 'ALL'>('ALL')
  const [scanError, setScanError] = useState<string | null>(null)

  const runScan = useCallback(async () => {
    setScanning(true)
    setScanError(null)
    try {
      const res = await fetch('/api/scan', { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.error ?? `HTTP ${res.status}`)
      }
      const data: ScanResponse = await res.json()
      setScanData(data)
      setActiveFilter('ALL')
    } catch (err) {
      setScanError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setScanning(false)
    }
  }, [])

  const filteredResults =
    scanData?.results.filter(
      (r) => activeFilter === 'ALL' || r.strategy === activeFilter
    ) ?? []

  const strategyCounts = scanData?.results.reduce<Record<string, number>>((acc, r) => {
    acc[r.strategy] = (acc[r.strategy] ?? 0) + 1
    return acc
  }, {})

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      {/* ── Header ── */}
      <header className="border-b border-zinc-800 bg-zinc-950/90 sticky top-0 z-10 backdrop-blur-sm">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-emerald-500/10 text-emerald-400">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight">Options Market Scanner</h1>
              <p className="text-xs text-zinc-500">Premium-selling opportunity screener</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {scanData && (
              <p className="text-xs text-zinc-500 hidden sm:block">
                Last scan: {new Date(scanData.scannedAt).toLocaleTimeString()}
                {' · '}
                {scanData.count} hit{scanData.count !== 1 ? 's' : ''}
              </p>
            )}
            <Button
              onClick={runScan}
              disabled={scanning}
              className="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold"
            >
              {scanning ? (
                <>
                  <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                  Scanning…
                </>
              ) : (
                <>
                  <RefreshCw className="h-4 w-4 mr-2" />
                  Scan Now
                </>
              )}
            </Button>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-8 space-y-6">
        {/* ── Error Banner ── */}
        {scanError && (
          <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-400">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {scanError}
          </div>
        )}

        {/* ── Scan errors (per-ticker) ── */}
        {(scanData?.errors.length ?? 0) > 0 && (
          <details className="rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3 text-xs text-zinc-500">
            <summary className="cursor-pointer font-medium text-zinc-400">
              {scanData!.errors.length} ticker{scanData!.errors.length !== 1 ? 's' : ''} failed
            </summary>
            <ul className="mt-2 space-y-1">
              {scanData!.errors.map((e) => (
                <li key={e.symbol}>
                  <span className="text-zinc-300">{e.symbol}</span>: {e.reason}
                </li>
              ))}
            </ul>
          </details>
        )}

        {/* ── Filter Pills ── */}
        {scanData && (
          <div className="flex flex-wrap gap-2">
            {ALL_STRATEGIES.map((s) => {
              const count = s === 'ALL' ? scanData.count : (strategyCounts?.[s] ?? 0)
              const label =
                s === 'ALL'
                  ? `All (${count})`
                  : `${STRATEGY_LABELS[s as StrategyType]}${count ? ` (${count})` : ''}`
              return (
                <FilterPill
                  key={s}
                  label={label}
                  active={activeFilter === s}
                  onClick={() => setActiveFilter(s)}
                />
              )
            })}
          </div>
        )}

        {/* ── Score Legend ── */}
        {scanData && (
          <div className="flex items-center gap-4 text-xs text-zinc-500">
            <span className="font-medium text-zinc-400">Score:</span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500" />
              ≥70 Strong
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-yellow-500" />
              40–69 Moderate
            </span>
            <span className="flex items-center gap-1.5">
              <span className="inline-block h-2.5 w-2.5 rounded-full bg-zinc-600" />
              &lt;40 Weak
            </span>
          </div>
        )}

        {/* ── Results Grid ── */}
        {filteredResults.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {filteredResults.map((r) => (
              <ResultCard key={r.symbol} result={r} />
            ))}
          </div>
        ) : (
          <EmptyState hasScanned={scanData !== null} />
        )}
      </main>

      {/* ── Footer ── */}
      <footer className="border-t border-zinc-800 mt-16 py-6 text-center text-xs text-zinc-600">
        Options data via Tradier · Not financial advice · Phase 1
      </footer>
    </div>
  )
}
