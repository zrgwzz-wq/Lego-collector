create table if not exists public.lego_kr_catalog (
  set_number text primary key,
  name_ko text,
  price_krw bigint,
  source text,
  source_url text,
  checked_at date,
  updated_at timestamptz default now()
);

alter table public.lego_kr_catalog enable row level security;
-- 서버의 service_role 키만 쓰므로 브라우저 공개 정책은 만들지 않습니다.
\n\ngrant usage on schema public to service_role;\ngrant select, insert, update, delete on table public.lego_kr_catalog to service_role;\n