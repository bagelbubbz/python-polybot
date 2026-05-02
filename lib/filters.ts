import type { TradierOption } from './tradier'
import {
  calculatePoP,
  calculateIronCondorPoP,
  calculateGammaThetaRatio,
  calculateImpliedMove,
  calculateScore,
} from './metrics'

// ─── Shared types ─────────────────────────────────────────────────────────────

export type StrategyType = 'IRON_CONDOR' | 'STRANGLE' | 'CASH_SECURED_PUT' | 'COVERED_CALL'

export interface ScanResult {
  symbol: string
  strategy: StrategyType
  score: number
  ivr: number
  pop: number
  impliedMove: number
  gammaThetaRatio: number
  currentIV: number
  underlyingPrice: number
  expirationDate: string
  dte: number
  details: {
    shortCallStrike?: number
    shortPutStrike?: number
    shortCallDelta?: number
    shortPutDelta?: number
    atmCallIV?: number
    atmPutIV?: number
    bidAskSpread?: number
    openInterest?: number
    volume?: number
  }
}

// ─── Tier 1 Filters ───────────────────────────────────────────────────────────

interface Tier1Input {
  ivr: number
  volume: number             // options volume at ATM strike
  openInterest: number       // open interest at ATM strike
  bidAskSpreadPct: number    // (ask - bid) / mid, 0-1
  underlyingPrice: number
}

/**
 * Returns true when a ticker passes minimum liquidity and volatility gates.
 *
 * Thresholds (conservative Phase 1):
 *   IVR          ≥ 30   — must be in at least the 30th IV percentile
 *   volume       ≥ 100  — options must have today's activity
 *   openInterest ≥ 500  — enough liquidity to fill orders
 *   bid-ask spread ≤ 15% of mid
 *   underlyingPrice ≥ 10 — avoid micro-caps / penny stocks
 */
export function applyTier1Filters(input: Tier1Input): boolean {
  return (
    input.ivr >= 30 &&
    input.volume >= 100 &&
    input.openInterest >= 500 &&
    input.bidAskSpreadPct <= 0.15 &&
    input.underlyingPrice >= 10
  )
}

// ─── Strategy Selection ───────────────────────────────────────────────────────

/**
 * Picks the most appropriate premium-selling strategy given the IVR level.
 *
 *   IVR ≥ 50  → Iron Condor   (highest IV: sell both sides with wing protection)
 *   IVR ≥ 40  → Strangle      (elevated IV: sell both sides naked)
 *   IVR ≥ 30, put skew        → Cash-Secured Put
 *   IVR ≥ 30, call skew       → Covered Call
 */
export function selectStrategy(
  ivr: number,
  atmCallIV: number,
  atmPutIV: number
): StrategyType {
  if (ivr >= 50) return 'IRON_CONDOR'
  if (ivr >= 40) return 'STRANGLE'
  // Use skew to decide directional bias
  if (atmPutIV >= atmCallIV) return 'CASH_SECURED_PUT'
  return 'COVERED_CALL'
}

// ─── Build ScanResult ─────────────────────────────────────────────────────────

/**
 * Assembles a ScanResult from raw Tradier data + pre-computed IVR.
 * Returns null if the ticker doesn't pass Tier 1 filters.
 */
export function buildScanResult(params: {
  symbol: string
  chain: TradierOption[]
  atmCall: TradierOption
  atmPut: TradierOption
  underlyingPrice: number
  ivr: number
  currentIV: number
  expirationDate: string
  dte: number
}): ScanResult | null {
  const { symbol, atmCall, atmPut, underlyingPrice, ivr, currentIV, expirationDate, dte } = params

  const callGreeks = atmCall.greeks
  const putGreeks = atmPut.greeks
  if (!callGreeks || !putGreeks) return null

  const atmCallIV = callGreeks.mid_iv
  const atmPutIV = putGreeks.mid_iv

  // Bid-ask spread as % of mid for each leg
  const callMid = (atmCall.bid + atmCall.ask) / 2
  const putMid = (atmPut.bid + atmPut.ask) / 2
  const callSpreadPct = callMid > 0 ? (atmCall.ask - atmCall.bid) / callMid : 1
  const putSpreadPct = putMid > 0 ? (atmPut.ask - atmPut.bid) / putMid : 1
  const bidAskSpreadPct = (callSpreadPct + putSpreadPct) / 2

  const volume = (atmCall.volume ?? 0) + (atmPut.volume ?? 0)
  const openInterest = (atmCall.open_interest ?? 0) + (atmPut.open_interest ?? 0)

  const passes = applyTier1Filters({
    ivr,
    volume,
    openInterest,
    bidAskSpreadPct,
    underlyingPrice,
  })
  if (!passes) return null

  const strategy = selectStrategy(ivr, atmCallIV, atmPutIV)

  // Greeks aggregated across the chosen structure
  const gamma = (callGreeks.gamma + putGreeks.gamma) / 2
  const theta = (callGreeks.theta + putGreeks.theta) / 2
  const gammaThetaRatio = calculateGammaThetaRatio(gamma, theta)
  const impliedMove = calculateImpliedMove(underlyingPrice, currentIV, dte)

  let pop: number
  if (strategy === 'IRON_CONDOR') {
    pop = calculateIronCondorPoP(putGreeks.delta, callGreeks.delta)
  } else if (strategy === 'STRANGLE') {
    pop = calculateIronCondorPoP(putGreeks.delta, callGreeks.delta)
  } else if (strategy === 'CASH_SECURED_PUT') {
    pop = calculatePoP(putGreeks.delta)
  } else {
    pop = calculatePoP(callGreeks.delta)
  }

  const score = calculateScore(ivr, pop, gammaThetaRatio)

  return {
    symbol,
    strategy,
    score: Math.round(score * 100) / 100,
    ivr: Math.round(ivr * 100) / 100,
    pop: Math.round(pop * 100) / 100,
    impliedMove: Math.round(impliedMove * 100) / 100,
    gammaThetaRatio: Math.round(gammaThetaRatio * 10000) / 10000,
    currentIV: Math.round(currentIV * 10000) / 10000,
    underlyingPrice,
    expirationDate,
    dte,
    details: {
      shortCallStrike: atmCall.strike,
      shortPutStrike: atmPut.strike,
      shortCallDelta: Math.round(callGreeks.delta * 1000) / 1000,
      shortPutDelta: Math.round(putGreeks.delta * 1000) / 1000,
      atmCallIV: Math.round(atmCallIV * 10000) / 10000,
      atmPutIV: Math.round(atmPutIV * 10000) / 10000,
      bidAskSpread: Math.round(bidAskSpreadPct * 10000) / 10000,
      openInterest,
      volume,
    },
  }
}
