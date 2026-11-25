-- 풋볼 도감 디씨 스타일 댓글 (Supabase SQL Editor에서 한 번 실행)
-- 닉네임(기본 ㅇㅇ) + 비밀번호(삭제용), IP 앞 두 자리 표시, 추천/비추/신고, 금지어, 30초 도배 제한
-- 테이블은 직접 못 건드리고, 아래 함수(rpc)로만 쓰기·삭제·추천이 가능함

create extension if not exists pgcrypto with schema extensions;

create table if not exists public.comments (
  id bigint generated always as identity primary key,
  player_no int not null check (player_no between 1 and 99999),
  nick text not null default 'ㅇㅇ',
  body text not null,
  pw_hash text not null,
  ip_prefix text not null default '',
  ip_hash text not null default '',
  up int not null default 0,
  down int not null default 0,
  reports int not null default 0,
  hidden boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists comments_player_idx on public.comments (player_no, created_at desc);

create table if not exists public.comment_votes (
  comment_id bigint not null references public.comments (id) on delete cascade,
  ip_hash text not null,
  kind text not null check (kind in ('up', 'down', 'report')),
  created_at timestamptz not null default now(),
  primary key (comment_id, ip_hash, kind)
);

create table if not exists public.banned_words (word text primary key);
insert into public.banned_words (word) values
  ('씨발'), ('시발'), ('씨팔'), ('ㅅㅂ'), ('ㅆㅂ'), ('병신'), ('ㅂㅅ'), ('ㅄ'), ('개새끼'), ('새끼'),
  ('좆'), ('지랄'), ('니애미'), ('느금'), ('애미'), ('애비'), ('엠창'), ('창녀'),
  ('짱깨'), ('쪽바리'), ('깜둥이'), ('흑돼지'), ('한남충'), ('김치녀')
on conflict do nothing;

-- 테이블은 모두 잠그고(RLS on, 정책 없음) 읽기는 아래 공개용 뷰로만
alter table public.comments enable row level security;
alter table public.comment_votes enable row level security;
alter table public.banned_words enable row level security;
revoke all on public.comments, public.comment_votes, public.banned_words from anon, authenticated;

create or replace view public.comments_public as
  select id, player_no, nick, body, ip_prefix, up, down, created_at
  from public.comments
  where not hidden;
grant select on public.comments_public to anon, authenticated;

-- 요청 보낸 사람의 IP (Supabase가 넣어 주는 헤더에서)
create or replace function public._client_ip() returns text
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.headers', true)::json ->> 'cf-connecting-ip', ''),
    nullif(split_part(current_setting('request.headers', true)::json ->> 'x-forwarded-for', ',', 1), ''),
    'unknown')
$$;

-- 디씨처럼 IP 앞 두 자리만 (예: 211.234)
create or replace function public._ip_prefix(ip text) returns text
language sql immutable as $$
  select case
    when ip ~ '^\d+\.\d+\.' then split_part(ip, '.', 1) || '.' || split_part(ip, '.', 2)
    when position(':' in ip) > 0 then split_part(ip, ':', 1) || ':' || split_part(ip, ':', 2)
    else '' end
$$;

create or replace function public._ip_hash(ip text) returns text
language sql stable set search_path = public, extensions as $$
  select encode(extensions.digest('player-dex:' || ip, 'sha256'), 'hex')
$$;

-- 댓글 쓰기
create or replace function public.post_comment(p_player int, p_nick text, p_pw text, p_body text)
returns bigint
language plpgsql security definer set search_path = public, extensions as $$
declare
  v_ip text := _client_ip();
  v_hash text := _ip_hash(v_ip);
  v_nick text := coalesce(nullif(trim(p_nick), ''), 'ㅇㅇ');
  v_body text := trim(coalesce(p_body, ''));
  v_flat text;
  v_id bigint;
begin
  if p_player is null or p_player < 1 or p_player > 99999 then raise exception '선수 번호가 올바르지 않습니다'; end if;
  if char_length(v_body) = 0 then raise exception '내용을 입력해 주십시오'; end if;
  if char_length(v_body) > 300 then raise exception '댓글은 300자까지 쓸 수 있습니다'; end if;
  if char_length(v_nick) > 12 then raise exception '닉네임은 12자까지입니다'; end if;
  if char_length(coalesce(p_pw, '')) < 4 then raise exception '비밀번호는 4자 이상입니다'; end if;
  v_flat := lower(regexp_replace(v_nick || v_body, '[\s\.\-_~!*]+', '', 'g'));
  if exists (select 1 from banned_words b where position(b.word in v_flat) > 0) then
    raise exception '금지어가 포함되어 있습니다. 표현을 바꿔 주십시오';
  end if;
  if exists (select 1 from comments c where c.ip_hash = v_hash and c.created_at > now() - interval '30 seconds') then
    raise exception '도배 방지: 30초 후에 다시 작성해 주십시오';
  end if;
  insert into comments (player_no, nick, body, pw_hash, ip_prefix, ip_hash)
  values (p_player, v_nick, v_body, extensions.crypt(p_pw, extensions.gen_salt('bf')), _ip_prefix(v_ip), v_hash)
  returning id into v_id;
  return v_id;
end $$;

-- 비밀번호로 내 댓글 삭제
create or replace function public.delete_comment(p_id bigint, p_pw text)
returns boolean
language plpgsql security definer set search_path = public, extensions as $$
declare v_hash text;
begin
  select pw_hash into v_hash from comments where id = p_id;
  if v_hash is null then raise exception '이미 삭제된 댓글입니다'; end if;
  if extensions.crypt(coalesce(p_pw, ''), v_hash) <> v_hash then raise exception '비밀번호가 일치하지 않습니다'; end if;
  delete from comments where id = p_id;
  return true;
end $$;

-- 추천(up) / 비추천(down) / 신고(report). IP당 댓글 하나에 종류별 한 번. 신고 3번이면 숨김
create or replace function public.vote_comment(p_id bigint, p_kind text)
returns json
language plpgsql security definer set search_path = public, extensions as $$
declare
  v_hash text := _ip_hash(_client_ip());
  v_new int;
  r record;
begin
  if p_kind not in ('up', 'down', 'report') then raise exception '잘못된 요청입니다'; end if;
  insert into comment_votes (comment_id, ip_hash, kind) values (p_id, v_hash, p_kind)
  on conflict do nothing;
  get diagnostics v_new = row_count;
  if v_new = 0 then raise exception '이미 참여했습니다'; end if;
  update comments set
    up = up + (p_kind = 'up')::int,
    down = down + (p_kind = 'down')::int,
    reports = reports + (p_kind = 'report')::int,
    hidden = hidden or (p_kind = 'report' and reports + 1 >= 3)
  where id = p_id
  returning up, down, hidden into r;
  return json_build_object('up', r.up, 'down', r.down, 'hidden', r.hidden);
end $$;

revoke all on function public._client_ip(), public._ip_prefix(text), public._ip_hash(text) from anon, authenticated, public;
grant execute on function public.post_comment(int, text, text, text) to anon, authenticated;
grant execute on function public.delete_comment(bigint, text) to anon, authenticated;
grant execute on function public.vote_comment(bigint, text) to anon, authenticated;
