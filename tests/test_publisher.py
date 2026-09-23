import asyncio
import json
import shutil
import subprocess

from publisher import publish_app_identity


def _git(cwd, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _write_identity(cache, region: str, app_ver: str, app_hash: str) -> dict:
    data = {"appVersion": app_ver, "appHash": app_hash}
    (cache / f"{region}.json").write_text(json.dumps(data))
    return data


def _publish(cache, clone, remote) -> bool:
    return asyncio.run(publish_app_identity(str(cache), str(clone), str(remote), "data", "bot", "bot@example.com"))


def _setup(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    return remote, cache, tmp_path / "clone"


def test_publish_creates_branch_and_skips_unchanged_identity(tmp_path):
    remote, cache, clone = _setup(tmp_path)
    kr = _write_identity(cache, "KR", "6.4.0", "hash-1")

    assert _publish(cache, clone, remote) is True
    assert json.loads(_git(remote, "show", "data:KR.json")) == kr
    assert _publish(cache, clone, remote) is False
    assert _git(remote, "rev-list", "--count", "data") == "1"


def test_publish_commits_only_changed_regions(tmp_path):
    remote, cache, clone = _setup(tmp_path)
    _write_identity(cache, "KR", "6.4.0", "hash-1")
    _publish(cache, clone, remote)

    tw = _write_identity(cache, "TW", "6.4.0", "hash-1")

    assert _publish(cache, clone, remote) is True
    assert json.loads(_git(remote, "show", "data:TW.json")) == tw
    assert _git(remote, "log", "-1", "--format=%s", "data") == "chore: update app identity for TW"


def test_publish_resyncs_a_recreated_clone_with_the_remote_branch(tmp_path):
    remote, cache, clone = _setup(tmp_path)
    _write_identity(cache, "KR", "6.4.0", "hash-1")
    _publish(cache, clone, remote)
    shutil.rmtree(clone)

    assert _publish(cache, clone, remote) is False
    kr = _write_identity(cache, "KR", "6.4.1", "hash-2")
    assert _publish(cache, clone, remote) is True
    assert json.loads(_git(remote, "show", "data:KR.json")) == kr
    assert _git(remote, "rev-list", "--count", "data") == "2"


def test_publish_without_cached_identity_does_nothing(tmp_path):
    remote, cache, clone = _setup(tmp_path)

    assert _publish(cache, clone, remote) is False
    assert not clone.exists()
