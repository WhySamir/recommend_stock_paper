-- Paper positions the suggestion engine sizes against.
-- Cash stays in config `account.cash` (the same number `budget` uses).

create table if not exists positions (
    symbol        text primary key,
    quantity      integer not null default 0 check (quantity >= 0),
    avg_price     double precision not null default 0,
    realized_pnl  double precision not null default 0,
    opened_at     timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
