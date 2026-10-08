import sqlite3
import shutil
import uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import settings


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f'{prefix}_{uuid.uuid4().hex}'


class Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(settings.db_path), timeout=30, isolation_level=None, factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA busy_timeout = 30000')
    return c


@contextmanager
def transaction():
    c = connect()
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skus(id TEXT PRIMARY KEY, product_id TEXT NOT NULL, label TEXT NOT NULL, color TEXT, size TEXT, price_cents INTEGER NOT NULL CHECK(price_cents>=0), stock INTEGER NOT NULL CHECK(stock>=0), restock_at TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'ai', generation INTEGER NOT NULL DEFAULT 0, selected_product_id TEXT, selected_order_id TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]', cards TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'completed', turn_id TEXT, client_message_id TEXT, UNIQUE(conversation_id,role,client_message_id));
CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, usage TEXT, elapsed_ms INTEGER);
CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, mime_type TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, order_id TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policies(version INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS promotions(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotes(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(user_id TEXT NOT NULL, operation TEXT NOT NULL, request_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(user_id,operation,request_id));
CREATE TABLE IF NOT EXISTS return_notices(user_id TEXT NOT NULL,order_id TEXT NOT NULL,first_at TEXT NOT NULL,PRIMARY KEY(user_id,order_id));
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL, target_id TEXT NOT NULL, before_data TEXT, after_data TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_movements(id TEXT PRIMARY KEY, sku_id TEXT NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL, reference_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS part_skus(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,name TEXT NOT NULL,stock INTEGER NOT NULL CHECK(stock>=0),version INTEGER NOT NULL DEFAULT 1,restock_at TEXT);
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_notices(user_id TEXT NOT NULL,order_id TEXT NOT NULL,type TEXT NOT NULL,first_at TEXT NOT NULL,PRIMARY KEY(user_id,order_id,type));
CREATE TABLE IF NOT EXISTS fulfillments(id TEXT PRIMARY KEY,case_id TEXT NOT NULL UNIQUE,order_id TEXT NOT NULL,user_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_origins(turn_id TEXT PRIMARY KEY,kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,created_at);
CREATE INDEX IF NOT EXISTS cases_user ON cases(user_id,status);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id,status);
'''


def init_db():
    # Back up the populated database through SQLite (including its WAL) before
    # the first additive upgrade. Repeated starts do not reset data or back up.
    if settings.db_path.exists() and settings.db_path.stat().st_size:
        with sqlite3.connect(str(settings.db_path)) as old:
            table = old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            missing=[version for version in (2,3,4,5,6,7) if not table or not old.execute('SELECT 1 FROM schema_migrations WHERE version=?',(version,)).fetchone()]
            if missing:
                backup_path = settings.db_path.with_name(settings.db_path.name + f'.pre-v{min(missing)}-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.bak')
                with sqlite3.connect(str(backup_path)) as destination:
                    old.backup(destination)
                if settings.upload_dir.exists():
                    shutil.copytree(settings.upload_dir,Path(str(backup_path)+'.uploads'))
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        from . import conversation_state, model_usage, catalog, packing, work_operations
        conversation_state.init_schema(c)
        model_usage.init_schema(c)
        catalog.init_schema(c)
        packing.init_schema(c)
        work_operations.init_schema(c)
        from . import packing_sources, packing_workflow, logistics_operations, operations_dashboard
        packing_sources.init_schema(c)
        packing_workflow.init_schema(c)
        logistics_operations.init_schema(c)
        operations_dashboard.init_schema(c)
    from . import support
    support.init_support()
    from .seed import seed, migrate
    seed()
    migrate()
    from .service import refresh_legacy_assessments
    refresh_legacy_assessments()
    with transaction() as c:
        work_operations.seed_default_operators(c)
        # A stopped process cannot finish these model calls. Make recovery explicit.
        model_usage.recover(c)
        packing_sources.recover_pending(c)
        model_usage.prune(c)
        c.execute('INSERT OR IGNORE INTO schema_migrations VALUES (3,?)',(now(),))
        c.execute('INSERT OR IGNORE INTO schema_migrations VALUES (4,?)',(now(),))
        c.execute('INSERT OR IGNORE INTO schema_migrations VALUES (5,?)',(now(),))
        c.execute('INSERT OR IGNORE INTO schema_migrations VALUES (6,?)',(now(),))
        c.execute('INSERT OR IGNORE INTO schema_migrations VALUES (7,?)',(now(),))
        c.execute("UPDATE turns SET status='failed',phase='服务已重启，请重试或联系人工',error='服务重启中断了回复',updated_at=? WHERE status='pending'", (now(),))
