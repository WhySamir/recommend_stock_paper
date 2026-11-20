-- Saved future-simulation paper trades with their due-date settlements.

create table if not exists future_simulations (
    id text primary key,
    symbol text not null,
    action text not null,
    shares integer not null,
    price double precision not null,
    created_date text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);

create index if not exists future_simulations_created_at_idx
    on future_simulations (created_at desc);
