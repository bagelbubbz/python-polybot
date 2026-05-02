-- Options Market Scanner — Phase 1 schema

-- Enable UUID generation
create extension if not exists "pgcrypto";

-- ─── Tickers ─────────────────────────────────────────────────────────────────

create table if not exists tickers (
  id         uuid primary key default gen_random_uuid(),
  symbol     text not null unique,
  active     boolean not null default true,
  created_at timestamptz not null default now()
);

-- Seed default watchlist
insert into tickers (symbol) values
  ('AAPL'), ('MSFT'), ('NVDA'), ('AMZN'),
  ('SPY'),  ('QQQ'),  ('JPM'),  ('TSLA')
on conflict (symbol) do nothing;

-- ─── Scan Results ─────────────────────────────────────────────────────────────

create type strategy_type as enum (
  'IRON_CONDOR',
  'STRANGLE',
  'CASH_SECURED_PUT',
  'COVERED_CALL'
);

create table if not exists scan_results (
  id                  uuid primary key default gen_random_uuid(),
  ticker_id           uuid not null references tickers(id) on delete cascade,
  symbol              text not null,
  strategy            strategy_type not null,
  score               numeric(6, 2) not null,
  ivr                 numeric(6, 2) not null,   -- IV Rank 0-100
  pop                 numeric(6, 2) not null,   -- Probability of Profit 0-100
  implied_move        numeric(10, 4),            -- 1-SD move in dollars
  gamma_theta_ratio   numeric(10, 6),
  current_iv          numeric(8, 4) not null,   -- ATM IV as decimal (e.g. 0.35)
  underlying_price    numeric(10, 2) not null,
  expiration_date     date,
  dte                 integer,                  -- days to expiration
  details             jsonb default '{}',       -- raw greeks, strikes, spreads
  scanned_at          timestamptz not null default now()
);

create index if not exists scan_results_symbol_idx      on scan_results(symbol);
create index if not exists scan_results_strategy_idx    on scan_results(strategy);
create index if not exists scan_results_score_idx       on scan_results(score desc);
create index if not exists scan_results_scanned_at_idx  on scan_results(scanned_at desc);

-- Row-Level Security (enable but allow anon read for the UI)
alter table tickers      enable row level security;
alter table scan_results enable row level security;

create policy "public read tickers"
  on tickers for select using (true);

create policy "public read scan_results"
  on scan_results for select using (true);

-- Service role can insert/update/delete (used by the API route)
create policy "service insert tickers"
  on tickers for insert to service_role with check (true);

create policy "service insert scan_results"
  on scan_results for insert to service_role with check (true);

create policy "service delete scan_results"
  on scan_results for delete to service_role using (true);
