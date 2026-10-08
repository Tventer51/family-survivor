"""Durable game logic. Every write rechecks identity, ownership and deadline."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')

def next_voting_deadline(now=None):
    """Next Wednesday at 20:00 local Eastern time, respecting DST."""
    current=datetime.fromtimestamp(time.time() if now is None else now,EASTERN)
    day=current.date()+timedelta(days=(2-current.weekday())%7)
    cutoff=datetime(day.year,day.month,day.day,20,tzinfo=EASTERN)
    if cutoff<=current: cutoff+=timedelta(days=7)
    return cutoff.timestamp()

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('SURVIVOR_DB', str(BASE / 'runtime' / 'survivor.sqlite3')))
DATABASE_URL = os.environ.get('DATABASE_URL', '')
TABLES = ('users','teams','contestants','episodes','access','picks','predictions','events','settings','audit','attempts','deleted_picks')
SEED = json.loads((BASE / 'data' / 'season.json').read_text(encoding='utf-8'))
GRID_RULES={}
for idx,rule in enumerate(SEED['rules']):
    values=[rule['points']] if isinstance(rule['points'],(int,float)) else [25,-25]
    for pos,points in enumerate(values):
        key=f'{idx}_{pos}'
        GRID_RULES[key]={**rule,'points':points}

SCHEMA = '''
        CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, hash TEXT NOT NULL,
          admin INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS access(username TEXT REFERENCES users(username), team TEXT,
          PRIMARY KEY(username,team));
        CREATE TABLE IF NOT EXISTS teams(name TEXT PRIMARY KEY, roster TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contestants(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS episodes(number INTEGER PRIMARY KEY, deadline REAL, closed INTEGER NOT NULL,
          results INTEGER NOT NULL DEFAULT 0, out_names TEXT NOT NULL DEFAULT '[]', title TEXT NOT NULL DEFAULT '',
          confirmed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS picks(team TEXT REFERENCES teams(name), episode INTEGER REFERENCES episodes(number),
          out_name TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
          legacy_out REAL NOT NULL DEFAULT 0, legacy_title REAL NOT NULL DEFAULT 0, PRIMARY KEY(team,episode));
        CREATE TABLE IF NOT EXISTS predictions(team TEXT PRIMARY KEY REFERENCES teams(name),
          contestant TEXT NOT NULL, episode INTEGER NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, episode INTEGER REFERENCES episodes(number),
          contestant TEXT REFERENCES contestants(name), category TEXT NOT NULL, points REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, details TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS attempts(username TEXT PRIMARY KEY, count INTEGER NOT NULL, since REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS deleted_picks(id INTEGER PRIMARY KEY AUTOINCREMENT,team TEXT,episode INTEGER,
          payload TEXT NOT NULL,actor TEXT NOT NULL,at REAL NOT NULL,reason TEXT NOT NULL);
        '''

class GameError(ValueError): pass

def password_hash(password):
    if len(password) < 12: raise GameError('Use a password of at least 12 characters.')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
    return f'pbkdf2$600000${salt}${digest}'

def verify_password(password, stored):
    try:
        _, rounds, salt, digest = stored.split('$')
        result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(result, digest)
    except (ValueError, TypeError): return False

@contextmanager
def connection(write=False):
    if DATABASE_URL:
        from postgres import Postgres
        db = Postgres(DATABASE_URL, write=write)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally: db.close()
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        if write: db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally: db.close()

def rows(sql, params=()):
    with connection() as db: return [dict(r) for r in db.execute(sql,params)]

def audit(db, actor, action, details):
    db.execute('INSERT INTO audit(actor,action,details,at) VALUES(?,?,?,?)',
               (actor, action, json.dumps(details), time.time()))

def initialize():
    with connection(write=True) as db:
        db.executescript(SCHEMA)
        # Source history is seeded exactly once and never overwrites live votes on restart.
        if not db.execute('SELECT 1 FROM settings WHERE key=?',('seeded',)).fetchone():
            db.executemany('INSERT INTO teams VALUES(?,?)',[(t['name'],json.dumps(t['roster'])) for t in SEED['teams']])
            db.executemany('INSERT INTO contestants VALUES(?)',[(n,) for n in SEED['contestants']])
            for ep in range(1,14):
                db.execute('INSERT INTO episodes(number,closed,results,out_names,title) VALUES(?,?,?,?,?)',
                           (ep,1,0,'[]',''))
            for p in SEED['picks']:
                db.execute('INSERT INTO picks VALUES(?,?,?,?,?,?,?)',
                           (p['team'],p['episode'],p['out'],p['title'],0,0,0))
                if p['winner'] in SEED['contestants'] and not db.execute('SELECT 1 FROM predictions WHERE team=?',(p['team'],)).fetchone():
                    db.execute('INSERT INTO predictions VALUES(?,?,?,?)',(p['team'],p['winner'],p['episode'],0))
            db.executemany('INSERT INTO settings VALUES(?,?)',[('seeded','1'),('winner',''),('bonus_mode','single')])
        # One-time schedule migration. Restarting never advances a missed deadline.
        if not db.execute("SELECT 1 FROM settings WHERE key='wednesday_schedule_v1'").fetchone():
            future=list(db.execute('SELECT number FROM episodes ORDER BY number'))
            first=datetime.fromtimestamp(next_voting_deadline(),EASTERN)
            for index,episode in enumerate(future):
                cutoff=(first+timedelta(weeks=episode['number']-game_start_episode())).timestamp()
                db.execute('UPDATE episodes SET deadline=? WHERE number=?',(cutoff,episode['number']))
            db.execute("INSERT INTO settings VALUES('wednesday_schedule_v1','1')")
            audit(db,'system','Wednesday voting schedule enabled',{'timezone':'America/New_York','hour':20})
        if not db.execute("SELECT 1 FROM settings WHERE key='jeff_title_v1'").fetchone():
            for pick in SEED['picks']:
                if pick['episode']==1 and pick['title']=='Jeff Probst':
                    db.execute("UPDATE picks SET title='Jeff Probst' WHERE team=? AND episode=1 AND title='No One' AND updated=0",(pick['team'],))
            db.execute("UPDATE episodes SET title='Jeff Probst' WHERE number=1 AND title IN ('','No One')")
            db.execute("INSERT INTO settings VALUES('jeff_title_v1','1')")
            audit(db,'system','episode 1 Other title picks mapped to Jeff Probst',{})

def game_start_episode(): return SEED['source_episodes']+1

def reset_scoring(identity):
    """Explicit organizer reset. Caller takes a database backup before using it."""
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        counts={'events':db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                'published_episodes':db.execute('SELECT COUNT(*) FROM episodes WHERE results=1').fetchone()[0]}
        db.execute('DELETE FROM events')
        db.execute("UPDATE episodes SET results=0,confirmed=0,out_names='[]',title='' WHERE 1")
        db.execute('UPDATE episodes SET closed=1 WHERE number<=?',(SEED['source_episodes'],))
        db.execute('UPDATE picks SET legacy_out=0,legacy_title=0')
        db.execute("UPDATE settings SET value='' WHERE key='winner'")
        anchor=db.execute('SELECT deadline FROM episodes WHERE number=?',(game_start_episode(),)).fetchone()[0]
        if anchor:
            first=datetime.fromtimestamp(anchor,EASTERN)
            for ep in range(1,game_start_episode()):
                deadline=(first+timedelta(weeks=ep-game_start_episode())).timestamp()
                db.execute('UPDATE episodes SET deadline=COALESCE(deadline,?) WHERE number=?',(deadline,ep))
        audit(db,actor['username'],'scoring reset; picks preserved',counts)

def has_admin(): return bool(rows('SELECT 1 FROM users WHERE admin=1 AND active=1'))

def bootstrap(username, password):
    username = clean_username(username)
    hashed = password_hash(password)
    with connection(write=True) as db:
        if db.execute('SELECT 1 FROM users WHERE admin=1').fetchone(): raise GameError('An organizer already exists.')
        db.execute('INSERT INTO users(username,hash,admin) VALUES(?,?,1)',(username,hashed))
        audit(db,username,'organizer created',{})

def clean_username(username):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_.-]{3,40}',username): raise GameError('Username must be 3–40 letters, numbers, dots, hyphens or underscores.')
    return username

def login(username, password):
    username = username.strip().lower()
    now=time.time()
    with connection(write=True) as db:
        attempt=db.execute('SELECT * FROM attempts WHERE username=?',(username,)).fetchone()
        if attempt and attempt['count']>=5 and now-attempt['since']<900:
            raise GameError('Too many attempts. Try again in 15 minutes.')
        user=db.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
        # Equal-cost dummy hash avoids a fast path for unknown usernames.
        valid=verify_password(password,user['hash'] if user else 'pbkdf2$600000$dummy$'+'0'*64)
        if not valid or not user:
            count=attempt['count']+1 if attempt and now-attempt['since']<900 else 1
            since=attempt['since'] if attempt and now-attempt['since']<900 else now
            db.execute('INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET count=excluded.count,since=excluded.since',(username,count,since))
            # Commit the failed-attempt record before returning an error.
            db.commit()
            raise GameError('Username or password is incorrect.')
        db.execute('DELETE FROM attempts WHERE username=?',(username,))
        audit(db,username,'signed in',{})
        return {'username':username,'version':user['version'],'expires':now+8*3600}

def require(db, identity, admin=False, team=None):
    if not identity or identity['expires']<=time.time(): raise GameError('Please sign in again.')
    user=db.execute('SELECT * FROM users WHERE username=? AND active=1',(identity['username'],)).fetchone()
    if not user or user['version']!=identity['version']: raise GameError('Please sign in again.')
    if admin and not user['admin']: raise GameError('Organizer access is required.')
    if team and not db.execute('SELECT 1 FROM access WHERE username=? AND team=?',(user['username'],team)).fetchone():
        raise GameError('This team is not assigned to your account.')
    return dict(user)

def who(identity):
    with connection() as db: return require(db,identity)

def my_teams(identity):
    with connection() as db:
        user=require(db,identity)
        return [r['team'] for r in db.execute('SELECT team FROM access WHERE username=? ORDER BY team',(user['username'],))]

def create_user(identity, username, password, teams):
    username=clean_username(username); hashed=password_hash(password)
    if not teams: raise GameError('Assign at least one team.')
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        if db.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone(): raise GameError('That username already exists.')
        known={r[0] for r in db.execute('SELECT name FROM teams')}
        if not set(teams)<=known: raise GameError('Unknown team.')
        db.execute('INSERT INTO users(username,hash) VALUES(?,?)',(username,hashed))
        db.executemany('INSERT INTO access VALUES(?,?)',[(username,t) for t in teams])
        audit(db,actor['username'],'account created',{'username':username,'teams':teams})

def manage_user(identity, username, teams, new_password='', active=True):
    hashed=password_hash(new_password) if new_password else None
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        target=db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        if not target or target['admin']: raise GameError('Use the password tab for the organizer; participant accounts only here.')
        known={r[0] for r in db.execute('SELECT name FROM teams')}
        if not set(teams)<=known: raise GameError('Unknown team.')
        db.execute('DELETE FROM access WHERE username=?',(username,))
        db.executemany('INSERT INTO access VALUES(?,?)',[(username,t) for t in teams])
        db.execute('UPDATE users SET active=?,version=version+1 WHERE username=?',(int(active),username))
        if hashed: db.execute('UPDATE users SET hash=? WHERE username=?',(hashed,username))
        audit(db,actor['username'],'account updated',{'username':username,'teams':teams,'active':active})

def change_password(identity, old, new):
    hashed=password_hash(new)
    with connection(write=True) as db:
        user=require(db,identity)
        if not verify_password(old,user['hash']): raise GameError('Current password is incorrect.')
        db.execute('UPDATE users SET hash=?,version=version+1 WHERE username=?',(hashed,user['username']))
        audit(db,user['username'],'password changed',{})

def eligible(db,ep):
    eliminated=set()
    for r in db.execute('SELECT out_names FROM episodes WHERE number<? AND results=1',(ep,)): eliminated.update(json.loads(r[0]))
    return [r[0] for r in db.execute('SELECT name FROM contestants ORDER BY name') if r[0] not in eliminated]

def save_pick(identity,team,ep,out,title,winner=''):
    with connection(write=True) as db:
        user=require(db,identity,team=team)
        episode=db.execute('SELECT * FROM episodes WHERE number=?',(ep,)).fetchone()
        if db.execute("SELECT value FROM settings WHERE key='winner'").fetchone()[0]:
            raise GameError('The season winner has been recorded. Voting is finished.')
        if not episode or episode['closed'] or episode['results'] or not episode['deadline'] or time.time()>=episode['deadline']:
            raise GameError('Voting is closed for this episode. Your existing picks are unchanged.')
        names=eligible(db,ep)
        if out not in names or title not in names+['No One','Jeff Probst']: raise GameError('Choose an eligible out pick and a valid title speaker.')
        existing=db.execute('SELECT * FROM predictions WHERE team=?',(team,)).fetchone()
        if winner:
            if winner not in names: raise GameError('Choose an eligible Sole Survivor.')
            if existing and existing['episode']!=ep:
                raise GameError('Your season-long Sole Survivor prediction is locked after its episode closes.')
        db.execute('INSERT INTO picks(team,episode,out_name,title,updated) VALUES(?,?,?,?,?) ON CONFLICT(team,episode) DO UPDATE SET out_name=excluded.out_name,title=excluded.title,updated=excluded.updated',
                   (team,ep,out,title,time.time()))
        if winner:
            db.execute('INSERT INTO predictions VALUES(?,?,?,?) ON CONFLICT(team) DO UPDATE SET contestant=excluded.contestant,updated=excluded.updated',(team,winner,ep,time.time()))
        elif existing and existing['episode']==ep:
            db.execute('DELETE FROM predictions WHERE team=?',(team,))
        audit(db,user['username'],'picks saved',{'team':team,'episode':ep,'out':out,'title':title,'winner':winner})

def set_episode(identity,ep,deadline,closed,out_names,title,results):
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        old=db.execute('SELECT * FROM episodes WHERE number=?',(ep,)).fetchone()
        if not old: raise GameError('Unknown episode.')
        # Deadline is owned by the fixed weekly schedule, never by a browser input.
        deadline=old['deadline']
        names={r[0] for r in db.execute('SELECT name FROM contestants')}
        if not set(out_names)<=names or (title and title not in names|{'No One','Jeff Probst'}): raise GameError('Unknown contestant or title speaker.')
        if not closed and (not deadline or deadline<=time.time()): raise GameError('Set a future deadline to open voting.')
        if not closed and db.execute("SELECT value FROM settings WHERE key='winner'").fetchone()[0]:
            raise GameError('Clear the recorded season winner before reopening voting.')
        if not closed and (results or old['results'] or db.execute('SELECT 1 FROM episodes WHERE number>? AND results=1',(ep,)).fetchone()):
            raise GameError('An episode with results, or earlier than published results, cannot be reopened.')
        if results and (not closed or not title): raise GameError('Close voting and select the episode-title speaker before publishing results.')
        if results and deadline and deadline>time.time(): raise GameError('The voting deadline must pass before results are published.')
        db.execute('UPDATE episodes SET deadline=?,closed=?,out_names=?,title=?,results=?,confirmed=? WHERE number=?',
                   (deadline,int(closed),json.dumps(out_names),title,int(results),int(results),ep))
        audit(db,actor['username'],'episode updated',{'episode':ep,'deadline':deadline,'closed':closed,'out':out_names,'title':title,'results':results})

def add_event(identity,ep,contestant,category,points,note=''):
    if not isinstance(points,(int,float)) or not -1000<=points<=1000: raise GameError('Points must be between -1000 and 1000.')
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        if not db.execute('SELECT 1 FROM episodes WHERE number=?',(ep,)).fetchone(): raise GameError('Unknown episode.')
        if not db.execute('SELECT 1 FROM contestants WHERE name=?',(contestant,)).fetchone(): raise GameError('Unknown contestant.')
        db.execute('INSERT INTO events(episode,contestant,category,points,note) VALUES(?,?,?,?,?)',(ep,contestant,category,points,note))
        audit(db,actor['username'],'score added',{'episode':ep,'contestant':contestant,'category':category,'points':points,'note':note})

def delete_event(identity,event_id):
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        event=db.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone()
        if not event: raise GameError('Scoring entry does not exist.')
        db.execute('DELETE FROM events WHERE id=?',(event_id,))
        audit(db,actor['username'],'score removed',dict(event))

def set_winner(identity,winner):
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        if winner and not db.execute('SELECT 1 FROM contestants WHERE name=?',(winner,)).fetchone(): raise GameError('Unknown winner.')
        if winner and db.execute('SELECT 1 FROM episodes WHERE closed=0').fetchone(): raise GameError('Close all voting before recording the season winner.')
        db.execute('UPDATE settings SET value=? WHERE key=?',(winner,'winner'))
        audit(db,actor['username'],'season winner updated',{'winner':winner})

def correct_prediction(identity,team,contestant,ep,reason):
    if not reason.strip(): raise GameError('Explain the historical correction in the note.')
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        if not db.execute('SELECT 1 FROM teams WHERE name=?',(team,)).fetchone(): raise GameError('Unknown team.')
        if not db.execute('SELECT 1 FROM episodes WHERE number=?',(ep,)).fetchone(): raise GameError('Unknown episode.')
        if contestant and contestant not in eligible(db,ep): raise GameError('That contestant was already eliminated before this episode.')
        previous=db.execute('SELECT * FROM predictions WHERE team=?',(team,)).fetchone()
        db.execute('DELETE FROM predictions WHERE team=?',(team,))
        if contestant: db.execute('INSERT INTO predictions VALUES(?,?,?,?)',(team,contestant,ep,time.time()))
        audit(db,actor['username'],'winner prediction corrected',{'team':team,'before':dict(previous) if previous else None,'contestant':contestant,'episode':ep,'reason':reason})

def snapshot(identity, through_episode=None):
    with connection() as db:
        user=require(db,identity)
        db.execute('UPDATE episodes SET closed=1 WHERE closed=0 AND deadline IS NOT NULL AND deadline<=?',(time.time(),))
        get=lambda sql:[dict(r) for r in db.execute(sql)]
        episodes=get('SELECT * FROM episodes ORDER BY number')
        events=get('SELECT * FROM events ORDER BY id')
        picks=get('SELECT * FROM picks ORDER BY episode,team')
        predictions=get('SELECT * FROM predictions')
        teams=get('SELECT * FROM teams ORDER BY name')
        winner=db.execute('SELECT value FROM settings WHERE key=?',('winner',)).fetchone()[0]
        contestant_points={n:0 for n in SEED['contestants']}
        all_published={e['number'] for e in episodes if e['results']}
        published={ep for ep in all_published if through_episode is None or ep<=through_episode}
        for event in events:
            if event['episode'] in published: contestant_points[event['contestant']]+=event['points']
        epmap={e['number']:e for e in episodes}
        scored=[]
        for p in picks:
            e=epmap[p['episode']]; o=t=0
            if p['episode'] in published:
                o=30*int(bool(p['out_name']) and p['out_name'] in json.loads(e['out_names']))
                t=30*int(bool(p['title']) and p['title']==e['title'])
            scored.append({**p,'out_points':o,'title_points':t,'points':o+t})
        standings=[]
        eliminated=set(n for e in episodes if e['number'] in published for n in json.loads(e['out_names']))
        winner_visible=winner and (through_episode is None or (all_published and through_episode>=max(all_published)))
        for team in teams:
            roster=json.loads(team['roster'])
            core=sum(contestant_points[n] for n in roster)
            weekly=sum(p['points'] for p in scored if p['team']==team['name'])
            pred=next((p for p in predictions if p['team']==team['name']),None)
            bonus=SEED['bonuses'].get(str(pred['episode']),0) if winner_visible and pred and pred['contestant']==winner else 0
            standings.append({'Team':team['name'],'Draft points':core,'Weekly picks':weekly,'Winner bonus':bonus,
                              'Total':core+weekly+bonus,'Remaining':sum(n not in eliminated for n in roster),'Roster':', '.join(roster)})
        standings.sort(key=lambda s:(-s['Total'],s['Team']))
        for s in standings: s['Rank']=1+sum(other['Total']>s['Total'] for other in standings)
        own={r[0] for r in db.execute('SELECT team FROM access WHERE username=?',(user['username'],))}
        # Other teams' unpublished choices never leave the server.
        visible=[p for p in scored if user['admin'] or p['team'] in own or epmap[p['episode']]['results']]
        visible_predictions=[p for p in predictions if user['admin'] or p['team'] in own or (epmap[p['episode']]['closed'] and (not epmap[p['episode']]['deadline'] or time.time()>=epmap[p['episode']]['deadline']))]
        return {'user':user,'teams':teams,'episodes':episodes,'events':events,'picks':visible,
                'predictions':visible_predictions,'standings':standings,'contestant_points':contestant_points,'winner':winner}

def weekly_standings(identity,ep,weekly_only=False):
    current=snapshot(identity,ep); previous=snapshot(identity,ep-1)
    old={row['Team']:row for row in previous['standings']}
    result=[]
    for row in current['standings']:
        prior=old[row['Team']]
        result.append({**row,'Week draft':row['Draft points']-prior['Draft points'],
          'Week picks':row['Weekly picks']-prior['Weekly picks'],'Week bonus':row['Winner bonus']-prior['Winner bonus'],
          'Week total':row['Total']-prior['Total'],'Cumulative':row['Total']})
    measure='Week total' if weekly_only else 'Cumulative'
    result.sort(key=lambda r:(-r[measure],r['Team']))
    for row in result: row['Rank']=1+sum(other[measure]>row[measure] for other in result)
    return result

def grid_fingerprint(db,ep):
    events=[dict(r) for r in db.execute('SELECT * FROM events WHERE episode=? ORDER BY id',(ep,))]
    payload={'events':events,'eligible':eligible(db,ep)}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()

def scoring_grid(identity,ep):
    with connection() as db:
        require(db,identity,admin=True)
        events=[dict(r) for r in db.execute('SELECT * FROM events WHERE episode=? ORDER BY id',(ep,))]
        return {'names':eligible(db,ep),'events':events,'version':grid_fingerprint(db,ep)}

def save_scoring_grid(identity,ep,selections,adjustments,version):
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        if grid_fingerprint(db,ep)!=version: raise GameError('Scores changed in another session. Reload the grid before saving.')
        names=eligible(db,ep)
        if set(selections)!=set(names) or set(adjustments)!=set(names): raise GameError('The episode contestant list changed. Reload the grid.')
        before=[dict(r) for r in db.execute('SELECT * FROM events WHERE episode=? ORDER BY id',(ep,))]
        for name,checked in selections.items():
            if not set(checked)<=set(GRID_RULES): raise GameError('Unknown scoring category.')
            for key,rule in GRID_RULES.items():
                matching=[e for e in before if e['contestant']==name and e['category']==rule['label'] and e['points']==rule['points']]
                if key in checked and not matching:
                    db.execute('INSERT INTO events(episode,contestant,category,points,note) VALUES(?,?,?,?,?)',(ep,name,rule['label'],rule['points'],'Scoring grid'))
                elif key not in checked:
                    for event in matching: db.execute('DELETE FROM events WHERE id=?',(event['id'],))
            points,note=adjustments[name]
            if not isinstance(points,(float,int)) or not -1000<=points<=1000: raise GameError('Adjustment points must be between -1000 and 1000.')
            if points and not note.strip(): raise GameError(f'Explain the custom adjustment for {name}.')
            db.execute("DELETE FROM events WHERE episode=? AND contestant=? AND category='Grid adjustment'",(ep,name))
            if points: db.execute('INSERT INTO events(episode,contestant,category,points,note) VALUES(?,?,?,?,?)',(ep,name,'Grid adjustment',points,note))
        audit(db,actor['username'],'scoring grid saved',{'episode':ep,'before':before,'checked':selections,'adjustments':adjustments})

def delete_pick(identity,team,ep,reason):
    if not reason.strip(): raise GameError('Enter a reason for deleting the pick.')
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        pick=db.execute('SELECT * FROM picks WHERE team=? AND episode=?',(team,ep)).fetchone()
        if not pick: raise GameError('That pick was already removed. Refresh the table.')
        db.execute('INSERT INTO deleted_picks(team,episode,payload,actor,at,reason) VALUES(?,?,?,?,?,?)',
          (team,ep,json.dumps(dict(pick)),actor['username'],time.time(),reason))
        db.execute('DELETE FROM picks WHERE team=? AND episode=?',(team,ep))
        audit(db,actor['username'],'weekly pick deleted',{'pick':dict(pick),'reason':reason})

def restore_pick(identity,deleted_id):
    with connection(write=True) as db:
        actor=require(db,identity,admin=True)
        record=db.execute('SELECT * FROM deleted_picks WHERE id=?',(deleted_id,)).fetchone()
        if not record: raise GameError('Deleted pick is no longer available.')
        pick=json.loads(record['payload'])
        if db.execute('SELECT 1 FROM picks WHERE team=? AND episode=?',(pick['team'],pick['episode'])).fetchone():
            raise GameError('A current pick exists for this team and episode. It will not be overwritten.')
        db.execute('INSERT INTO picks VALUES(?,?,?,?,?,?,?)',tuple(pick[k] for k in ['team','episode','out_name','title','updated','legacy_out','legacy_title']))
        db.execute('DELETE FROM deleted_picks WHERE id=?',(deleted_id,))
        audit(db,actor['username'],'weekly pick restored',{'pick':pick})

def backup(identity,destination):
    with connection() as db:
        require(db,identity,admin=True)
        dest=sqlite3.connect(destination)
        try:
            if DATABASE_URL:
                dest.executescript(SCHEMA)
                for table in TABLES:
                    values=[tuple(r.values()) for r in db.execute(f'SELECT * FROM "{table}"')]
                    if values:
                        dest.executemany(f'INSERT INTO "{table}" VALUES({",".join("?" for _ in values[0])})',values)
                dest.commit()
            else: db.backup(dest)
        finally: dest.close()
