"""Shared Telegram UI helpers for the admin bot."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


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
                callback_data=f"{item_prefix}:{u}",
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
