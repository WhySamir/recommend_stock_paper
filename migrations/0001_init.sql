-- Initial Neon Postgres schema.
--
-- The engine's former JSON files map onto these tables:
--   trader/scan.json, plans.json, orders.json, timemachine.json -> state_snapshots
--   trader/alerts.json                                          -> alerts
--   trader/simulations/<id>/result.json                         -> simulations
-- The suggestions feature (watchlists, runs, suggestions, notification log)
-- gets its own tables here so later phases do not need another migration.

create table if not exists state_snapshots (
    kind        text primary key,
    payload     jsonb not null,
    updated_at  timestamptz not null default now()
);

create table if not exists alerts (
    id           bigserial primary key,
    symbol       text not null,
    kind         text not null,
    level        double precision,
    note         text not null default '',
    since        text not null default '',
    sessions     integer not null default 0,
    triggered    boolean not null default false,
    triggered_on text not null default '',
    created_at   timestamptz not null default now()
);

create index if not exists alerts_symbol_idx on alerts (symbol);

create table if not exists simulations (
    id          text primary key,
    symbol      text not null,
    action      text not null,
    shares      integer not null,
    days        integer not null,
    payload     jsonb not null,
    created_at  timestamptz not null default now()
);

create index if not exists simulations_created_idx on simulations (created_at desc);

create table if not exists watchlists (
    id          bigserial primary key,
    name        text not null unique,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table if not exists watchlist_symbols (
    watchlist_id bigint not null references watchlists(id) on delete cascade,
    symbol       text not null,
    created_at   timestamptz not null default now(),
    primary key (watchlist_id, symbol)
);

create table if not exists suggestion_runs (
    id           text primary key,
    created_at   timestamptz not null default now(),
    finished_at  timestamptz,
    symbols      text[] not null default '{}',
    params       jsonb not null default '{}'::jsonb,
    status       text not null default 'running',
    error        text
);

create index if not exists suggestion_runs_created_idx on suggestion_runs (created_at desc);

create table if not exists suggestions (
    id          bigserial primary key,
    run_id      text not null references suggestion_runs(id) on delete cascade,
    symbol      text not null,
    action      text not null check (action in ('BUY', 'SELL', 'HOLD')),
    quantity    integer not null default 0,
    confidence  double precision not null default 0,
    price       double precision,
    reasoning   jsonb not null default '[]'::jsonb,
    signals     jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);

create index if not exists suggestions_run_idx on suggestions (run_id);

create table if not exists notification_log (
    id         bigserial primary key,
    run_id     text references suggestion_runs(id) on delete set null,
    channel    text not null,
    status     text not null,
    sent_at    timestamptz not null default now(),
    error      text
);

create index if not exists notification_log_run_idx on notification_log (run_id);
