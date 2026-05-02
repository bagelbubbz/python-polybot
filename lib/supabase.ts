import { createClient } from '@supabase/supabase-js'

export function createAnonClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
  )
}

export function createServiceClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,
    { auth: { persistSession: false } }
  )
}

// ─── DB row types ─────────────────────────────────────────────────────────────

export interface TickerRow {
  id: string
  symbol: string
  active: boolean
  created_at: string
}

export interface ScanResultRow {
  id: string
  ticker_id: string
  symbol: string
  strategy: 'IRON_CONDOR' | 'STRANGLE' | 'CASH_SECURED_PUT' | 'COVERED_CALL'
  score: number
  ivr: number
  pop: number
  implied_move: number | null
  gamma_theta_ratio: number | null
  current_iv: number
  underlying_price: number
  expiration_date: string | null
  dte: number | null
  details: Record<string, unknown>
  scanned_at: string
}
