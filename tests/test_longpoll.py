from vkstream.vk.longpoll import peers_in_update

CHAT = 2_000_000_001
WATCHED = {CHAT, 42, -7}


def test_classic_new_message_event():
    assert peers_in_update([4, 1354, 1, CHAT, 1790006688, "текст", {"from": "5"}, {}], WATCHED) == {CHAT}
    assert peers_in_update([4, 1354, 1, 999, 1790006688, "текст"], WATCHED) == set()


def test_web_messenger_new_message_event():
    update = [10004, 74, 33, 1354, 42, 1790006688, "text", {"title": "..."}, {}, -115, 1354, 0]
    assert peers_in_update(update, WATCHED) == {42}


def test_community_dialog_and_nested_payload():
    assert peers_in_update([10018, {"peer_id": -7, "items": [[1, 2, CHAT]]}], WATCHED) == {-7, CHAT}


def test_noise_events_do_not_trigger():
    assert peers_in_update([63, CHAT, [42], 1, 1790006688], WATCHED) == set()  # «печатает…»
    assert peers_in_update([7, 42, 1354], WATCHED) == set()  # прочтение
    assert peers_in_update([10006, 42, 1354], WATCHED) == set()  # то же в нумерации веб-версии
    assert peers_in_update([80, 3, 0], WATCHED) == set()


def test_garbage_is_ignored():
    assert peers_in_update("ping", WATCHED) == set()
    assert peers_in_update([], WATCHED) == set()
    assert peers_in_update([4, True, None, "42"], {1, 42}) == set()
