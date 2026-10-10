import time

from vkstream.vk.cookies import Cookie, CookieStore


def test_header_only_for_matching_hosts():
    store = CookieStore(
        [
            Cookie("remixsid", "abc", "vk.ru"),
            Cookie("p", "xyz", "login.vk.ru", host_only=True),
        ]
    )
    assert store.header_for("https://login.vk.ru/?act=web_token") == "remixsid=abc; p=xyz"
    assert store.header_for("https://web.api.vk.ru/method/users.get") == "remixsid=abc"
    # сессия не должна утекать на CDN и чужие домены
    assert store.header_for("https://sun9-1.userapi.com/file.jpg") == ""
    assert store.header_for("https://evilvk.ru/") == ""


def test_expired_and_path_scoped_cookies_are_skipped():
    store = CookieStore(
        [
            Cookie("old", "1", "vk.ru", expires=time.time() - 10),
            Cookie("scoped", "2", "vk.ru", path="/im"),
        ]
    )
    assert store.header_for("https://vk.ru/feed") == ""
    assert store.header_for("https://vk.ru/im/convo/1") == "scoped=2"
    assert store.header_for("https://vk.ru/images") == ""


def test_set_cookie_headers_update_and_delete():
    store = CookieStore([Cookie("remixsid", "old", "vk.ru")])
    changed = store.update_from_headers(
        "https://login.vk.ru/?act=web_token",
        [
            "remixsid=new; expires=Fri, 01 Jan 2100 00:00:00 GMT; path=/; domain=.vk.ru; secure; HttpOnly",
            "p=host; path=/",
        ],
    )
    assert changed
    assert store.get("remixsid") == "new"
    assert store.header_for("https://vk.ru/") == "remixsid=new"  # p привязан к login.vk.ru
    assert "p=host" in store.header_for("https://login.vk.ru/")

    store.update_from_headers(
        "https://login.vk.ru/", ["remixsid=DELETED; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; domain=.vk.ru"]
    )
    assert store.get("remixsid") is None


def test_unchanged_set_cookie_reports_no_change():
    store = CookieStore([Cookie("a", "1", "vk.ru", host_only=True)])
    assert not store.update_from_headers("https://vk.ru/", ["a=1; path=/"])


def test_import_from_devtools_header_and_browser():
    store = CookieStore.from_header("Cookie: remixsid=abc; remixlang=0; p=q=1")
    assert store.get("remixsid") == "abc"
    assert store.get("p") == "q=1"
    assert "remixsid=abc" in store.header_for("https://login.vk.ru/")

    browser = CookieStore.from_browser(
        [
            {"name": "remixsid", "value": "abc", "domain": ".vk.ru", "path": "/", "expires": -1},
            {"name": "p", "value": "xyz", "domain": "login.vk.ru", "path": "/", "expires": time.time() + 100},
            {"name": "tracker", "value": "1", "domain": ".mail.ru", "path": "/", "expires": -1},
        ]
    )
    assert {c.name for c in browser} == {"remixsid", "p"}
    assert browser.header_for("https://vk.ru/") == "remixsid=abc"


def test_roundtrip_through_json():
    store = CookieStore([Cookie("remixsid", "abc", "vk.ru", expires=time.time() + 100)])
    assert CookieStore.from_list(store.to_list()).header_for("https://vk.ru/") == "remixsid=abc"
