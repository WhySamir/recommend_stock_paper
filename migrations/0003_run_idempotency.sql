-- Run-level idempotency for the cron endpoint, so a retried request returns the
-- existing run instead of generating (and notifying) a second time.

alter table suggestion_runs add column if not exists idempotency_key text;
alter table suggestion_runs add column if not exists notify_requested boolean not null default false;

create unique index if not exists suggestion_runs_idem_idx
    on suggestion_runs (idempotency_key)
    where idempotency_key is not null;
