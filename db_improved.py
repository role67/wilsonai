"""
Улучшенная версия Database с правильной обработкой ошибок.
Заменяет молчаливое игнорирование на явные исключения.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from exceptions import (
    DatabaseConnectionError,
    DatabaseQueryError,
    DatabaseIntegrityError,
)

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


class DatabaseImproved:
    """
    Улучшенная версия Database с правильной обработкой ошибок.
    
    Изменения:
    - Явные исключения вместо молчаливого возврата None
    - Разделение критичных и некритичных операций
    - Метрики для мониторинга
    - Оптимизация N+1 запросов
    """
    
    def __init__(self, url: Optional[str]):
        self.url = url
        self.enabled = bool(url)
        self._connection_tested = False
        
        # Метрики для мониторинга
        self.metrics = {
            "queries_total": 0,
            "queries_failed": 0,
            "connection_errors": 0,
            "last_error": None,
        }
    
    async def init(self) -> None:
        """Инициализация БД с проверкой подключения."""
        if not self.enabled:
            logger.info("DATABASE_URL is empty, PostgreSQL memory disabled")
            return
        
        try:
            await self._run(self._init_sync)
            self._connection_tested = True
            logger.info("PostgreSQL memory initialized")
        except Exception as e:
            logger.error("Failed to initialize database: %s", e)
            raise DatabaseConnectionError(f"Failed to initialize database: {e}") from e
    
    async def health_check(self) -> bool:
        """Проверка здоровья БД."""
        if not self.enabled:
            return True
        
        try:
            result = await self._run(self._health_check_sync)
            return result is True
        except Exception:
            return False
    
    def _health_check_sync(self) -> bool:
        """Синхронная проверка здоровья БД."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
    
    async def _run(self, func: Any, *args: Any, critical: bool = False) -> Any:
        """
        Выполнить функцию в отдельном потоке.
        
        Args:
            func: Синхронная функция для выполнения
            *args: Аргументы функции
            critical: Если True, пробрасывает исключения наверх
        
        Returns:
            Результат функции или None при ошибке (если не critical)
        
        Raises:
            DatabaseError: При ошибке в критичной операции
        """
        if not self.enabled:
            return None
        
        self.metrics["queries_total"] += 1
        
        try:
            return await asyncio.to_thread(func, *args)
        except psycopg.OperationalError as e:
            self.metrics["connection_errors"] += 1
            self.metrics["queries_failed"] += 1
            self.metrics["last_error"] = str(e)
            logger.error("Database connection error: %s", e)
            
            if critical:
                raise DatabaseConnectionError(f"Database connection failed: {e}") from e
            return None
        
        except psycopg.IntegrityError as e:
            self.metrics["queries_failed"] += 1
            self.metrics["last_error"] = str(e)
            logger.error("Database integrity error: %s", e)
            
            if critical:
                raise DatabaseIntegrityError(f"Database integrity error: {e}") from e
            return None
        
        except Exception as e:
            self.metrics["queries_failed"] += 1
            self.metrics["last_error"] = str(e)
            logger.exception("Database operation failed")
            
            if critical:
                raise DatabaseQueryError(f"Database query failed: {e}") from e
            return None
    
    def _connect(self) -> psycopg.Connection[Any]:
        """Создать подключение к БД."""
        if not self.url:
            raise DatabaseConnectionError("DATABASE_URL is empty")
        
        try:
            return psycopg.connect(self.url, row_factory=dict_row)
        except Exception as e:
            raise DatabaseConnectionError(f"Failed to connect to database: {e}") from e
    
    def _init_sync(self) -> None:
        """Синхронная инициализация схемы БД."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
    
    async def record_message(self, message: dict[str, Any], critical: bool = False) -> None:
        """
        Записать сообщение в БД.
        
        Args:
            message: Данные сообщения
            critical: Если True, пробросит исключение при ошибке
        """
        await self._run(self._record_message_sync, message, critical=critical)
    
    def _record_message_sync(self, message: dict[str, Any]) -> None:
        """Синхронная запись сообщения."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                # Upsert чата
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
                
                # Upsert отправителя
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
                    
                    # Обновить участника чата
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
                
                # Вставить сообщение
                cur.execute(
                    """
                    insert into telegram_messages
                        (telegram_message_id, chat_id, sender_id, direction, text, has_media, is_trigger, source, telegram_date)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (chat_id, telegram_message_id, direction) do update set
                        text = excluded.text,
                        has_media = excluded.has_media,
                        is_trigger = excluded.is_trigger
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
    
    async def add_moderation_case(
        self,
        chat_id: int,
        user_id: int,
        category: str,
        severity: int,
        reason: str,
        evidence: str,
        source_message_id: Optional[int] = None,
    ) -> None:
        """Добавить случай модерации (критичная операция)."""
        await self._run(
            self._add_moderation_case_sync,
            chat_id,
            user_id,
            category,
            severity,
            reason,
            evidence,
            source_message_id,
            critical=True,  # Критичная операция
        )
    
    def _add_moderation_case_sync(
        self,
        chat_id: int,
        user_id: int,
        category: str,
        severity: int,
        reason: str,
        evidence: str,
        source_message_id: Optional[int],
    ) -> None:
        """Синхронное добавление случая модерации."""
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
    
    async def context_for_chat(
        self, 
        chat_id: int, 
        sender_ids: list[int], 
        limit: int = 35
    ) -> str:
        """
        Получить контекст для чата с оптимизацией N+1 запросов.
        Теперь использует один запрос для всех scope вместо цикла.
        """
        result = await self._run(self._context_for_chat_sync, chat_id, sender_ids, limit)
        return result or ""
    
    def _context_for_chat_sync(self, chat_id: int, sender_ids: list[int], limit: int) -> str:
        """Синхронное получение контекста с оптимизацией."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                # Получить сообщения
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
                
                # Получить заметки одним запросом (оптимизация N+1)
                scopes = [("chat", str(chat_id))]
                scopes.extend(("person", str(sender_id)) for sender_id in sender_ids if sender_id)
                
                if scopes:
                    # Используем VALUES для передачи списка scope
                    scope_values = ",".join(f"('{st}', '{si}')" for st, si in scopes)
                    cur.execute(
                        f"""
                        select scope_type, scope_id, category, key, value
                        from memory_notes
                        where (scope_type, scope_id) in (values {scope_values})
                        order by updated_at desc
                        limit 12
                        """
                    )
                    notes = cur.fetchall()
                else:
                    notes = []
                
                # Получить профили одним запросом
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
        
        # Форматирование результата
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
    
    # Остальные методы аналогично...
    # Для краткости показываю только ключевые изменения
