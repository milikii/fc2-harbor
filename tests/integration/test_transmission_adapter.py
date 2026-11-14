"""Transmission adapter — spec 15 / 17.3.

Mocked RPC: session handshake, add, completion mapping and the same
``delete-local-data: false`` guarantee as qBittorrent.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from app.db.enums import DownloaderName
from app.downloaders.base import DownloaderAuthError, DownloaderRejectedError
from app.downloaders.transmission import TransmissionClient
from app.schemas.download import DownloadHandle, DownloadRequest, evaluate_completion

RPC = "https://tr.local:9091/transmission/rpc"
HASH = "0123456789abcdef0123456789abcdef01234567"

TORRENT = {
    "hashString": HASH,
    "name": "FC2-PPV-1234567",
    "status": 6,
    "percentDone": 1.0,
    "downloadDir": "/downloads/fc2/job-1",
    "totalSize": 1000,
    "leftUntilDone": 0,
}


def client() -> TransmissionClient:
    return TransmissionClient(base_url="https://tr.local:9091", username="u", password="p")


def request() -> DownloadRequest:
    return DownloadRequest(
        magnet=f"magnet:?xt=urn:btih:{HASH}",
        info_hash=HASH,
        save_path="/downloads/fc2/job-1",
        category="fc2-harbor",
        tag="fc2h-job-1",
        job_id="job-1",
    )


def rpc_response(arguments: dict) -> httpx.Response:
    return httpx.Response(200, json={"result": "success", "arguments": arguments})


@respx.mock(assert_all_called=False)
async def test_session_handshake_then_get(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(RPC).mock(
        side_effect=[
            httpx.Response(409, headers={"X-Transmission-Session-Id": "sess-1"}),
            rpc_response({"torrents": [TORRENT]}),
        ]
    )
    adapter = client()
    try:
        snapshot = await adapter.get(
            DownloadHandle(downloader=DownloaderName.TRANSMISSION, info_hash=HASH)
        )
    finally:
        await adapter.aclose()

    assert snapshot is not None
    assert route.calls[0].request.headers.get("X-Transmission-Session-Id") is None
    assert route.calls[1].request.headers["X-Transmission-Session-Id"] == "sess-1"


@respx.mock(assert_all_called=False)
async def test_add_sends_magnet_and_save_path(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(RPC).respond(
        200,
        json={
            "result": "success",
            "arguments": {"torrent-added": {"hashString": HASH, "name": "x"}},
        },
    )
    adapter = client()
    try:
        handle = await adapter.add(request())
    finally:
        await adapter.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["method"] == "torrent-add"
    assert body["arguments"]["filename"].startswith("magnet:?xt=urn:btih:")
    assert body["arguments"]["download-dir"] == "/downloads/fc2/job-1"
    assert body["arguments"]["labels"] == ["fc2-harbor"]
    assert handle.info_hash == HASH


@respx.mock(assert_all_called=False)
async def test_add_rejection_is_reported(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(RPC).respond(200, json={"result": "success", "arguments": {}})
    adapter = client()
    try:
        with pytest.raises(DownloaderRejectedError):
            await adapter.add(request())
    finally:
        await adapter.aclose()


@respx.mock(assert_all_called=False)
async def test_seeding_torrent_counts_as_complete(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(RPC).respond(
        200, json={"result": "success", "arguments": {"torrents": [TORRENT]}}
    )
    adapter = client()
    try:
        snapshot = await adapter.get(
            DownloadHandle(downloader=DownloaderName.TRANSMISSION, info_hash=HASH)
        )
    finally:
        await adapter.aclose()

    assert snapshot is not None
    assert snapshot.state == "uploading"
    assert snapshot.progress == 1.0
    assert snapshot.content_path == "/downloads/fc2/job-1/FC2-PPV-1234567"
    assert evaluate_completion(snapshot).complete is True


@respx.mock(assert_all_called=False)
async def test_downloading_torrent_is_not_complete(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(RPC).respond(
        200,
        json={
            "result": "success",
            "arguments": {"torrents": [{**TORRENT, "status": 4, "percentDone": 0.4}]},
        },
    )
    adapter = client()
    try:
        snapshot = await adapter.get(
            DownloadHandle(downloader=DownloaderName.TRANSMISSION, info_hash=HASH)
        )
    finally:
        await adapter.aclose()

    assert snapshot is not None
    assert evaluate_completion(snapshot).complete is False


@respx.mock(assert_all_called=False)
async def test_missing_torrent_returns_none(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(RPC).respond(200, json={"result": "success", "arguments": {"torrents": []}})
    adapter = client()
    try:
        assert (
            await adapter.get(
                DownloadHandle(downloader=DownloaderName.TRANSMISSION, info_hash=HASH)
            )
            is None
        )
    finally:
        await adapter.aclose()


@respx.mock(assert_all_called=False)
async def test_remove_never_deletes_data(respx_mock: respx.MockRouter) -> None:
    remove_route = respx_mock.post(RPC).mock(
        side_effect=[
            rpc_response({}),
            rpc_response({"torrents": []}),
        ]
    )
    adapter = client()
    try:
        await adapter.remove(DownloadHandle(downloader=DownloaderName.TRANSMISSION, info_hash=HASH))
    finally:
        await adapter.aclose()

    body = json.loads(remove_route.calls[0].request.content)
    assert body["method"] == "torrent-remove"
    assert body["arguments"]["delete-local-data"] is False


async def test_remove_refuses_to_delete_files() -> None:
    adapter = client()
    with pytest.raises(ValueError):
        await adapter.remove(
            DownloadHandle(downloader=DownloaderName.TRANSMISSION, info_hash=HASH),
            delete_files=True,
        )
    await adapter.aclose()


@respx.mock(assert_all_called=False)
async def test_bad_credentials_raise_auth_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(RPC).respond(401)
    adapter = client()
    try:
        with pytest.raises(DownloaderAuthError):
            await adapter.list_active()
    finally:
        await adapter.aclose()
