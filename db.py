import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row


logger = logging.getLogger("telegram-agent.db")


SCHEMA_SQL = """
create table if not exists telegram_chats (
    chat_id bigint primary key,
    title text,
    chat_type text,
    username text,
    updated_at timestamptz not null default now()
);

create table if not exists telegram_people (
    user_id bigint primary key,
    username text,
    first_name text,
    last_name text,
    display_name text,
    is_bot boolean,
    last_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists chat_participants (
    chat_id bigint not null references telegram_chats(chat_id) on delete cascade,
    user_id bigint not null references telegram_people(user_id) on delete cascade,
    last_seen_at timestamptz not null default now(),
    message_count integer not null default 0,
    primary key (chat_id, user_id)
);

create table if not exists telegram_messages (
    id bigserial primary key,
    telegram_message_id bigint,
    chat_id bigint not null references telegram_chats(chat_id) on delete cascade,
    sender_id bigint references telegram_people(user_id) on delete set null,
    direction text not null check (direction in ('incoming', 'outgoing', 'passive', 'assistant')),
    text text not null default '',
    has_media boolean not null default false,
    is_trigger boolean not null default false,
    source text not null default 'telegram',
    telegram_date timestamptz,
    created_at timestamptz not null default now(),
    unique(chat_id, telegram_message_id, direction)
);

create index if not exists idx_telegram_messages_chat_created
    on telegram_messages(chat_id, created_at desc);
create index if not exists idx_telegram_messages_sender_created
    on telegram_messages(sender_id, created_at desc);

create table if not exists memory_notes (
    id bigserial primary key,
    scope_type text not null check (scope_type in ('global', 'chat', 'person')),
    scope_id text not null,
    category text not null,
    key text not null,
    value text not null,
    confidence real not null default 0.7,
    source_message_db_id bigint references telegram_messages(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(scope_type, scope_id, category, key)
);

create index if not exists idx_memory_notes_scope
    on memory_notes(scope_type, scope_id, category);

create table if not exists behavior_profiles (
    id bigserial primary key,
    scope_type text not null check (scope_type in ('chat', 'person')),
    scope_id text not null,
    summary text not null default '',
    style jsonb not null default '{}'::jsonb,
    boundaries jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    unique(scope_type, scope_id)
);

create table if not exists chat_moderation_cases (
    id bigserial primary key,
    chat_id bigint not null,
    user_id bigint not null,
    source_message_id bigint,
    category text not null,
    severity integer not null default 1,
    reason text not null,
    evidence text not null default '',
    created_at timestamptz not null default now()
);

create table if not exists chat_moderation_actions (
    id bigserial primary key,
    chat_id bigint not null,
    user_id bigint not null,
    action text not null,
    reason text not null,
    duration_seconds integer,
    source text not null default 'auto',
    created_at timestamptz not null default now()
);

create table if not exists chat_user_risk (
    chat_id bigint not null,
    user_id bigint not null,
    risk_score real not null default 0,
    strikes integer not null default 0,
    is_newcomer boolean not null default true,
    last_incident_at timestamptz,
    updated_at timestamptz not null default now(),
    primary key (chat_id, user_id)
);

create table if not exists chat_policy (
    chat_id bigint primary key,
    mode text not null default 'balanced',
    spam_threshold integer not null default 3,
    flood_threshold integer not null default 6,
    abuse_threshold_new integer not null default 3,
    abuse_threshold_old integer not null default 5,
    mute_preferred boolean not null default true,
    updated_at timestamptz not null default now()
);
"""


class Database:
    def __init__(self, url: str | None):
        self.url = url
        self.enabled = bool(url)

    async def init(self) -> None:
        if not self.enabled:
            logger.info("DATABASE_URL is empty, PostgreSQL memory disabled")
            return
        await self._run(self._init_sync)
        logger.info("PostgreSQL memory initialized")

    async def _run(self, func: Any, *args: Any) -> Any:
        if not self.enabled:
            return None
        try:
            return await asyncio.to_thread(func, *args)
        except Exception:
            logger.exception("Database operation failed")
            return None

    def _connect(self) -> psycopg.Connection[Any]:
        if not self.url:
            raise RuntimeError("DATABASE_URL is empty")
        return psycopg.connect(self.url, row_factory=dict_row)

    def _init_sync(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    async def upsert_chat(self, chat: dict[str, Any]) -> None:
        await self._run(self._upsert_chat_sync, chat)

    def _upsert_chat_sync(self, chat: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into telegram_chats (chat_id, title, chat_type, username, updated_at)
                    values (%s, %s, %s, %s, now())
                    on conflict (chat_id) do update set
                        title = excluded.title,
                        chat_type = excluded.chat_type,
                        username = excluded.username,
                        updated_at = now()
                    """,
                    (
                        chat["chat_id"],
                        chat.get("title"),
                        chat.get("chat_type"),
                        chat.get("username"),
                    ),
                )
            conn.commit()

    async def upsert_person(self, person: dict[str, Any]) -> None:
        if not person.get("user_id"):
            return
        await self._run(self._upsert_person_sync, person)

    def _upsert_person_sync(self, person: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into telegram_people
                        (user_id, username, first_name, last_name, display_name, is_bot, last_seen_at, updated_at)
                    values (%s, %s, %s, %s, %s, %s, now(), now())
                    on conflict (user_id) do update set
                        username = excluded.username,
                        first_name = excluded.first_name,
                        last_name = excluded.last_name,
                        display_name = excluded.display_name,
                        is_bot = excluded.is_bot,
                        last_seen_at = now(),
                        updated_at = now()
                    """,
                    (
                        person["user_id"],
                        person.get("username"),
                        person.get("first_name"),
                        person.get("last_name"),
                        person.get("display_name"),
                        person.get("is_bot"),
                    ),
                )
            conn.commit()

    async def record_message(self, message: dict[str, Any]) -> None:
        await self._run(self._record_message_sync, message)

    def _record_message_sync(self, message: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into telegram_chats (chat_id, title, chat_type, username, updated_at)
                    values (%s, %s, %s, %s, now())
                    on conflict (chat_id) do update set updated_at = now()
                    """,
                    (
                        message["chat_id"],
                        message.get("chat_title"),
                        message.get("chat_type"),
                        message.get("chat_username"),
                    ),
                )
                sender_id = message.get("sender_id")
                if sender_id:
                    cur.execute(
                        """
                        insert into telegram_people
                            (user_id, username, first_name, last_name, display_name, is_bot, last_seen_at, updated_at)
                        values (%s, %s, %s, %s, %s, %s, now(), now())
                        on conflict (user_id) do update set
                            username = excluded.username,
                            first_name = excluded.first_name,
                            last_name = excluded.last_name,
                            display_name = excluded.display_name,
                            is_bot = excluded.is_bot,
                            last_seen_at = now(),
                            updated_at = now()
                        """,
                        (
                            sender_id,
                            message.get("sender_username"),
                            message.get("sender_first_name"),
                            message.get("sender_last_name"),
                            message.get("sender_display_name"),
                            message.get("sender_is_bot"),
                        ),
                    )
                    cur.execute(
                        """
                        insert into chat_participants (chat_id, user_id, last_seen_at, message_count)
                        values (%s, %s, now(), 1)
                        on conflict (chat_id, user_id) do update set
                            last_seen_at = now(),
                            message_count = chat_participants.message_count + 1
                        """,
                        (message["chat_id"], sender_id),
                    )

                cur.execute(
                    """
                    insert into telegram_messages
                        (telegram_message_id, chat_id, sender_id, direction, text, has_media, is_trigger, source, telegram_date)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (chat_id, telegram_message_id, direction) do update set
                        text = excluded.text,
                        has_media = excluded.has_media,
                        is_trigger = excluded.is_trigger
                    returning id
                    """,
                    (
                        message.get("telegram_message_id"),
                        message["chat_id"],
                        sender_id,
                        message.get("direction", "incoming"),
                        message.get("text") or "",
                        bool(message.get("has_media")),
                        bool(message.get("is_trigger")),
                        message.get("source") or "telegram",
                        message.get("telegram_date"),
                    ),
                )
            conn.commit()

    async def add_note(
        self,
        scope_type: str,
        scope_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.7,
    ) -> None:
        await self._run(self._add_note_sync, scope_type, scope_id, category, key, value, confidence)

    async def list_notes(self, scope_type: str, scope_id: str, limit: int = 20) -> list[dict[str, Any]]:
        result = await self._run(self._list_notes_sync, scope_type, scope_id, limit)
        return result or []

    async def deactivate_note(self, scope_type: str, scope_id: str, needle: str) -> int:
        result = await self._run(self._deactivate_note_sync, scope_type, scope_id, needle)
        return int(result or 0)

    async def clear_notes(self, scope_type: str, scope_id: str) -> int:
        result = await self._run(self._clear_notes_sync, scope_type, scope_id)
        return int(result or 0)

    def _add_note_sync(
        self,
        scope_type: str,
        scope_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into memory_notes
                        (scope_type, scope_id, category, key, value, confidence, updated_at)
                    values (%s, %s, %s, %s, %s, %s, now())
                    on conflict (scope_type, scope_id, category, key) do update set
                        value = excluded.value,
                        confidence = greatest(memory_notes.confidence, excluded.confidence),
                        updated_at = now()
                    """,
                    (scope_type, scope_id, category, key, value, confidence),
                )
            conn.commit()

    def _list_notes_sync(self, scope_type: str, scope_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select scope_type, scope_id, category, key, value, confidence, updated_at
                    from memory_notes
                    where scope_type = %s and scope_id = %s and key not like 'deleted:%'
                    order by
                        case when key like 'pinned:%' then 0 else 1 end asc,
                        updated_at desc
                    limit %s
                    """,
                    (scope_type, scope_id, max(1, min(limit, 100))),
                )
                return [dict(row) for row in cur.fetchall()]

    def _deactivate_note_sync(self, scope_type: str, scope_id: str, needle: str) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update memory_notes
                    set key = 'deleted:' || key, updated_at = now()
                    where scope_type = %s and scope_id = %s and key not like 'deleted:%'
                      and (key ilike %s or value ilike %s)
                    """,
                    (scope_type, scope_id, f"%{needle}%", f"%{needle}%"),
                )
                count = cur.rowcount or 0
            conn.commit()
        return count

    def _clear_notes_sync(self, scope_type: str, scope_id: str) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update memory_notes
                    set key = 'deleted:' || key, updated_at = now()
                    where scope_type = %s and scope_id = %s and key not like 'deleted:%'
                    """,
                    (scope_type, scope_id),
                )
                count = cur.rowcount or 0
            conn.commit()
        return count

    async def context_for_chat(self, chat_id: int, sender_ids: list[int], limit: int = 35) -> str:
        result = await self._run(self._context_for_chat_sync, chat_id, sender_ids, limit)
        return result or ""

    async def participant_stats(self, chat_id: int, user_id: int) -> dict[str, Any] | None:
        return await self._run(self._participant_stats_sync, chat_id, user_id)

    async def moderation_policy(self, chat_id: int) -> dict[str, Any]:
        result = await self._run(self._moderation_policy_sync, chat_id)
        return result or {
            "mode": "balanced",
            "spam_threshold": 3,
            "flood_threshold": 6,
            "abuse_threshold_new": 3,
            "abuse_threshold_old": 5,
            "mute_preferred": True,
        }

    async def upsert_risk(self, chat_id: int, user_id: int, risk_score: float, strikes: int, is_newcomer: bool) -> None:
        await self._run(self._upsert_risk_sync, chat_id, user_id, risk_score, strikes, is_newcomer)

    async def add_moderation_case(
        self,
        chat_id: int,
        user_id: int,
        category: str,
        severity: int,
        reason: str,
        evidence: str,
        source_message_id: int | None = None,
    ) -> None:
        await self._run(
            self._add_moderation_case_sync,
            chat_id,
            user_id,
            category,
            severity,
            reason,
            evidence,
            source_message_id,
        )

    async def add_moderation_action(
        self,
        chat_id: int,
        user_id: int,
        action: str,
        reason: str,
        duration_seconds: int | None = None,
        source: str = "auto",
    ) -> None:
        await self._run(
            self._add_moderation_action_sync,
            chat_id,
            user_id,
            action,
            reason,
            duration_seconds,
            source,
        )

    def _participant_stats_sync(self, chat_id: int, user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select message_count, last_seen_at
                    from chat_participants
                    where chat_id = %s and user_id = %s
                    """,
                    (chat_id, user_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def _moderation_policy_sync(self, chat_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select * from chat_policy where chat_id = %s", (chat_id,))
                row = cur.fetchone()
                if row:
                    return dict(row)
                cur.execute("insert into chat_policy (chat_id) values (%s) on conflict do nothing", (chat_id,))
            conn.commit()
        return None

    def _upsert_risk_sync(self, chat_id: int, user_id: int, risk_score: float, strikes: int, is_newcomer: bool) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chat_user_risk (chat_id, user_id, risk_score, strikes, is_newcomer, last_incident_at, updated_at)
                    values (%s, %s, %s, %s, %s, now(), now())
                    on conflict (chat_id, user_id) do update set
                        risk_score = excluded.risk_score,
                        strikes = excluded.strikes,
                        is_newcomer = excluded.is_newcomer,
                        last_incident_at = now(),
                        updated_at = now()
                    """,
                    (chat_id, user_id, risk_score, strikes, is_newcomer),
                )
            conn.commit()

    def _add_moderation_case_sync(
        self,
        chat_id: int,
        user_id: int,
        category: str,
        severity: int,
        reason: str,
        evidence: str,
        source_message_id: int | None,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chat_moderation_cases
                        (chat_id, user_id, source_message_id, category, severity, reason, evidence)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (chat_id, user_id, source_message_id, category, severity, reason, evidence[:500]),
                )
            conn.commit()

    def _add_moderation_action_sync(
        self,
        chat_id: int,
        user_id: int,
        action: str,
        reason: str,
        duration_seconds: int | None,
        source: str,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chat_moderation_actions (chat_id, user_id, action, reason, duration_seconds, source)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    (chat_id, user_id, action, reason, duration_seconds, source),
                )
            conn.commit()

    def _context_for_chat_sync(self, chat_id: int, sender_ids: list[int], limit: int) -> str:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select direction, text, sender_id, created_at
                    from telegram_messages
                    where chat_id = %s and text <> ''
                    order by created_at desc
                    limit %s
                    """,
                    (chat_id, limit),
                )
                messages = list(reversed(cur.fetchall()))

                scopes = [("chat", str(chat_id))]
                scopes.extend(("person", str(sender_id)) for sender_id in sender_ids if sender_id)
                notes: list[dict[str, Any]] = []
                for scope_type, scope_id in scopes:
                    cur.execute(
                        """
                        select scope_type, scope_id, category, key, value
                        from memory_notes
                        where scope_type = %s and scope_id = %s
                        order by updated_at desc
                        limit 12
                        """,
                        (scope_type, scope_id),
                    )
                    notes.extend(cur.fetchall())

                person_scope_ids = [str(sender_id) for sender_id in sender_ids if sender_id]
                if person_scope_ids:
                    cur.execute(
                        """
                        select scope_type, scope_id, summary, style, boundaries
                        from behavior_profiles
                        where (scope_type = 'chat' and scope_id = %s)
                           or (scope_type = 'person' and scope_id = any(%s::text[]))
                        order by updated_at desc
                        limit 10
                        """,
                        (str(chat_id), person_scope_ids),
                    )
                else:
                    cur.execute(
                        """
                        select scope_type, scope_id, summary, style, boundaries
                        from behavior_profiles
                        where scope_type = 'chat' and scope_id = %s
                        order by updated_at desc
                        limit 10
                        """,
                        (str(chat_id),),
                    )
                profiles = cur.fetchall()

        lines: list[str] = []
        if notes:
            lines.append("Долгая память:")
            for note in notes:
                lines.append(
                    f"- {note['scope_type']}:{note['scope_id']} | {note['category']}:{note['key']} = {note['value']}"
                )
        if profiles:
            lines.append("Поведенческие профили:")
            for profile in profiles:
                style = json.dumps(profile["style"], ensure_ascii=False) if profile["style"] else "{}"
                boundaries = (
                    json.dumps(profile["boundaries"], ensure_ascii=False)
                    if profile["boundaries"]
                    else "{}"
                )
                lines.append(
                    f"- {profile['scope_type']}:{profile['scope_id']} | {profile['summary']} | style={style} | boundaries={boundaries}"
                )
        if messages:
            lines.append("Недавний контекст из БД:")
            for item in messages[-limit:]:
                created = item["created_at"]
                if isinstance(created, datetime):
                    created_text = created.astimezone(timezone.utc).isoformat(timespec="minutes")
                else:
                    created_text = str(created)
                text = (item["text"] or "").replace("\n", " ")[:500]
                lines.append(f"- {created_text} | {item['direction']} | {item['sender_id']} | {text}")
        return "\n".join(lines)
