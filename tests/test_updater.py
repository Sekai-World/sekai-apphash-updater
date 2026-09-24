import asyncio
from urllib.parse import quote

import pytest

import updater

PRESIGNED = "https://apks.example.r2.cloudflarestorage.com/com.pjsekai.kr/6.4.0/22011.apks?X-Amz-Expires=14400&a=1"


def _variant(url: str, label: str) -> str:
    return f'<a href="/r2?u={quote(url, safe="")}" class="variant" rel="nofollow"><span>{label}</span></a>'


def test_parse_apkcombo_xapk_url_picks_the_expected_version():
    page = _variant("https://old", "Project Sekai KR 6.3.2 (21001) XAPK 500 MB") + _variant(
        PRESIGNED, "Project Sekai KR 6.4.0 (22011) XAPK 520 MB Android 7.0+"
    )

    assert updater.parse_apkcombo_xapk_url(page, "6.4.0") == PRESIGNED


def test_parse_apkcombo_xapk_url_rejects_a_lagging_mirror():
    page = _variant(PRESIGNED, "Project Sekai KR 6.4.0 (22011) XAPK 520 MB")

    assert updater.parse_apkcombo_xapk_url(page, "6.4.1") is None
    assert updater.parse_apkcombo_xapk_url(page, "6.4") is None


@pytest.fixture
def region_state(monkeypatch):
    state = {"cached": None, "saved": [], "tried": []}

    async def cached(region):
        return state["cached"]

    async def save_json(region, app_ver, app_hash):
        state["saved"].append((region, app_ver, app_hash))

    async def noop(*args):
        return None

    monkeypatch.setattr(updater, "get_cached_app_ver", cached)
    monkeypatch.setattr(updater, "save_app_json", save_json)
    monkeypatch.setattr(updater, "save_app_hash", noop)
    monkeypatch.setattr(updater, "save_app_ver", noop)
    return state


def _sources(*pairs):
    async def sources(region, app_ver):
        return list(pairs)

    return sources


def test_update_region_falls_back_to_the_next_source(monkeypatch, region_state):
    async def download(url, app_ver):
        region_state["tried"].append(url)
        if url == "combo":
            raise AssertionError("App version mismatch")
        return "hash-1"

    monkeypatch.setattr(updater, "download_and_extract_app_hash", download)

    asyncio.run(updater.update_region("KR", "6.4.0", _sources(("APKCombo", "combo"), ("APKPure", "pure"))))

    assert region_state["tried"] == ["combo", "pure"]
    assert region_state["saved"] == [("KR", "6.4.0", "hash-1")]


def test_update_region_skips_a_cached_version(monkeypatch, region_state):
    region_state["cached"] = "6.4.0"

    async def download(url, app_ver):
        raise AssertionError("must not download")

    monkeypatch.setattr(updater, "download_and_extract_app_hash", download)

    asyncio.run(updater.update_region("KR", "6.4.0", _sources(("APKCombo", "combo"))))

    assert region_state["saved"] == []


def test_update_region_raises_when_no_source_yields_a_hash(monkeypatch, region_state):
    async def download(url, app_ver):
        return None

    monkeypatch.setattr(updater, "download_and_extract_app_hash", download)

    with pytest.raises(RuntimeError, match="No APK source"):
        asyncio.run(updater.update_region("KR", "6.4.0", _sources(("APKCombo", "combo"), ("APKPure", "pure"))))

    assert region_state["saved"] == []
