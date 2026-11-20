-- Run once in Supabase -> SQL Editor. Optional: without it, research is cached in memory + a local file.
create table if not exists coin_research (
  key        text primary key,          -- e.g. 'SOL:deep'
  symbol     text not null,
  mode       text not null,             -- 'quick' | 'deep'
  verdict    text,
  grade      text,
  score      numeric,
  report     jsonb not null,
  created_at bigint not null
);
create index if not exists coin_research_created_idx on coin_research (created_at desc);
-- Keep Row Level Security the same as your paper_trades table (if paper_trades works with your key, this works too).
