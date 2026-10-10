-- Tests du décompte des heures. Chaque bloc lève une exception en cas d'échec.
\set ON_ERROR_STOP 1
begin;
insert into auth.users values ('11111111-1111-1111-1111-111111111111'), ('22222222-2222-2222-2222-222222222222');
grant select on all tables in schema public to authenticated;
insert into gl_subscriptions values ('11111111-1111-1111-1111-111111111111', 'sub_1', 'essentiel', 'active',
  now() - interval '1 day', now() + interval '29 day', false, now());

create function pg_temp.as_user(u text) returns void language plpgsql as $$
begin perform set_config('request.jwt.claim.sub', u, true); end $$;
create function pg_temp.check(cond boolean, msg text) returns void language plpgsql as $$
begin if not cond then raise exception 'ÉCHEC : %', msg; end if; end $$;

do $$
declare ent jsonb; sid uuid; err text;
begin
  perform pg_temp.as_user('11111111-1111-1111-1111-111111111111');
  set local role authenticated;

  ent := gl_entitlement();
  perform pg_temp.check((ent->>'remaining_seconds')::int = 172800, 'Essentiel = 48 h');
  perform pg_temp.check(not (ent->>'routes')::boolean, 'pas de trajets en Essentiel');

  sid := (gl_session_start('iPhone test')->>'session_id')::uuid;
  reset role;
  update gl_sessions set last_seen = now() - interval '50 seconds' where id = sid;
  set local role authenticated;
  ent := gl_session_heartbeat(sid);
  perform pg_temp.check((ent->>'remaining_seconds')::int = 172750, '50 s décomptées');

  reset role;
  update gl_sessions set last_seen = now() - interval '3 hours' where id = sid;
  set local role authenticated;
  ent := gl_session_heartbeat(sid);
  perform pg_temp.check((ent->>'used_seconds')::int = 170, 'coupure plafonnée à 120 s');

  -- Isolation : un autre compte ne voit ni ne touche cette session.
  perform pg_temp.as_user('22222222-2222-2222-2222-222222222222');
  begin perform gl_session_heartbeat(sid); err := 'aucune'; exception when others then err := sqlerrm; end;
  perform pg_temp.check(err = 'session_not_found', 'session d''autrui inaccessible');
  begin perform gl_session_start('x'); err := 'aucune'; exception when others then err := sqlerrm; end;
  perform pg_temp.check(err = 'no_active_subscription', 'pas d''abonnement, pas de session');
  perform pg_temp.check((select count(*) from gl_sessions) = 0, 'RLS sur gl_sessions');
  begin perform gl_entitlement_for('11111111-1111-1111-1111-111111111111'); err := 'aucune';
  exception when insufficient_privilege then err := 'refusé'; end;
  perform pg_temp.check(err = 'refusé', 'gl_entitlement_for interdit aux clients');

  -- Quota épuisé, puis pack de 10 h.
  perform pg_temp.as_user('11111111-1111-1111-1111-111111111111');
  reset role;
  update gl_sessions set seconds = 48 * 3600 where id = sid;
  set local role authenticated;
  ent := gl_session_heartbeat(sid);
  perform pg_temp.check((ent->>'stop')::boolean, 'stop quand le quota est épuisé');
  begin perform gl_session_start('iPhone test'); err := 'aucune'; exception when others then err := sqlerrm; end;
  perform pg_temp.check(err = 'quota_exhausted', 'nouvelle session refusée');

  reset role;
  insert into gl_hour_packs (user_id, seconds, period_start, stripe_session_id)
    select user_id, 36000, period_start, 'cs_test' from gl_subscriptions;
  set local role authenticated;
  ent := gl_entitlement();
  perform pg_temp.check((ent->>'remaining_seconds')::int between 35800 and 36000, 'pack de 10 h crédité');

  -- Renouvellement en pleine session : la suite est décomptée sur la nouvelle période.
  sid := (gl_session_start('iPhone test')->>'session_id')::uuid;
  reset role;
  update gl_sessions set seconds = 600, last_seen = now() - interval '60 seconds' where id = sid;
  update gl_subscriptions set period_start = now(), period_end = now() + interval '30 day';
  set local role authenticated;
  ent := gl_session_heartbeat(sid);
  perform pg_temp.check((ent->>'remaining_seconds')::int = 172800, 'nouvelle période complète');
  perform pg_temp.check(not (ent->>'stop')::boolean, 'session poursuivie après renouvellement');

  -- Illimité : pas de quota, trajets autorisés.
  reset role;
  update gl_subscriptions set plan = 'illimite';
  set local role authenticated;
  ent := gl_entitlement();
  perform pg_temp.check(ent->'remaining_seconds' = 'null'::jsonb and (ent->>'routes')::boolean, 'Illimité');

  ent := gl_session_stop(sid);
  perform pg_temp.check((ent->>'stop')::boolean, 'arrêt de session');

  -- Abonnement résilié : plus d'accès.
  reset role;
  update gl_subscriptions set status = 'canceled';
  set local role authenticated;
  perform pg_temp.check(not (gl_entitlement()->>'active')::boolean, 'abonnement résilié inactif');
  reset role;
end $$;

-- Un visiteur anonyme ne peut rien appeler.
set role anon;
do $$ begin
  perform gl_entitlement();
  raise exception 'ÉCHEC : anon a pu appeler gl_entitlement';
exception when insufficient_privilege then null;
end $$;
reset role;
rollback;
\echo 'quota_test.sql : tous les tests passent'
