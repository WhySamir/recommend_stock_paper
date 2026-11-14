-- Market context, portfolio snapshot and notification summary for a finished
-- run, so a replayed (idempotent) response has the same shape as the original.

alter table suggestion_runs add column if not exists result jsonb not null default '{}'::jsonb;
