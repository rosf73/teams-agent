"""투표 상태 저장소 — SQLite.

프로세스가 재시작돼도 진행 중인 투표가 깨지지 않아야 한다. 로컬 호스팅이라
파일 하나로 끝난다.

시각은 **UTC 로 저장**하고 표시할 때만 KST 로 바꾼다. 로컬 시간을 그대로 넣으면
서머타임/타임존 변경 때 조용히 어긋난다.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "VOTE_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "votes.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    activity_id     TEXT,
    service_url     TEXT,
    creator_id      TEXT NOT NULL,
    creator_name    TEXT NOT NULL,
    title           TEXT NOT NULL,
    multi_select    INTEGER NOT NULL DEFAULT 0,
    closes_at       TEXT,
    closed_at       TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_options (
    poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    idx     INTEGER NOT NULL,
    label   TEXT NOT NULL,
    PRIMARY KEY (poll_id, idx)
);
CREATE TABLE IF NOT EXISTS votes (
    poll_id    TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    option_idx INTEGER NOT NULL,
    user_id    TEXT NOT NULL,
    user_name  TEXT NOT NULL,
    voted_at   TEXT NOT NULL,
    PRIMARY KEY (poll_id, option_idx, user_id)
);
CREATE INDEX IF NOT EXISTS idx_polls_open ON polls(closed_at, closes_at);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Option:
    idx: int
    label: str
    voters: list[str] = field(default_factory=list)   # 표시명

    @property
    def count(self) -> int:
        return len(self.voters)


@dataclass
class Poll:
    id: str
    conversation_id: str
    activity_id: str | None
    service_url: str | None
    creator_id: str
    creator_name: str
    title: str
    multi_select: bool
    closes_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    options: list[Option] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    @property
    def total_votes(self) -> int:
        return sum(o.count for o in self.options)

    @property
    def voter_count(self) -> int:
        return len({name for o in self.options for name in o.voters})

    @property
    def leaders(self) -> list[Option]:
        """최다 득표 항목. 동표면 여러 개."""
        top = max((o.count for o in self.options), default=0)
        return [o for o in self.options if o.count == top and top > 0]

    def is_expired(self, at: datetime | None = None) -> bool:
        return bool(self.closes_at and (at or now()) >= self.closes_at)


class Store:
    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── 생성 ──────────────────────────────────────────────────────
    def create_poll(self, *, conversation_id: str, service_url: str | None,
                    creator_id: str, creator_name: str, title: str,
                    options: list[str], multi_select: bool,
                    closes_at: datetime | None) -> Poll:
        poll_id = secrets.token_hex(6)
        created = now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO polls (id, conversation_id, activity_id, service_url,"
                " creator_id, creator_name, title, multi_select, closes_at, closed_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (poll_id, conversation_id, None, service_url, creator_id, creator_name,
                 title, int(multi_select), _iso(closes_at), None, _iso(created)))
            self._conn.executemany(
                "INSERT INTO poll_options (poll_id, idx, label) VALUES (?,?,?)",
                [(poll_id, i, label) for i, label in enumerate(options)])
        return self.get_poll(poll_id)  # type: ignore[return-value]

    def set_activity_id(self, poll_id: str, activity_id: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE polls SET activity_id = ? WHERE id = ?",
                               (activity_id, poll_id))

    # ── 조회 ──────────────────────────────────────────────────────
    def get_poll(self, poll_id: str) -> Poll | None:
        row = self._conn.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        if row is None:
            return None
        poll = Poll(
            id=row["id"], conversation_id=row["conversation_id"],
            activity_id=row["activity_id"], service_url=row["service_url"],
            creator_id=row["creator_id"], creator_name=row["creator_name"],
            title=row["title"], multi_select=bool(row["multi_select"]),
            closes_at=_parse(row["closes_at"]), closed_at=_parse(row["closed_at"]),
            created_at=_parse(row["created_at"]),  # type: ignore[arg-type]
        )
        options = self._conn.execute(
            "SELECT idx, label FROM poll_options WHERE poll_id = ? ORDER BY idx",
            (poll_id,)).fetchall()
        voters: dict[int, list[str]] = {}
        for v in self._conn.execute(
                "SELECT option_idx, user_name FROM votes WHERE poll_id = ?"
                " ORDER BY voted_at", (poll_id,)):
            voters.setdefault(v["option_idx"], []).append(v["user_name"])
        poll.options = [Option(idx=o["idx"], label=o["label"], voters=voters.get(o["idx"], []))
                        for o in options]
        return poll

    def open_polls(self) -> list[Poll]:
        """아직 마감되지 않은 투표. 재시작 후 타이머를 다시 걸 때 쓴다."""
        rows = self._conn.execute(
            "SELECT id FROM polls WHERE closed_at IS NULL ORDER BY created_at").fetchall()
        return [p for p in (self.get_poll(r["id"]) for r in rows) if p]

    # ── 투표 ──────────────────────────────────────────────────────
    def toggle_vote(self, poll: Poll, option_idx: int, user_id: str,
                    user_name: str) -> str:
        """투표를 토글한다. 반환값은 사용자에게 보여줄 결과 문구.

        단일 선택이면 기존 표를 옮긴다. 복수 선택이면 해당 항목만 켜고 끈다.
        """
        existing = self._conn.execute(
            "SELECT option_idx FROM votes WHERE poll_id = ? AND user_id = ?",
            (poll.id, user_id)).fetchall()
        chosen = {r["option_idx"] for r in existing}
        label = next((o.label for o in poll.options if o.idx == option_idx), "?")

        with self._conn:
            if option_idx in chosen:
                self._conn.execute(
                    "DELETE FROM votes WHERE poll_id = ? AND user_id = ? AND option_idx = ?",
                    (poll.id, user_id, option_idx))
                return f"'{label}' 선택을 취소했습니다."

            if not poll.multi_select and chosen:
                self._conn.execute("DELETE FROM votes WHERE poll_id = ? AND user_id = ?",
                                   (poll.id, user_id))
            self._conn.execute(
                "INSERT INTO votes (poll_id, option_idx, user_id, user_name, voted_at)"
                " VALUES (?,?,?,?,?)",
                (poll.id, option_idx, user_id, user_name, _iso(now())))
        moved = (not poll.multi_select) and bool(chosen)
        return f"'{label}' 으로 변경했습니다." if moved else f"'{label}' 에 투표했습니다."

    # ── 마감 ──────────────────────────────────────────────────────
    def close_poll(self, poll_id: str, at: datetime | None = None) -> bool:
        """이미 마감된 투표는 다시 마감하지 않는다 (중복 알림 방지)."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE polls SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (_iso(at or now()), poll_id))
        return cur.rowcount > 0
