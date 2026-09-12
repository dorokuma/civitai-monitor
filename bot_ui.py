"""Shared Telegram UI helpers for the admin bot."""

from __future__ import annotations

import hashlib

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram hard limit: callback_data must be 1-64 bytes.
_CALLBACK_DATA_MAX_BYTES = 64

# In-process mapping of short_hash -> username, used when a username would
# push a row's callback_data past Telegram's 64-byte limit.
#
# Lifecycle & limits: entries are appended when a keyboard is rendered and
# live for as long as the bot process — the same lifetime as the inline
# panels themselves. After a bot restart the mapping is empty, so buttons
# rendered by a previous process can no longer be resolved to a username
# (the consumer falls back to exact-name matching and rejects the unknown
# segment; the user simply re-opens the panel). The 12-hex-char hash gives a
# 48-bit space — ample for a watch list of a few dozen names. On the
# astronomically unlikely collision the first username wins and only that
# one button could mis-resolve.
_CALLBACK_HASH_TO_USERNAME: dict[str, str] = {}


def _short_hash(username: str) -> str:
    return hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]


def _encode_callback_data(item_prefix: str, username: str) -> str:
    """callback_data for a user row; short-hashed when it would exceed 64 bytes."""
    data = f"{item_prefix}:{username}"
    if len(data.encode("utf-8")) <= _CALLBACK_DATA_MAX_BYTES:
        return data
    short = _short_hash(username)
    _CALLBACK_HASH_TO_USERNAME.setdefault(short, username)
    return f"{item_prefix}:{short}"


def resolve_callback_username(raw: str) -> str:
    """Map a callback_data username segment back to the real username.

    Short-hash segments are resolved via the in-process mapping; anything
    else (plain names, segments from keyboards rendered by an older bot
    process) is returned unchanged so consumers can match by exact name.
    """
    return _CALLBACK_HASH_TO_USERNAME.get(raw, raw)


def paginated_user_keyboard(
    users: list[str],
    page: int,
    *,
    item_prefix: str,
    item_label_fmt: str,
    page_prefix: str,
    close_data: str,
    per_page: int = 8,
) -> tuple[InlineKeyboardMarkup, str, int]:
    """Build a paginated user list keyboard shared by remove / backfill UIs.

    Args:
        users: Full sorted/display list of usernames.
        page: Zero-based page index (clamped).
        item_prefix: Callback prefix for row buttons (e.g. ``"rem"`` or ``"bf"``).
        item_label_fmt: Format string with ``{u}`` for the username
            (e.g. ``"❌ @{u}"`` or ``"⏳ @{u}"``).
        page_prefix: Callback prefix for page nav (e.g. ``"rem_pg"``).
        close_data: Full callback_data for the close button.
        per_page: Users per page.

    Returns:
        (markup, total_text, page) where ``page`` is the clamped page index
        and ``total_text`` is a Chinese summary like ``👥 共 N 个…``.
    """
    if not users:
        return InlineKeyboardMarkup([]), "👥 共 0 个监控对象", 0

    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]

    keyboard: list[list[InlineKeyboardButton]] = []
    for u in page_users:
        keyboard.append([
            InlineKeyboardButton(
                item_label_fmt.format(u=u),
                callback_data=_encode_callback_data(item_prefix, u),
            )
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"{page_prefix}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"{page_prefix}:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔒 关闭", callback_data=close_data)])

    if total_pages <= 1:
        total_text = f"👥 共 {len(users)} 个监控对象"
    else:
        total_text = f"👥 共 {len(users)} 个（第 {page + 1}/{total_pages} 页）"

    return InlineKeyboardMarkup(keyboard), total_text, page
