-- ============================================================
-- Côtéa — schéma Supabase (MVP multi-utilisateurs réel)
-- À coller dans : Supabase Dashboard > SQL Editor > New query > Run
-- ============================================================

create extension if not exists "pgcrypto";

-- ---------- Établissements ----------
create table if not exists establishments (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  place text,
  join_code text unique not null,
  created_at timestamptz default now()
);

-- ---------- Profils (1 par utilisateur anonyme) ----------
create table if not exists profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  establishment_id uuid references establishments(id) on delete cascade,
  name text not null,
  envies jsonb default '[]',
  visibility text default 'visible',
  is_director boolean default false,
  created_at timestamptz default now()
);

-- ---------- Activités ----------
create table if not exists activities (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null references establishments(id) on delete cascade,
  topcat text not null,
  subcat text,
  title text not null,
  description text,
  date date not null,
  time text not null,
  lieu text not null,
  type text check (type in ('A','B')) default 'B',
  cap int not null default 6,
  price text default 'Gratuit',
  status text default 'ouverte', -- ouverte | complete | live | annulee
  stats jsonb,
  a_prevoir jsonb default '[]',
  photo text,
  orga_id uuid references profiles(id),
  orga_name text not null,
  created_at timestamptz default now()
);

-- ---------- Participants (table de jointure) ----------
create table if not exists activity_participants (
  activity_id uuid references activities(id) on delete cascade,
  user_id uuid references profiles(id) on delete cascade,
  user_name text not null,
  joined_at timestamptz default now(),
  primary key (activity_id, user_id)
);

-- ---------- Messages (chat par activité) ----------
create table if not exists messages (
  id uuid primary key default gen_random_uuid(),
  activity_id uuid not null references activities(id) on delete cascade,
  user_id uuid references profiles(id),
  user_name text not null,
  text text not null,
  created_at timestamptz default now()
);

-- ---------- Conseils & Tips ----------
create table if not exists tips (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null references establishments(id) on delete cascade,
  title text not null,
  body text not null,
  user_name text not null,
  created_at timestamptz default now()
);

-- ---------- Signalements (modération) ----------
create table if not exists reports (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid references establishments(id) on delete cascade,
  what text not null,
  ref uuid,
  by_user_id uuid,
  resolved boolean default false,
  created_at timestamptz default now()
);

-- ---------- Encarts publicitaires (publiés par le directeur) ----------
create table if not exists ads (
  id uuid primary key default gen_random_uuid(),
  establishment_id uuid not null references establishments(id) on delete cascade,
  title text not null,
  body text,
  image_url text,
  cta_label text,
  cta_url text,
  placement text not null default 'home' check (placement in ('home','category')),
  active boolean not null default true,
  created_at timestamptz default now()
);
create index if not exists ads_establishment_idx on ads(establishment_id, active);

-- ============================================================
-- Row Level Security — chaque voyageur ne voit que son établissement
-- ============================================================
alter table establishments enable row level security;
alter table profiles enable row level security;
alter table activities enable row level security;
alter table activity_participants enable row level security;
alter table messages enable row level security;
alter table tips enable row level security;
alter table reports enable row level security;

-- Établissements : lecture publique du nom/lieu par code (nécessaire avant onboarding)
create policy "public read establishments" on establishments for select using (true);

-- Profils : chacun gère son propre profil
create policy "own profile select" on profiles for select using (
  id = auth.uid() or establishment_id = (select establishment_id from profiles where id = auth.uid())
);
create policy "own profile insert" on profiles for insert with check (id = auth.uid());
create policy "own profile update" on profiles for update using (id = auth.uid());

-- Activités : visibles/modifiables dans son établissement uniquement
create policy "activities select in establishment" on activities for select using (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);
create policy "activities insert own establishment" on activities for insert with check (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
  and orga_id = auth.uid()
);
create policy "activities update own" on activities for update using (orga_id = auth.uid());

-- Participants : chacun gère sa propre participation (rejoindre / quitter)
create policy "participants select in establishment" on activity_participants for select using (
  activity_id in (select id from activities where establishment_id = (select establishment_id from profiles where id = auth.uid()))
);
create policy "participants insert self" on activity_participants for insert with check (user_id = auth.uid());
create policy "participants delete self" on activity_participants for delete using (user_id = auth.uid());
-- user_name est dénormalisé pour éviter des jointures : permet de le répercuter
-- quand le voyageur renomme son profil.
create policy "participants update own name" on activity_participants for update
  using (user_id = auth.uid()) with check (user_id = auth.uid());

-- Messages : réservés aux participants de l'activité
create policy "messages select if participant" on messages for select using (
  activity_id in (select activity_id from activity_participants where user_id = auth.uid())
  or activity_id in (select id from activities where orga_id = auth.uid())
);
create policy "messages insert if participant" on messages for insert with check (
  user_id = auth.uid() and (
    activity_id in (select activity_id from activity_participants where user_id = auth.uid())
    or activity_id in (select id from activities where orga_id = auth.uid())
  )
);
create policy "messages update own name" on messages for update
  using (user_id = auth.uid()) with check (user_id = auth.uid());

-- Tips : lecture/écriture dans son établissement
create policy "tips select in establishment" on tips for select using (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);
create policy "tips insert in establishment" on tips for insert with check (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);

-- Signalements : n'importe qui peut signaler, tout le monde de l'établissement peut voir (modération)
create policy "reports select in establishment" on reports for select using (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);
create policy "reports insert in establishment" on reports for insert with check (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);
create policy "reports update in establishment" on reports for update using (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);

-- Encarts : lus par tout membre de l'établissement, écrits par le seul directeur.
alter table ads enable row level security;
create policy "ads select in establishment" on ads for select using (
  establishment_id = (select establishment_id from profiles where id = auth.uid())
);
create policy "ads insert by director" on ads for insert with check (
  exists (select 1 from profiles p where p.id = auth.uid() and p.is_director and p.establishment_id = ads.establishment_id)
);
create policy "ads update by director" on ads for update using (
  exists (select 1 from profiles p where p.id = auth.uid() and p.is_director and p.establishment_id = ads.establishment_id)
);
create policy "ads delete by director" on ads for delete using (
  exists (select 1 from profiles p where p.id = auth.uid() and p.is_director and p.establishment_id = ads.establishment_id)
);

-- ============================================================
-- Active la synchronisation temps réel (chat, nouvelles activités, encarts)
-- ============================================================
alter publication supabase_realtime add table activities, activity_participants, messages, tips, reports, ads;

-- ============================================================
-- Établissement de démo (garde le même code que le prototype)
-- ============================================================
insert into establishments (name, place, join_code)
values ('Domaine des Pins', 'Saint-Jean-de-Monts', 'PINS-2607')
on conflict (join_code) do nothing;
