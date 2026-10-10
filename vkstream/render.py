"""Сообщение ВК -> текст (HTML Telegram) и список вложений для загрузки."""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field

from .vk.peers import Names

MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # лимит Bot API на отправку файла — 50 МБ
MAX_FWD_DEPTH = 3
MAX_FWD_COUNT = 20
QUOTE_PREFIX = "┃ "

KIND_ICONS = {"user": "👤", "chat": "💬", "group": "📢", "email": "✉️"}

ATTACHMENT_LABELS = {
    "gift": "🎁 Подарок",
    "story": "📖 История",
    "audio_playlist": "🎶 Плейлист",
    "podcast": "🎙 Подкаст",
    "money_transfer": "💸 Денежный перевод",
    "money_request": "💸 Запрос денег",
    "mini_app": "🧩 Мини-приложение",
    "video_message": "⏺ Видеосообщение",
    "narrative": "📚 Сюжет",
    "event": "📅 Встреча",
    "group_call_in_progress": "📞 Групповой звонок",
    "curator": "🎤 Исполнитель",
    "album": "🖼 Альбом",
    "market_album": "🛍 Подборка товаров",
}

SNIPPET_LABELS = {
    "photo": "Фото",
    "video": "Видео",
    "audio": "Аудио",
    "doc": "Файл",
    "audio_message": "Голосовое сообщение",
    "sticker": "Стикер",
    "graffiti": "Граффити",
    "link": "Ссылка",
    "wall": "Запись со стены",
    "poll": "Опрос",
}

_MENTION = re.compile(r"\[(id|club|public|event)(\d+)\|([^\]\n]+)\]")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Media:
    kind: str  # photo | document | animation | voice
    url: str
    filename: str = ""
    duration: int = 0


@dataclass
class Rendered:
    text: str
    media: list[Media] = field(default_factory=list)


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def strip_tags(text: str) -> str:
    """HTML Telegram -> обычный текст."""
    return html.unescape(_TAG.sub("", text))


def message_url(origin: str, peer_id: int, cmid: int) -> str:
    return f"{origin}/im/convo/{peer_id}?cmid={cmid}"


def format_text(text: str) -> str:
    """Экранировать текст и превратить упоминания [id1|Имя] в ссылки."""
    return _MENTION.sub(lambda m: f'<a href="https://vk.com/{m[1]}{m[2]}">{m[3]}</a>', esc(text))


def render_message(
    msg: dict, *, title: str, kind: str, names: Names, now: float | None = None
) -> Rendered:
    media: list[Media] = []
    head = f"{KIND_ICONS.get(kind, '💬')} <b>{esc(title)}</b>"
    # Сообщение, догнанное после простоя, помечаем временем отправки
    date = msg.get("date")
    if date and (now or time.time()) - date > 180:
        head += f" · <i>{_fmt_date(date, now)}</i>"
    lines = [head]
    if msg.get("out"):
        lines.append("👤 <b>Вы</b>")
    elif kind == "chat":
        lines.append(f"👤 <b>{esc(names.get(msg.get('from_id')))}</b>")
    body = _body(msg, names, media, depth=0)
    if not body and not media:
        body = ["<i>(сообщение без текста)</i>"]
    return Rendered("\n".join(lines + body), media)


def _body(msg: dict, names: Names, media: list[Media], depth: int) -> list[str]:
    lines: list[str] = []
    reply = msg.get("reply_message")
    if reply:
        quote = f"↪️ {esc(names.get(reply.get('from_id')))}: {esc(_snippet(reply))}"
        # blockquote в Telegram нельзя вкладывать и резать префиксом — только на верхнем уровне
        lines.append(f"<blockquote>{quote}</blockquote>" if depth == 0 else f"<i>{quote}</i>")
    if msg.get("action"):
        lines.append(f"<i>{esc(_action(msg['action'], msg.get('from_id'), names))}</i>")
    if msg.get("text"):
        lines.append(format_text(msg["text"]))
    for att in msg.get("attachments") or []:
        lines += _attachment(att, media, depth)
    if msg.get("geo"):
        lines.append(_geo(msg["geo"]))
    forwarded = msg.get("fwd_messages") or []
    if forwarded and depth >= MAX_FWD_DEPTH:
        lines.append(f"{QUOTE_PREFIX}… ещё пересланные сообщения")
        return lines
    for fwd in forwarded[:MAX_FWD_COUNT]:
        inner = [f"↗️ <b>{esc(names.get(fwd.get('from_id')))}</b> <i>{_fmt_date(fwd.get('date'))}</i>"]
        inner += _body(fwd, names, media, depth + 1) or ["<i>(без текста)</i>"]
        lines += [QUOTE_PREFIX + line for block in inner for line in block.split("\n")]
    return lines


def _attachment(att: dict, media: list[Media], depth: int) -> list[str]:
    kind = att.get("type") or ""
    obj = att.get(kind) or {}

    if kind == "photo":
        url = _photo_url(obj)
        if not url:
            return ["🖼 Фото"]
        media.append(Media("photo", url, f"photo{obj.get('owner_id', '')}_{obj.get('id', '')}.jpg"))
        return []

    if kind == "sticker":
        url = _sticker_url(obj)
        if not url:
            return ["🧩 Стикер"]
        media.append(Media("photo", url, "sticker.png"))
        return []

    if kind == "graffiti":
        if not obj.get("url"):
            return ["🎨 Граффити"]
        media.append(Media("photo", obj["url"], "graffiti.png"))
        return []

    if kind == "audio_message":
        url = obj.get("link_ogg") or obj.get("link_mp3")
        transcript = (obj.get("transcript") or "").strip()
        if not url:
            return ["🎤 Голосовое сообщение" + (f": <i>{esc(transcript)}</i>" if transcript else "")]
        ext = "ogg" if obj.get("link_ogg") else "mp3"
        media.append(Media("voice", url, f"voice.{ext}", int(obj.get("duration") or 0)))
        return [f"🎤 <i>{esc(transcript)}</i>"] if transcript else []

    if kind == "doc":
        title = obj.get("title") or "файл"
        ext = (obj.get("ext") or "").lower()
        if ext and not title.lower().endswith("." + ext):
            title = f"{title}.{ext}"
        url, size = obj.get("url"), int(obj.get("size") or 0)
        if not url:
            return [f"📎 {esc(title)}"]
        if size > MAX_UPLOAD_BYTES:
            return [f"📎 {_link(url, title)} ({_fmt_size(size)}) — слишком большой для Telegram"]
        media.append(Media("animation" if ext == "gif" else "document", url, title))
        return []

    if kind == "video":
        title = obj.get("title") or "Видео"
        if obj.get("owner_id") is None or obj.get("id") is None:
            return [f"🎬 {esc(title)}"]
        url = "https://vk.com/video{}_{}".format(obj["owner_id"], obj["id"])
        line = f"🎬 {_link(url, title)}"
        if obj.get("duration"):
            line += f" ({_fmt_duration(obj['duration'])})"
        return [line]

    if kind == "audio":
        return [f"🎵 {esc(obj.get('artist') or '?')} — {esc(obj.get('title') or '?')}"]

    if kind == "link":
        url = obj.get("url") or ""
        return [f"🔗 {_link(url, obj.get('title') or url)}"] if url else []

    if kind == "wall":
        owner = obj.get("owner_id") or obj.get("to_id") or obj.get("from_id")
        url = "https://vk.com/wall{}_{}".format(owner, obj.get("id"))
        lines = [f"📰 {_link(url, 'Запись со стены')}"]
        if obj.get("text"):
            lines.append(format_text(_clip(obj["text"], 1500)))
        if depth < MAX_FWD_DEPTH:
            for nested in obj.get("attachments") or []:
                lines += _attachment(nested, media, depth + 1)
        return lines

    if kind == "wall_reply":
        return [f"💬 Комментарий: {format_text(_clip(obj.get('text') or '', 500))}"]

    if kind == "poll":
        lines = [f"📊 Опрос: {esc(obj.get('question') or '')}"]
        lines += [f"  • {esc(a.get('text') or '')}" for a in obj.get("answers") or []]
        return lines

    if kind == "market":
        price = (obj.get("price") or {}).get("text")
        return [f"🛍 {esc(obj.get('title') or 'Товар')}" + (f" — {esc(price)}" if price else "")]

    if kind == "article":
        title = obj.get("title") or "Статья"
        return [f"📰 {_link(obj['url'], title)}" if obj.get("url") else f"📰 {esc(title)}"]

    if kind == "call":
        duration = obj.get("duration")
        return ["📞 Звонок" + (f" ({_fmt_duration(duration)})" if duration else "")]

    return [ATTACHMENT_LABELS.get(kind) or f"📎 Вложение: {esc(kind or '?')}"]


def _photo_url(photo: dict) -> str | None:
    sizes = [s for s in photo.get("sizes") or [] if s.get("url")]
    if sizes:
        return max(sizes, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))["url"]
    return (photo.get("orig_photo") or {}).get("url")


def _sticker_url(sticker: dict) -> str | None:
    images = [i for i in sticker.get("images") or [] if i.get("url")]
    if not images:
        return None
    fitting = [i for i in images if (i.get("width") or 0) <= 512] or images
    return max(fitting, key=lambda i: i.get("width") or 0)["url"]


def _geo(geo: dict) -> str:
    coords = geo.get("coordinates") or {}
    title = (geo.get("place") or {}).get("title") or "Местоположение"
    lat, lon = coords.get("latitude"), coords.get("longitude")
    if lat is None or lon is None:
        return f"📍 {esc(title)}"
    return f"📍 {_link(f'https://yandex.ru/maps/?pt={lon},{lat}&z=16&l=map', title)}"


def _action(action: dict, from_id: int | None, names: Names) -> str:
    kind = action.get("type") or ""
    member = action.get("member_id")
    text = action.get("text") or ""
    who = names.get(member) if member else ""
    if kind == "chat_invite_user":
        return "вернулся(-ась) в беседу" if member == from_id else f"пригласил(а) {who}"
    if kind == "chat_kick_user":
        return "вышел(-ла) из беседы" if member == from_id else f"исключил(а) {who}"
    return {
        "chat_create": f"создал(а) беседу «{text}»",
        "chat_title_update": f"изменил(а) название беседы на «{text}»",
        "chat_photo_update": "обновил(а) фотографию беседы",
        "chat_photo_remove": "удалил(а) фотографию беседы",
        "chat_pin_message": "закрепил(а) сообщение" + (f": «{action['message']}»" if action.get("message") else ""),
        "chat_unpin_message": "открепил(а) сообщение",
        "chat_invite_user_by_link": "присоединился(-ась) по ссылке",
        "chat_screenshot": "сделал(а) скриншот беседы",
        "conversation_style_update": "изменил(а) оформление чата",
    }.get(kind, f"действие в беседе: {kind}")


def _snippet(msg: dict, limit: int = 150) -> str:
    text = (msg.get("text") or "").strip()
    if text:
        return _clip(text.replace("\n", " "), limit)
    for att in msg.get("attachments") or []:
        return SNIPPET_LABELS.get(att.get("type"), "Вложение")
    if msg.get("fwd_messages"):
        return "Пересланные сообщения"
    return "Сообщение"


def _link(url: str, title: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")) or any(c.isspace() for c in url):
        return esc(title)
    return f'<a href="{html.escape(url, quote=True)}">{esc(title)}</a>'


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_date(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    then, today = time.localtime(ts), time.localtime(now or time.time())
    same_day = (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday)
    return time.strftime("%H:%M" if same_day else "%d.%m.%Y %H:%M", then)


def _fmt_duration(seconds: int) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"
