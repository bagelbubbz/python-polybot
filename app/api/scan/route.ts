import { NextResponse } from 'next/server'
import { createServiceClient } from '@/lib/supabase'
import {
  getQuote,
  getExpirations,
  getOptionsChain,
  getHistoricalPrices,
  selectExpiration,
  findATMOptions,
} from '@/lib/tradier'
import { deriveIVRangeFromHistory } from '@/lib/metrics'
import { buildScanResult } from '@/lib/filters'
import type { ScanResult } from '@/lib/filters'

// ─── POST /api/scan ───────────────────────────────────────────────────────────
// Scans all active watchlist tickers and persists results to Supabase.
// Returns the scan results sorted by score descending.

export async function POST() {
  const db = createServiceClient()

  // 1. Fetch active tickers
  const { data: tickers, error: tickersError } = await db
    .from('tickers')
    .select('id, symbol')
    .eq('active', true)

  if (tickersError) {
    return NextResponse.json({ error: tickersError.message }, { status: 500 })
  }

  const results: ScanResult[] = []
  const errors: { symbol: string; reason: string }[] = []

  // 2. Process each ticker (sequentially to avoid rate limiting)
  for (const ticker of tickers ?? []) {
    try {
      const result = await scanTicker(ticker.symbol)
      if (result) results.push(result)
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err)
      errors.push({ symbol: ticker.symbol, reason })
      console.error(`[scan] ${ticker.symbol}:`, reason)
    }
  }

  // 3. Persist results — clear old rows first then bulk insert
  if (results.length > 0) {
    const tickerMap = Object.fromEntries((tickers ?? []).map((t) => [t.symbol, t.id]))

    await db.from('scan_results').delete().neq('id', '00000000-0000-0000-0000-000000000000')

    const rows = results.map((r) => ({
      ticker_id: tickerMap[r.symbol],
      symbol: r.symbol,
      strategy: r.strategy,
      score: r.score,
      ivr: r.ivr,
      pop: r.pop,
      implied_move: r.impliedMove,
      gamma_theta_ratio: r.gammaThetaRatio,
      current_iv: r.currentIV,
      underlying_price: r.underlyingPrice,
      expiration_date: r.expirationDate,
      dte: r.dte,
      details: r.details,
    }))

    const { error: insertError } = await db.from('scan_results').insert(rows)
    if (insertError) console.error('[scan] insert error:', insertError.message)
  }

  // 4. Return sorted results
  results.sort((a, b) => b.score - a.score)

  return NextResponse.json({
    scannedAt: new Date().toISOString(),
    count: results.length,
    results,
    errors,
  })
}

// ─── Per-ticker scan logic ────────────────────────────────────────────────────

async function scanTicker(symbol: string): Promise<ScanResult | null> {
  // Quote + expirations in parallel
  const [quote, expirations] = await Promise.all([
    getQuote(symbol),
    getExpirations(symbol),
  ])

  if (!quote) throw new Error('no quote returned')
  if (!expirations.length) throw new Error('no expirations returned')

  const expiry = selectExpiration(expirations)
  if (!expiry) throw new Error('no suitable expiration in 15-45 DTE window')

  const today = Date.now()
  const dte = Math.round((new Date(expiry.date).getTime() - today) / 86_400_000)

  // Options chain + price history in parallel
  const [chain, history] = await Promise.all([
    getOptionsChain(symbol, expiry.date),
    getHistoricalPrices(symbol, 252),
  ])

  if (!chain.length) throw new Error('empty options chain')

  const underlyingPrice = quote.last ?? (quote.bid + quote.ask) / 2
  const { call: atmCall, put: atmPut } = findATMOptions(chain, underlyingPrice)
  if (!atmCall || !atmPut) throw new Error('could not find ATM options')

  // Current IV: average of ATM call and put mid-IV
  const atmCallIV = atmCall.greeks?.mid_iv ?? 0
  const atmPutIV = atmPut.greeks?.mid_iv ?? 0
  const currentIV = (atmCallIV + atmPutIV) / 2
  if (currentIV <= 0) throw new Error('invalid ATM IV (≤ 0)')

  const { ivr } = deriveIVRangeFromHistory(history, currentIV)

  return buildScanResult({
    symbol,
    chain,
    atmCall,
    atmPut,
    underlyingPrice,
    ivr,
    currentIV,
    expirationDate: expiry.date,
    dte,
  })
}

// ─── GET /api/scan — return latest stored results ─────────────────────────────

export async function GET() {
  const db = createServiceClient()

  const { data, error } = await db
    .from('scan_results')
    .select('*')
    .order('score', { ascending: false })
    .limit(50)

  if (error) return NextResponse.json({ error: error.message }, { status: 500 })

  return NextResponse.json({
    results: data ?? [],
    count: data?.length ?? 0,
  })
}
