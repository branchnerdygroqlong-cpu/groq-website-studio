import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH


def now_utc():
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite-backed project, task, and model usage store."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  business_type TEXT,
                  status TEXT NOT NULL DEFAULT 'planning',
                  root_path TEXT,
                  brief TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                  title TEXT NOT NULL,
                  description TEXT,
                  status TEXT NOT NULL DEFAULT 'pending',
                  assigned_agent TEXT,
                  result TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_usage (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  model TEXT NOT NULL,
                  used_at REAL NOT NULL,
                  total_tokens INTEGER NOT NULL,
                  request_date TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_model_time
                  ON model_usage(model, used_at);
                """
            )
            # Non-destructive migration: existing usage rows remain valid.
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(model_usage)").fetchall()
            }
            if "prompt_tokens" not in columns:
                db.execute(
                    "ALTER TABLE model_usage ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "completion_tokens" not in columns:
                db.execute(
                    "ALTER TABLE model_usage ADD COLUMN completion_tokens INTEGER NOT NULL DEFAULT 0"
                )

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create_project(self, name, business_type="", brief="", root_path=""):
        stamp = now_utc()
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO projects
                   (name,business_type,brief,root_path,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (name, business_type, brief, root_path, stamp, stamp),
            )
            return cur.lastrowid

    def get_project(self, project_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_projects(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM projects ORDER BY updated_at DESC"
                )
            ]

    def set_project_status(self, project_id, status):
        with self.connect() as db:
            db.execute(
                "UPDATE projects SET status=?,updated_at=? WHERE id=?",
                (status, now_utc(), project_id),
            )

    def add_task(self, project_id, title, description="", agent=""):
        stamp = now_utc()
        with self.connect() as db:
            cur = db.execute(
                """INSERT INTO tasks
                   (project_id,title,description,assigned_agent,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (project_id, title, description, agent, stamp, stamp),
            )
            return cur.lastrowid

    def update_task(self, task_id, status, result=None):
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?",
                (status, result, now_utc(), task_id),
            )

    def list_tasks(self, project_id):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM tasks WHERE project_id=? ORDER BY id",
                    (project_id,),
                )
            ]

    def record_usage(
        self,
        model,
        timestamp,
        tokens,
        prompt_tokens=None,
        completion_tokens=None,
    ):
        day = datetime.fromtimestamp(timestamp).date().isoformat()
        total = int(tokens or 0)
        prompt = int(prompt_tokens or 0)
        completion = int(completion_tokens or 0)
        with self.connect() as db:
            db.execute(
                """INSERT INTO model_usage
                   (model,used_at,total_tokens,request_date,prompt_tokens,completion_tokens)
                   VALUES(?,?,?,?,?,?)""",
                (model, timestamp, total, day, prompt, completion),
            )

    def usage(self, model, timestamp=None):
        timestamp = timestamp or time.time()
        day = datetime.fromtimestamp(timestamp).date().isoformat()
        with self.connect() as db:
            minute = db.execute(
                """SELECT COALESCE(SUM(total_tokens),0) AS total,
                          COALESCE(SUM(prompt_tokens),0) AS prompt,
                          COALESCE(SUM(completion_tokens),0) AS completion,
                          COUNT(*) AS requests
                   FROM model_usage WHERE model=? AND used_at>=?""",
                (model, timestamp - 60),
            ).fetchone()
            daily = db.execute(
                """SELECT COALESCE(SUM(total_tokens),0) AS total,
                          COALESCE(SUM(prompt_tokens),0) AS prompt,
                          COALESCE(SUM(completion_tokens),0) AS completion,
                          COUNT(*) AS requests
                   FROM model_usage WHERE model=? AND request_date=?""",
                (model, day),
            ).fetchone()

        return {
            "minute_tokens": minute["total"],
            "minute_prompt_tokens": minute["prompt"],
            "minute_completion_tokens": minute["completion"],
            "minute_requests": minute["requests"],
            "daily_tokens": daily["total"],
            "daily_prompt_tokens": daily["prompt"],
            "daily_completion_tokens": daily["completion"],
            "daily_requests": daily["requests"],
        }
