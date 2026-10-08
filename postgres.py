"""PostgreSQL adapter for the game's fixed, parameterized SQL statements."""
import re

class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)

def row_factory(cursor):
    names=[column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names,values))

def translate(sql):
    # Only placeholders outside quoted SQL literals are converted.
    parts=re.split(r"('(?:''|[^'])*')",sql)
    for i in range(0,len(parts),2):
        parts[i]=parts[i].replace('?', '%s')
        parts[i]=parts[i].replace('INTEGER PRIMARY KEY AUTOINCREMENT','BIGSERIAL PRIMARY KEY')
        parts[i]=re.sub(r'\bREAL\b','DOUBLE PRECISION',parts[i])
    return ''.join(parts)

class Postgres:
    def __init__(self,url,write=False):
        import psycopg
        self.db=psycopg.connect(url,row_factory=row_factory,connect_timeout=15,prepare_threshold=None)
        self.db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ' if not write else 'SELECT pg_advisory_xact_lock(51512026)')
        # Serialize writes before their first read, matching SQLite BEGIN IMMEDIATE.
    def execute(self,sql,params=()):
        return self.db.execute(translate(sql),params)
    def executemany(self,sql,params):
        cursor=self.db.cursor(); cursor.executemany(translate(sql),params); return cursor
    def executescript(self,sql):
        for statement in sql.split(';'):
            if statement.strip(): self.execute(statement)
    def commit(self): self.db.commit()
    def rollback(self): self.db.rollback()
    def close(self): self.db.close()
