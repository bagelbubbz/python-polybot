import type { TradierHistoricalDay } from './tradier'

// ─── Types ────────────────────────────────────────────────────────────────────

export interface IVRangeResult {
  ivr: number       // IV Rank 0-100
  iv52High: number  // highest realised/implied vol in past year (decimal)
  iv52Low: number   // lowest realised/implied vol in past year (decimal)
}

// ─── IV Rank ──────────────────────────────────────────────────────────────────

/**
 * IV Rank = (currentIV − 52w_low) / (52w_high − 52w_low) × 100
 * Clamped to [0, 100].
 */
export function calculateIVR(currentIV: number, iv52High: number, iv52Low: number): number {
  const range = iv52High - iv52Low
  if (range <= 0) return 0
  return Math.max(0, Math.min(100, ((currentIV - iv52Low) / range) * 100))
}

/**
 * Derive historical-volatility range from daily price history.
 * Returns rolling 30-day HV values; caller uses min/max as IV52Low/High proxy.
 */
export function deriveIVRangeFromHistory(
  history: TradierHistoricalDay[],
  currentIV: number
): IVRangeResult {
  if (history.length < 30) {
    return { ivr: 50, iv52High: currentIV * 1.5, iv52Low: currentIV * 0.5 }
  }

  // Log daily returns
  const returns: number[] = []
  for (let i = 1; i < history.length; i++) {
    returns.push(Math.log(history[i].close / history[i - 1].close))
  }

  // Rolling 30-day annualised HV windows
  const hvWindows: number[] = []
  const windowSize = 30
  for (let i = windowSize; i <= returns.length; i++) {
    const slice = returns.slice(i - windowSize, i)
    const mean = slice.reduce((a, b) => a + b, 0) / windowSize
    const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / (windowSize - 1)
    hvWindows.push(Math.sqrt(variance * 252))
  }

  const iv52High = Math.max(...hvWindows, currentIV)
  const iv52Low = Math.min(...hvWindows, currentIV)
  const ivr = calculateIVR(currentIV, iv52High, iv52Low)

  return { ivr, iv52High, iv52Low }
}

// ─── Probability of Profit ────────────────────────────────────────────────────

/**
 * Simple delta-based PoP approximation.
 * For a short option: PoP ≈ (1 − |delta|) × 100
 */
export function calculatePoP(delta: number): number {
  return Math.max(0, Math.min(100, (1 - Math.abs(delta)) * 100))
}

/**
 * Iron condor PoP: uses both short-put delta and short-call delta.
 * PoP ≈ 1 − P(below short put) − P(above short call)
 */
export function calculateIronCondorPoP(shortPutDelta: number, shortCallDelta: number): number {
  const pBelow = Math.abs(shortPutDelta)
  const pAbove = 1 - Math.abs(shortCallDelta)
  return Math.max(0, Math.min(100, (1 - pBelow - pAbove) * 100))
}

// ─── Gamma/Theta Ratio ────────────────────────────────────────────────────────

/**
 * Lower is better for premium sellers: less gamma risk per $1 of daily theta decay.
 * Returns absolute ratio (both inputs may be signed — theta is typically negative).
 */
export function calculateGammaThetaRatio(gamma: number, theta: number): number {
  if (theta === 0) return Infinity
  return Math.abs(gamma / theta)
}

// ─── Implied Move ─────────────────────────────────────────────────────────────

/**
 * 1-standard-deviation expected move by expiration:
 *   ImpliedMove = underlyingPrice × IV × √(DTE / 365)
 * Returns dollar amount.
 */
export function calculateImpliedMove(
  underlyingPrice: number,
  iv: number,
  daysToExpiry: number
): number {
  return underlyingPrice * iv * Math.sqrt(daysToExpiry / 365)
}

// ─── Composite Score ──────────────────────────────────────────────────────────

/**
 * Composite score 0-100 for ranking scan candidates.
 *
 * Weights:
 *   40% IVR         — elevated IV = richer premium
 *   40% PoP         — probability of profit
 *   20% GTR         — gamma/theta efficiency (lower ratio is better)
 *
 * GTR is normalised: ratio < 0.05 → 20 pts, ratio > 0.5 → 0 pts.
 */
export function calculateScore(ivr: number, pop: number, gammaThetaRatio: number): number {
  const ivrScore = Math.min(ivr, 100) * 0.4
  const popScore = Math.min(pop, 100) * 0.4

  const clampedGTR = isFinite(gammaThetaRatio)
    ? Math.min(gammaThetaRatio, 0.5)
    : 0.5
  const gtrScore = (1 - clampedGTR / 0.5) * 20

  return Math.min(100, Math.max(0, ivrScore + popScore + gtrScore))
}

/** Badge colour bucket for the UI. */
export function scoreTier(score: number): 'green' | 'yellow' | 'grey' {
  if (score >= 70) return 'green'
  if (score >= 40) return 'yellow'
  return 'grey'
}
