"""本地学习数据库（SQLite，零依赖）。

两个必须注意的点：

1. 全部方法都是**同步**的，插件侧一律用 ``asyncio.to_thread(...)`` 调用。
2. ``sqlite3`` 的 ``with conn:`` 只管事务，**不会关闭连接**。这里统一用
   ``contextlib.closing`` 包装，避免长跑插件连接泄漏（Windows 下还会导致
   文件被占用、无法删除）。

数据库文件落在插件的 ``data/study.db``，``data/`` 已被 .gitignore 排除。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    exam_type TEXT DEFAULT 'gaokao',
    region TEXT DEFAULT '',
    school TEXT DEFAULT '',
    grade TEXT DEFAULT '',
    subjects TEXT DEFAULT '',
    target_score REAL DEFAULT 0,
    exam_date TEXT DEFAULT '',
    daily_minutes INTEGER DEFAULT 180,
    teach_style TEXT DEFAULT 'gentle',
    quiz_stage TEXT DEFAULT 'auto',
    psychology_level TEXT DEFAULT 'normal',
    credits TEXT DEFAULT '',
    updated REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mastery (
    point_id TEXT PRIMARY KEY,
    subject TEXT DEFAULT '',
    level REAL DEFAULT 0.5,
    attempts INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0,
    next_review REAL DEFAULT 0,
    updated REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id TEXT DEFAULT '',
    form TEXT DEFAULT '',
    stage TEXT DEFAULT '',
    correct INTEGER DEFAULT 0,
    note TEXT DEFAULT '',
    created REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    created REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    created REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    url TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    created REAL DEFAULT 0
);
"""

_PROFILE_COLUMNS = (
    "exam_type", "region", "school", "grade", "subjects", "target_score",
    "exam_date", "daily_minutes", "teach_style", "quiz_stage",
    "psychology_level", "credits", "updated",
)

# 老库升级用：新增列的 DDL
_PROFILE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("credits", "TEXT DEFAULT ''"),
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_payload(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class StudyStore:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """给已存在的旧库补上新列（直接在旧库上 INSERT 新列会报错）。"""
        with closing(self._connect()) as conn:
            existing = {row["name"] for row in self._read(conn, "PRAGMA table_info(profile)")}
            for column, ddl in _PROFILE_MIGRATIONS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE profile ADD COLUMN {column} {ddl}")
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _read(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    # ── 档案 ──────────────────────────────────────────────────
    def get_profile(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            rows = self._read(conn, "SELECT * FROM profile WHERE id = 1")
        if not rows:
            return {}
        data = rows[0]
        data.pop("id", None)
        return data

    def save_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self.get_profile())
        merged.update({key: value for key, value in (profile or {}).items() if value is not None})
        merged["updated"] = time.time()
        values = [merged.get(column, "") for column in _PROFILE_COLUMNS]
        columns = ", ".join(_PROFILE_COLUMNS)
        placeholders = ", ".join("?" for _ in _PROFILE_COLUMNS)
        updates = ", ".join(f"{column} = excluded.{column}" for column in _PROFILE_COLUMNS)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    f"INSERT INTO profile (id, {columns}) VALUES (1, {placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {updates}",
                    values,
                )
        return merged

    # ── 掌握度 ────────────────────────────────────────────────
    def mastery_map(self, subject: str = "") -> dict[str, float]:
        with closing(self._connect()) as conn:
            if subject:
                rows = self._read(conn, "SELECT point_id, level FROM mastery WHERE subject = ?", (subject,))
            else:
                rows = self._read(conn, "SELECT point_id, level FROM mastery")
        return {row["point_id"]: _safe_float(row["level"], 0.5) for row in rows}

    def set_mastery(self, point_id: str, subject: str, level: float) -> float:
        level = min(1.0, max(0.0, _safe_float(level, 0.5)))
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO mastery (point_id, subject, level, attempts, correct, next_review, updated) "
                    "VALUES (?, ?, ?, 0, 0, 0, ?) "
                    "ON CONFLICT(point_id) DO UPDATE SET level = excluded.level, "
                    "subject = excluded.subject, updated = excluded.updated",
                    (point_id, subject, level, time.time()),
                )
        return level

    def update_mastery(self, point_id: str, subject: str, correct: bool) -> float:
        """按作答结果推进掌握度：答对向 1 收敛，答错按比例衰减。"""
        level = self.mastery_map().get(point_id, 0.5)
        if correct:
            level = level + 0.18 * (1.0 - level)
        else:
            level = level * 0.78
        level = min(1.0, max(0.02, level))
        # 艾宾浩斯式复习间隔：掌握越好，下一次复习越远
        interval_days = 1 if not correct else max(1, int(round(1 + level * 14)))
        next_review = time.time() + interval_days * 86400
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO mastery (point_id, subject, level, attempts, correct, next_review, updated) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(point_id) DO UPDATE SET level = excluded.level, "
                    "attempts = attempts + 1, correct = correct + ?, "
                    "next_review = excluded.next_review, updated = excluded.updated",
                    (point_id, subject, level, 1 if correct else 0, next_review, time.time(), 1 if correct else 0),
                )
                conn.execute(
                    "INSERT INTO attempts (point_id, form, stage, correct, note, created) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (point_id, "", "", 1 if correct else 0, "", time.time()),
                )
        return level

    def due_reviews(self, limit: int = 10) -> list[dict[str, Any]]:
        now = time.time()
        with closing(self._connect()) as conn:
            return self._read(
                conn,
                "SELECT point_id, subject, level, next_review FROM mastery "
                "WHERE next_review > 0 AND next_review <= ? ORDER BY next_review LIMIT ?",
                (now, limit),
            )

    def attempt_stats(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()
            correct = conn.execute("SELECT COUNT(*) AS n FROM attempts WHERE correct = 1").fetchone()
        total_n = int(total["n"] or 0) if total else 0
        correct_n = int(correct["n"] or 0) if correct else 0
        return {
            "total": total_n,
            "correct": correct_n,
            "rate": round(correct_n / total_n, 3) if total_n else 0.0,
        }

    # ── 计划 ──────────────────────────────────────────────────
    def save_plan(self, title: str, start_date: str, end_date: str, payload: dict[str, Any]) -> int:
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO plans (title, start_date, end_date, payload, created) VALUES (?, ?, ?, ?, ?)",
                    (title, start_date, end_date, json.dumps(payload or {}, ensure_ascii=False), time.time()),
                )
                return int(cursor.lastrowid or 0)

    def latest_plan(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            rows = self._read(conn, "SELECT * FROM plans ORDER BY id DESC LIMIT 1")
        if not rows:
            return {}
        data = rows[0]
        data["payload"] = _load_payload(data.get("payload"))
        return data

    def list_plans(self, limit: int = 5) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = self._read(conn, "SELECT * FROM plans ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = _load_payload(row.get("payload"))
        return rows

    # ── 诊断 ──────────────────────────────────────────────────
    def save_diagnosis(self, summary: str, payload: dict[str, Any]) -> int:
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO diagnoses (summary, payload, created) VALUES (?, ?, ?)",
                    (summary, json.dumps(payload or {}, ensure_ascii=False), time.time()),
                )
                return int(cursor.lastrowid or 0)

    def latest_diagnosis(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            rows = self._read(conn, "SELECT * FROM diagnoses ORDER BY id DESC LIMIT 1")
        if not rows:
            return {}
        data = rows[0]
        data["payload"] = _load_payload(data.get("payload"))
        return data

    # ── 资源 ──────────────────────────────────────────────────
    def save_resources(self, point_id: str, items: list[dict[str, Any]]) -> int:
        now = time.time()
        count = 0
        with closing(self._connect()) as conn:
            with conn:
                for item in items or []:
                    url = str(item.get("url") or "")
                    if not url:
                        continue
                    conn.execute(
                        "INSERT INTO resources (point_id, title, url, source, created) VALUES (?, ?, ?, ?, ?)",
                        (point_id, str(item.get("title") or "")[:120], url, str(item.get("source") or ""), now),
                    )
                    count += 1
        return count

    def list_resources(self, point_id: str = "", limit: int = 10) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            if point_id:
                return self._read(
                    conn,
                    "SELECT * FROM resources WHERE point_id = ? ORDER BY id DESC LIMIT ?",
                    (point_id, limit),
                )
            return self._read(conn, "SELECT * FROM resources ORDER BY id DESC LIMIT ?", (limit,))

    # ── 会话流水 ──────────────────────────────────────────────
    def add_session(self, kind: str, summary: str) -> int:
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO sessions (kind, summary, created) VALUES (?, ?, ?)",
                    (kind, summary[:2000], time.time()),
                )
                return int(cursor.lastrowid or 0)

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            return self._read(conn, "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))

    def overview(self) -> dict[str, Any]:
        mastery = self.mastery_map()
        tracked = len(mastery)
        average = round(sum(mastery.values()) / tracked, 3) if tracked else 0.0
        return {
            "profile": self.get_profile(),
            "tracked_points": tracked,
            "average_mastery": average,
            "attempts": self.attempt_stats(),
            "due_reviews": len(self.due_reviews()),
            "plan": bool(self.latest_plan()),
            "diagnosis": bool(self.latest_diagnosis()),
            "resources": len(self.list_resources(limit=100)),
        }
