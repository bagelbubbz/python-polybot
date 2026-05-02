// Tradier REST API client for options chains and price history

const BASE_URL = process.env.TRADIER_BASE_URL ?? 'https://api.tradier.com/v1'
const TOKEN = process.env.TRADIER_API_TOKEN ?? ''

function tradierHeaders() {
  return {
    Authorization: `Bearer ${TOKEN}`,
    Accept: 'application/json',
  }
}

async function get<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const url = new URL(`${BASE_URL}${path}`)
  Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v))

  const res = await fetch(url.toString(), { headers: tradierHeaders(), next: { revalidate: 0 } })
  if (!res.ok) {
    throw new Error(`Tradier ${path} → HTTP ${res.status}: ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

// ─── Types ────────────────────────────────────────────────────────────────────

export interface TradierQuote {
  symbol: string
  last: number
  bid: number
  ask: number
  volume: number
  average_volume: number
  week_52_high: number
  week_52_low: number
  close: number
}

export interface TradierOption {
  symbol: string
  description: string
  strike: number
  expiration_date: string
  option_type: 'call' | 'put'
  last: number | null
  bid: number
  ask: number
  volume: number
  open_interest: number
  greeks: {
    delta: number
    gamma: number
    theta: number
    vega: number
    mid_iv: number
    ask_iv: number
    bid_iv: number
    smv_vol: number
  } | null
}

export interface TradierHistoricalDay {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface OptionsExpiration {
  date: string
  expiration_type: string
}

// ─── API methods ──────────────────────────────────────────────────────────────

export async function getQuote(symbol: string): Promise<TradierQuote | null> {
  const data = await get<{ quotes: { quote: TradierQuote | TradierQuote[] } }>(
    '/markets/quotes',
    { symbols: symbol, greeks: 'false' }
  )
  const quote = data.quotes?.quote
  if (!quote) return null
  return Array.isArray(quote) ? quote[0] : quote
}

export async function getExpirations(symbol: string): Promise<OptionsExpiration[]> {
  const data = await get<{ expirations: { date: string[] | null } }>(
    '/markets/options/expirations',
    { symbol, includeAllRoots: 'true', strikes: 'false' }
  )
  const dates = data.expirations?.date ?? []
  return dates.map((d) => ({ date: d, expiration_type: 'standard' }))
}

export async function getOptionsChain(
  symbol: string,
  expiration: string
): Promise<TradierOption[]> {
  const data = await get<{ options: { option: TradierOption | TradierOption[] | null } }>(
    '/markets/options/chains',
    { symbol, expiration, greeks: 'true' }
  )
  const options = data.options?.option
  if (!options) return []
  return Array.isArray(options) ? options : [options]
}

export async function getHistoricalPrices(
  symbol: string,
  days = 252
): Promise<TradierHistoricalDay[]> {
  const end = new Date()
  const start = new Date()
  start.setDate(end.getDate() - days)

  const fmt = (d: Date) => d.toISOString().split('T')[0]

  const data = await get<{ history: { day: TradierHistoricalDay | TradierHistoricalDay[] | null } }>(
    '/markets/history',
    { symbol, interval: 'daily', start: fmt(start), end: fmt(end) }
  )
  const days_ = data.history?.day
  if (!days_) return []
  return Array.isArray(days_) ? days_ : [days_]
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Returns the nearest expiration 21–45 DTE (standard monthly preferred). */
export function selectExpiration(expirations: OptionsExpiration[]): OptionsExpiration | null {
  const today = Date.now()
  const inRange = expirations.filter((e) => {
    const dte = (new Date(e.date).getTime() - today) / 86_400_000
    return dte >= 21 && dte <= 45
  })
  if (inRange.length === 0) {
    const nearest = expirations.find((e) => {
      const dte = (new Date(e.date).getTime() - today) / 86_400_000
      return dte >= 15
    })
    return nearest ?? null
  }
  return inRange[0]
}

/** Returns the ATM straddle (closest call + put to underlying price). */
export function findATMOptions(
  chain: TradierOption[],
  underlyingPrice: number
): { call: TradierOption | null; put: TradierOption | null } {
  const calls = chain.filter((o) => o.option_type === 'call' && o.greeks?.mid_iv)
  const puts = chain.filter((o) => o.option_type === 'put' && o.greeks?.mid_iv)

  const closest = (opts: TradierOption[]) =>
    opts.reduce<TradierOption | null>((best, o) => {
      if (!best) return o
      return Math.abs(o.strike - underlyingPrice) < Math.abs(best.strike - underlyingPrice)
        ? o
        : best
    }, null)

  return { call: closest(calls), put: closest(puts) }
}
