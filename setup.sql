-- جدول الغرف
create table rooms (
  id text primary key,
  player_x_id text,
  player_x_name text,
  player_o_id text,
  player_o_name text,
  board text default '---------',
  current_turn text default 'X',
  status text default 'waiting',
  winner text,
  created_at timestamp default now()
);

-- جدول النتائج
create table results (
  id serial primary key,
  room_id text,
  winner_id text,
  winner_name text,
  loser_id text,
  loser_name text,
  draw boolean default false,
  played_at timestamp default now()
);

-- جدول إحصائيات اللاعبين
create table players (
  telegram_id text primary key,
  name text,
  wins integer default 0,
  losses integer default 0,
  draws integer default 0,
  last_played timestamp default now()
);

-- تفعيل Realtime على جدول rooms
alter publication supabase_realtime add table rooms;
