-- GhostLoc : abonnements, packs d'heures et décompte du temps de localisation simulée.
-- Toutes les écritures passent par les fonctions SECURITY DEFINER ci-dessous ou par le
-- webhook Stripe (service role). Les clients n'ont qu'un accès en lecture à leurs lignes.

create table if not exists public.gl_customers (
  user_id uuid primary key references auth.users (id) on delete cascade,
  stripe_customer_id text unique not null,
  created_at timestamptz not null default now()
);

create table if not exists public.gl_subscriptions (
  user_id uuid primary key references auth.users (id) on delete cascade,
  stripe_subscription_id text unique not null,
  plan text not null check (plan in ('essentiel', 'illimite')),
  status text not null,
  period_start timestamptz not null,
  period_end timestamptz not null,
  cancel_at_period_end boolean not null default false,
  updated_at timestamptz not null default now()
);

create table if not exists public.gl_hour_packs (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  seconds integer not null check (seconds > 0),
  -- Un pack ajoute des heures à la période d'abonnement en cours et expire avec elle.
  period_start timestamptz not null,
  stripe_session_id text unique not null,
  created_at timestamptz not null default now()
);

create table if not exists public.gl_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  device text not null,
  period_start timestamptz not null,
  started_at timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  ended_at timestamptz,
  seconds integer not null default 0
);
create index if not exists gl_sessions_user_period on public.gl_sessions (user_id, period_start);
create index if not exists gl_hour_packs_user_period on public.gl_hour_packs (user_id, period_start);

alter table public.gl_customers enable row level security;
alter table public.gl_subscriptions enable row level security;
alter table public.gl_hour_packs enable row level security;
alter table public.gl_sessions enable row level security;

drop policy if exists gl_customers_read on public.gl_customers;
create policy gl_customers_read on public.gl_customers for select to authenticated using ((select auth.uid()) = user_id);
drop policy if exists gl_subscriptions_read on public.gl_subscriptions;
create policy gl_subscriptions_read on public.gl_subscriptions for select to authenticated using ((select auth.uid()) = user_id);
drop policy if exists gl_hour_packs_read on public.gl_hour_packs;
create policy gl_hour_packs_read on public.gl_hour_packs for select to authenticated using ((select auth.uid()) = user_id);
drop policy if exists gl_sessions_read on public.gl_sessions;
create policy gl_sessions_read on public.gl_sessions for select to authenticated using ((select auth.uid()) = user_id);

-- Quota inclus dans l'Essentiel : 48 h par période payée.
create or replace function public.gl_quota_seconds(p_plan text)
returns integer language sql immutable set search_path = public as $$
  select case p_plan when 'essentiel' then 48 * 3600 else null end
$$;

-- Droits actuels d'un utilisateur. remaining_seconds est null pour l'Illimité.
create or replace function public.gl_entitlement_for(p_user uuid)
returns jsonb
language plpgsql stable security definer set search_path = public
as $$
declare
  s public.gl_subscriptions;
  v_active boolean;
  v_quota integer;
  v_packs integer := 0;
  v_used integer := 0;
begin
  select * into s from public.gl_subscriptions where user_id = p_user;
  if not found then
    return jsonb_build_object('plan', null, 'active', false, 'routes', false,
      'quota_seconds', 0, 'pack_seconds', 0, 'used_seconds', 0, 'remaining_seconds', 0);
  end if;

  v_active := s.status in ('active', 'trialing') and s.period_end > now();
  v_quota := public.gl_quota_seconds(s.plan);
  select coalesce(sum(seconds), 0) into v_packs
    from public.gl_hour_packs where user_id = p_user and period_start = s.period_start;
  select coalesce(sum(seconds), 0) into v_used
    from public.gl_sessions where user_id = p_user and period_start = s.period_start;

  return jsonb_build_object(
    'plan', s.plan,
    'status', s.status,
    'active', v_active,
    'routes', v_active and s.plan = 'illimite',
    'period_start', s.period_start,
    'period_end', s.period_end,
    'cancel_at_period_end', s.cancel_at_period_end,
    'quota_seconds', v_quota,
    'pack_seconds', v_packs,
    'used_seconds', v_used,
    'remaining_seconds', case
      when not v_active then 0
      when v_quota is null then null
      else greatest(v_quota + v_packs - v_used, 0) end
  );
end $$;

create or replace function public.gl_entitlement()
returns jsonb language sql stable security definer set search_path = public
as $$ select public.gl_entitlement_for(auth.uid()) $$;

-- Ajoute au compteur le temps écoulé depuis le dernier signe de vie (120 s max,
-- pour ne pas facturer une coupure réseau ou un PC mis en veille).
create or replace function public.gl_session_tick(p_session uuid, p_end boolean)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  ses public.gl_sessions;
  ent jsonb;
  v_was_ended boolean;
begin
  select * into ses from public.gl_sessions
    where id = p_session and user_id = auth.uid() for update;
  if not found then raise exception 'session_not_found' using errcode = 'P0002'; end if;
  v_was_ended := ses.ended_at is not null;

  if not v_was_ended then
    update public.gl_sessions
      set seconds = seconds + least(greatest(extract(epoch from now() - last_seen)::integer, 0), 120),
          last_seen = now(),
          ended_at = case when p_end then now() else null end
      where id = p_session
      returning * into ses;
  end if;

  ent := public.gl_entitlement_for(auth.uid());

  -- Renouvellement pendant une session : le temps déjà passé reste sur l'ancienne
  -- période, la suite est décomptée sur la nouvelle.
  if ses.ended_at is null and (ent->>'active')::boolean
     and (ent->>'period_start')::timestamptz <> ses.period_start then
    insert into public.gl_sessions (user_id, device, period_start, started_at, last_seen, ended_at, seconds)
      values (ses.user_id, ses.device, ses.period_start, ses.started_at, now(), now(), ses.seconds);
    update public.gl_sessions
      set period_start = (ent->>'period_start')::timestamptz, started_at = now(), seconds = 0
      where id = p_session;
    ent := public.gl_entitlement_for(auth.uid());
  end if;

  return ent || jsonb_build_object(
    'session_id', p_session,
    'stop', v_was_ended or p_end
      or not (ent->>'active')::boolean
      or coalesce((ent->>'remaining_seconds')::integer, 1) <= 0
  );
end $$;

create or replace function public.gl_session_start(p_device text)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  ent jsonb;
  v_id uuid;
begin
  if auth.uid() is null then raise exception 'not_authenticated' using errcode = '28000'; end if;
  ent := public.gl_entitlement_for(auth.uid());
  if not (ent->>'active')::boolean then
    raise exception 'no_active_subscription' using errcode = 'P0001';
  end if;
  if coalesce((ent->>'remaining_seconds')::integer, 1) <= 0 then
    raise exception 'quota_exhausted' using errcode = 'P0001';
  end if;

  -- Une seule session ouverte par appareil : la précédente est clôturée.
  update public.gl_sessions
    set seconds = seconds + least(greatest(extract(epoch from now() - last_seen)::integer, 0), 120),
        last_seen = now(), ended_at = now()
    where user_id = auth.uid() and device = left(p_device, 120) and ended_at is null;

  insert into public.gl_sessions (user_id, device, period_start)
    values (auth.uid(), left(coalesce(nullif(p_device, ''), 'iPhone'), 120), (ent->>'period_start')::timestamptz)
    returning id into v_id;

  return ent || jsonb_build_object('session_id', v_id, 'stop', false);
end $$;

create or replace function public.gl_session_heartbeat(p_session uuid)
returns jsonb language sql security definer set search_path = public
as $$ select public.gl_session_tick(p_session, false) $$;

create or replace function public.gl_session_stop(p_session uuid)
returns jsonb language sql security definer set search_path = public
as $$ select public.gl_session_tick(p_session, true) $$;

revoke all on function public.gl_entitlement_for(uuid) from public, anon, authenticated;
revoke all on function public.gl_session_tick(uuid, boolean) from public, anon, authenticated;
revoke all on function public.gl_entitlement() from public, anon;
revoke all on function public.gl_session_start(text) from public, anon;
revoke all on function public.gl_session_heartbeat(uuid) from public, anon;
revoke all on function public.gl_session_stop(uuid) from public, anon;
grant execute on function public.gl_entitlement() to authenticated;
grant execute on function public.gl_session_start(text) to authenticated;
grant execute on function public.gl_session_heartbeat(uuid) to authenticated;
grant execute on function public.gl_session_stop(uuid) to authenticated;
