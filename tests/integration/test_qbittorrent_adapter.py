"""qBittorrent adapter — spec 14 / 22.1 (add, complete, remove, auth).

The adapter is exercised against a mocked HTTP transport; no real qBittorrent is
contacted. The assertions cover the two hard rules: an explicit save path with
AutoTMM disabled, and ``deleteFiles=false`` on every removal.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx
from app.db.enums import DownloaderName
from app.downloaders.base import (
    DownloaderAuthError,
    DownloaderProtocolError,
    DownloaderRejectedError,
    DownloaderUnavailableError,
)
from app.downloaders.qbittorrent import QBittorrentClient
from app.schemas.download import DownloadHandle, DownloadRequest, evaluate_completion

BASE = "http://qb.local:8080"
HASH = "0123456789abcdef0123456789abcdef01234567"

ENTRY = {
    "hash": HASH,
    "name": "FC2-PPV-1234567",
    "state": "uploading",
    "progress": 1.0,
    "content_path": "/downloads/fc2/job-1/FC2-PPV-1234567",
    "save_path": "/downloads/fc2/job-1",
    "total_size": 1000,
    "completed": 1000,
    "category": "fc2-harbor",
    "tags": "fc2h-job-1",
}


def make_client(**kwargs: object) -> QBittorrentClient:
    return QBittorrentClient(
        base_url=BASE,
        username="admin",
        password="secret",
        confirm_attempts=2,
        confirm_interval_seconds=0,
        **kwargs,  # type: ignore[arg-type]
    )


def make_request() -> DownloadRequest:
    return DownloadRequest(
        magnet=f"magnet:?xt=urn:btih:{HASH}&dn=FC2-PPV-1234567",
        info_hash=HASH,
        save_path="/downloads/fc2/job-1",
        category="fc2-harbor",
        tag="fc2h-job-1",
        job_id="job-1",
    )


@respx.mock(assert_all_called=False)
async def test_add_uses_explicit_savepath_and_disables_autotmm(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    add_route = respx_mock.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Ok.")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[ENTRY])

    client = make_client()
    handle = await client.add(make_request())

    assert handle.info_hash == HASH
    assert handle.task_id == HASH
    assert handle.downloader is DownloaderName.QBITTORRENT

    body = parse_qs(add_route.calls[0].request.content.decode())
    assert body["savepath"] == ["/downloads/fc2/job-1"]
    assert body["autoTMM"] == ["false"]
    assert body["category"] == ["fc2-harbor"]
    assert body["tags"] == ["fc2h-job-1"]
    assert body["urls"][0].startswith("magnet:?xt=urn:btih:")


@respx.mock(assert_all_called=False)
async def test_add_counts_existing_torrent_as_success(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    respx_mock.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Fails.")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[ENTRY])

    handle = await make_client().add(make_request())
    assert handle.info_hash == HASH


@respx.mock(assert_all_called=False)
async def test_add_with_empty_body_is_accepted(respx_mock: respx.MockRouter) -> None:
    """qBittorrent 5.x may answer torrents/add with an empty body."""

    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(204)
    respx_mock.post(f"{BASE}/api/v2/torrents/add").respond(200, text="")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[])

    handle = await make_client().add(make_request())

    assert handle.info_hash == HASH


@respx.mock(assert_all_called=False)
async def test_add_rejected_when_torrent_absent(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    respx_mock.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Fails.")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[])

    with pytest.raises(DownloaderRejectedError):
        await make_client().add(make_request())


@respx.mock(assert_all_called=False)
async def test_get_maps_snapshot_and_completion(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[ENTRY])

    snapshot = await make_client().get(
        DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash=HASH, task_id=HASH)
    )

    assert snapshot is not None
    assert snapshot.state == "uploading"
    assert snapshot.progress == 1.0
    assert snapshot.content_path == "/downloads/fc2/job-1/FC2-PPV-1234567"
    assert snapshot.total_bytes == 1000
    assert evaluate_completion(snapshot).complete is True


@respx.mock(assert_all_called=False)
async def test_get_returns_none_for_unknown_hash(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[])

    snapshot = await make_client().get(
        DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash=HASH)
    )
    assert snapshot is None


@respx.mock(assert_all_called=False)
async def test_remove_always_sends_delete_files_false(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    delete_route = respx_mock.post(f"{BASE}/api/v2/torrents/delete").respond(200)
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[])

    await make_client().remove(
        DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash=HASH)
    )

    body = parse_qs(delete_route.calls[0].request.content.decode())
    assert body["deleteFiles"] == ["false"]
    assert body["hashes"] == [HASH]


async def test_client_ignores_global_proxy_environment() -> None:
    """Spec 8.2: qBittorrent traffic must never follow HTTP_PROXY/ALL_PROXY."""

    client = make_client()
    try:
        assert client.client.trust_env is False
    finally:
        await client.close()


async def test_remove_refuses_to_delete_files() -> None:
    client = make_client()
    with pytest.raises(ValueError):
        await client.remove(
            DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash=HASH),
            delete_files=True,
        )
    await client.close()


@respx.mock(assert_all_called=False)
async def test_expired_session_is_reauthenticated_once(respx_mock: respx.MockRouter) -> None:
    login_route = respx_mock.post(f"{BASE}/api/v2/auth/login").mock(
        side_effect=[httpx.Response(200, text="Ok."), httpx.Response(200, text="Ok.")]
    )
    respx_mock.get(f"{BASE}/api/v2/torrents/info").mock(
        side_effect=[httpx.Response(403), httpx.Response(200, json=[ENTRY])]
    )

    snapshot = await make_client().get(
        DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash=HASH)
    )

    assert snapshot is not None
    assert login_route.call_count == 2


@respx.mock(assert_all_called=False)
async def test_login_204_is_success_and_the_sid_cookie_is_reused(
    respx_mock: respx.MockRouter,
) -> None:
    """qBittorrent 5.x answers 204 with an empty body plus a QBT_SID cookie."""

    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(
        204, headers={"Set-Cookie": "QBT_SID_8085=abc123; path=/"}
    )
    info = respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(200, json=[ENTRY])

    client = make_client()
    try:
        snapshot = await client.get(
            DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash=HASH)
        )
    finally:
        await client.close()

    assert snapshot is not None
    cookie = info.calls[0].request.headers.get("cookie", "")
    assert "QBT_SID_8085=abc123" in cookie


@respx.mock(assert_all_called=False)
async def test_login_200_ok_is_accepted_for_4x(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    client = make_client()
    try:
        await client.login()
        assert client._logged_in is True
    finally:
        await client.close()


@respx.mock(assert_all_called=False)
async def test_login_401_is_a_credential_error_not_a_ban(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(401, text="Unauthorized")

    client = make_client()
    try:
        with pytest.raises(DownloaderAuthError) as excinfo:
            await client.login()
    finally:
        await client.close()
    assert excinfo.value.code == "downloader_auth_failed"


@respx.mock(assert_all_called=False)
async def test_login_200_fails_is_bad_credentials(respx_mock: respx.MockRouter) -> None:
    """4.x reports a wrong password as 200 + "Fails."."""

    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Fails.")
    client = make_client()
    try:
        with pytest.raises(DownloaderAuthError) as excinfo:
            await client.login()
    finally:
        await client.close()
    assert excinfo.value.code == "downloader_auth_failed"


@respx.mock(assert_all_called=False)
async def test_unexpected_login_body_reports_status_and_size(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="weird")

    client = make_client()
    try:
        with pytest.raises(DownloaderProtocolError) as excinfo:
            await client.login()
    finally:
        await client.close()

    message = str(excinfo.value)
    assert "status=200" in message
    assert "body_bytes=5" in message
    assert excinfo.value.code == "downloader_login_unexpected"


@respx.mock(assert_all_called=False)
async def test_bad_credentials_raise_auth_error_without_leaking_secret(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Fails.")

    client = make_client()
    with pytest.raises(DownloaderAuthError) as excinfo:
        await client.login()
    assert "secret" not in str(excinfo.value)


@respx.mock(assert_all_called=False)
async def test_banned_ip_raises_auth_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(403)
    with pytest.raises(DownloaderAuthError) as excinfo:
        await make_client().login()
    assert excinfo.value.code == "downloader_banned"


@respx.mock(assert_all_called=False)
async def test_unreachable_downloader_raises_unavailable(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(DownloaderUnavailableError):
        await make_client().login()


@respx.mock(assert_all_called=False)
async def test_list_active_returns_all_tasks(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{BASE}/api/v2/auth/login").respond(200, text="Ok.")
    respx_mock.get(f"{BASE}/api/v2/torrents/info").respond(
        200, json=[ENTRY, {**ENTRY, "hash": "f" * 40}]
    )

    snapshots = await make_client().list_active()
    assert len(snapshots) == 2
