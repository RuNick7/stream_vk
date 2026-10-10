"""VKClient: токен, повторы, скачивание вложений."""

from __future__ import annotations

import pytest

from vkstream.vk.client import VKAuthError, VKClient, VKError

from .conftest import FakeVK


@pytest.fixture
async def vk(cfg):
    client = VKClient(cfg)
    yield client
    await client.close()


async def test_token_is_fetched_once_and_reused(vk: VKClient, fake_vk: FakeVK):
    assert (await vk.call("users.get"))[0]["first_name"] == "Иван"
    await vk.call("users.get")
    assert fake_vk.logins == 1 and vk.user_id == 1


async def test_rejected_token_is_refreshed_transparently(vk: VKClient, fake_vk: FakeVK):
    await vk.call("users.get")
    fake_vk.expire_token()
    assert await vk.call("users.get")
    assert fake_vk.logins == 2


async def test_dead_session_raises_auth_error(vk: VKClient, fake_vk: FakeVK):
    fake_vk.session_ok = False
    with pytest.raises(VKAuthError, match="need login"):
        await vk.call("users.get")


async def test_token_rejected_again_after_refresh_does_not_loop(vk: VKClient, fake_vk: FakeVK):
    fake_vk.reject_tokens = True
    with pytest.raises(VKAuthError):
        await vk.call("users.get")
    assert fake_vk.logins == 2 and fake_vk.calls == ["users.get", "users.get"]


async def test_api_errors_carry_code_and_method(vk: VKClient, fake_vk: FakeVK):
    with pytest.raises(VKError) as info:
        await vk.call("messages.noSuchMethod")
    assert info.value.code == 3 and "messages.noSuchMethod" in str(info.value)
    assert not isinstance(info.value, VKAuthError)


async def test_lists_and_bools_are_encoded_for_vk(vk: VKClient, fake_vk: FakeVK):
    fake_vk.history[5] = []
    fake_vk.history[6] = []
    resp = await vk.call("messages.getConversationsById", peer_ids=[5, 6], extended=True, skipped=None)
    assert [c["peer"]["id"] for c in resp["items"]] == [5, 6]


async def test_download_sends_session_only_to_vk_hosts(vk: VKClient, fake_vk: FakeVK):
    await vk.call("users.get")  # сессия подхватывается с диска при первом обращении к API
    got = await vk.download(f"{fake_vk.base}/redirect", max_bytes=1000)
    assert got is not None and got.data == b"x" * 8 and got.content_type == "image/jpeg"
    # первый запрос — на «домен ВК» с cookie, после редиректа на чужой хост — без
    assert fake_vk.file_cookies == ["remixsid=good", ""]


async def test_download_refuses_oversized_and_missing_files(vk: VKClient, fake_vk: FakeVK):
    assert await vk.download(f"{fake_vk.base}/file/big.bin?size=5000", max_bytes=1000) is None
    assert await vk.download(f"{fake_vk.base}/nope", max_bytes=1000) is None
    assert await vk.download("http://127.0.0.1:1/unreachable", max_bytes=1000) is None
