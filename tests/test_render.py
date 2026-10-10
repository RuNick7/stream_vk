import time

from vkstream.render import MAX_UPLOAD_BYTES, message_url, render_message, strip_tags
from vkstream.vk.peers import Names


def names() -> Names:
    n = Names()
    n.update(
        {
            "profiles": [
                {"id": 1, "first_name": "Анна", "last_name": "Смирнова"},
                {"id": 2, "first_name": "Пётр", "last_name": "Ильин"},
            ],
            "groups": [{"id": 5, "name": "Новости <города>"}],
        }
    )
    return n


def render(msg: dict, kind: str = "chat", title: str = "Чат") -> "Rendered":
    msg.setdefault("date", int(time.time()))
    return render_message(msg, title=title, kind=kind, names=names())


def test_chat_message_shows_title_author_and_escapes_html():
    r = render({"from_id": 1, "text": "2 < 3 & <b>жирный</b>"}, title="Отдел <R&D>")
    assert r.text == (
        "💬 <b>Отдел &lt;R&amp;D&gt;</b>\n"
        "👤 <b>Анна Смирнова</b>\n"
        "2 &lt; 3 &amp; &lt;b&gt;жирный&lt;/b&gt;"
    )
    assert r.media == []


def test_private_dialog_has_no_separate_author_line():
    r = render({"from_id": 1, "text": "привет"}, kind="user", title="Анна Смирнова")
    assert r.text == "👤 <b>Анна Смирнова</b>\nпривет"


def test_outgoing_and_community_author():
    assert "👤 <b>Вы</b>" in render({"from_id": 9, "out": 1, "text": "ок"}, kind="user").text
    assert "👤 <b>Новости &lt;города&gt;</b>" in render({"from_id": -5, "text": "пост"}).text


def test_mentions_become_links():
    r = render({"from_id": 1, "text": "[id2|Пётр], глянь; и [club5|паблик]"})
    assert '<a href="https://vk.com/id2">Пётр</a>, глянь' in r.text
    assert '<a href="https://vk.com/club5">паблик</a>' in r.text


def test_photo_uses_largest_size():
    photo = {
        "owner_id": 1,
        "id": 9,
        "sizes": [
            {"type": "s", "width": 75, "height": 50, "url": "https://cdn/s.jpg"},
            {"type": "w", "width": 2560, "height": 1440, "url": "https://cdn/w.jpg"},
            {"type": "x", "width": 604, "height": 400, "url": "https://cdn/x.jpg"},
        ],
    }
    r = render({"from_id": 1, "text": "", "attachments": [{"type": "photo", "photo": photo}]})
    assert [(m.kind, m.url) for m in r.media] == [("photo", "https://cdn/w.jpg")]
    assert "без текста" not in r.text


def test_voice_message_with_transcript():
    att = {
        "type": "audio_message",
        "audio_message": {"link_ogg": "https://cdn/v.ogg", "duration": 7, "transcript": "буду в пять"},
    }
    r = render({"from_id": 1, "attachments": [att]})
    assert (r.media[0].kind, r.media[0].filename, r.media[0].duration) == ("voice", "voice.ogg", 7)
    assert "🎤 <i>буду в пять</i>" in r.text


def test_documents_gif_and_oversized():
    def doc(**fields):
        return {"type": "doc", "doc": {"url": "https://vk.ru/doc1_2", **fields}}

    r = render(
        {
            "from_id": 1,
            "attachments": [
                doc(title="Отчёт", ext="pdf", size=1000),
                doc(title="cat.gif", ext="gif", size=1000),
                doc(title="фильм.mkv", ext="mkv", size=MAX_UPLOAD_BYTES + 1),
            ],
        }
    )
    assert [(m.kind, m.filename) for m in r.media] == [("document", "Отчёт.pdf"), ("animation", "cat.gif")]
    assert '<a href="https://vk.ru/doc1_2">фильм.mkv</a>' in r.text and "слишком большой" in r.text


def test_sticker_video_link_poll_and_unknown():
    r = render(
        {
            "from_id": 1,
            "attachments": [
                {"type": "sticker", "sticker": {"images": [{"url": "https://cdn/128.png", "width": 128}, {"url": "https://cdn/512.png", "width": 512}]}},
                {"type": "video", "video": {"owner_id": -5, "id": 77, "title": "Клип", "duration": 125}},
                {"type": "link", "link": {"url": "https://example.com/?a=1&b=2", "title": "Сайт"}},
                {"type": "poll", "poll": {"question": "Когда?", "answers": [{"text": "Сегодня"}, {"text": "Завтра"}]}},
                {"type": "hologram", "hologram": {}},
            ],
        }
    )
    assert r.media[0].url == "https://cdn/512.png"
    assert '🎬 <a href="https://vk.com/video-5_77">Клип</a> (2:05)' in r.text
    assert '🔗 <a href="https://example.com/?a=1&amp;b=2">Сайт</a>' in r.text
    assert "📊 Опрос: Когда?\n  • Сегодня\n  • Завтра" in r.text
    assert "📎 Вложение: hologram" in r.text


def test_reply_is_quoted():
    r = render({"from_id": 1, "text": "да", "reply_message": {"from_id": 2, "text": "идём?\nв шесть"}})
    assert "<blockquote>↪️ Пётр Ильин: идём? в шесть</blockquote>\nда" in r.text


def test_forwarded_messages_are_nested_and_keep_media():
    photo = {"type": "photo", "photo": {"sizes": [{"width": 10, "height": 10, "url": "https://cdn/p.jpg"}]}}
    inner = {"from_id": 2, "date": 1_700_000_000, "text": "глубже", "attachments": [photo]}
    outer = {"from_id": 1, "date": 1_700_000_000, "text": "строка 1\nстрока 2", "fwd_messages": [inner]}
    r = render({"from_id": 1, "text": "смотри", "fwd_messages": [outer]})
    lines = r.text.split("\n")
    assert lines[2] == "смотри"
    assert lines[3].startswith("┃ ↗️ <b>Анна Смирнова</b>")
    assert lines[4:6] == ["┃ строка 1", "┃ строка 2"]
    assert lines[6].startswith("┃ ┃ ↗️ <b>Пётр Ильин</b>")
    assert lines[7] == "┃ ┃ глубже"
    assert len(r.media) == 1


def test_chat_actions():
    r = render({"from_id": 1, "action": {"type": "chat_invite_user", "member_id": 2}})
    assert "<i>пригласил(а) Пётр Ильин</i>" in r.text
    r = render({"from_id": 1, "action": {"type": "chat_kick_user", "member_id": 1}})
    assert "<i>вышел(-ла) из беседы</i>" in r.text


def test_old_message_is_stamped_with_its_time_and_empty_gets_placeholder():
    r = render({"from_id": 1, "date": int(time.time()) - 86400 * 2})
    first, *_ = r.text.split("\n")
    assert "· <i>" in first
    assert r.text.endswith("<i>(сообщение без текста)</i>")
    assert "· <i>" not in render({"from_id": 1, "text": "свежее"}).text


def test_strip_tags_and_message_url():
    assert strip_tags('<b>A &amp; B</b> <a href="https://x">ссылка</a>') == "A & B ссылка"
    assert message_url("https://vk.ru", 2_000_000_001, 15) == "https://vk.ru/im/convo/2000000001?cmid=15"
