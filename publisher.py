"""Publish per-region app identity JSON to a Git branch.

Every cached ``{REGION}.json`` is mirrored into a dedicated clone checked out
on the data branch, then committed and pushed when anything changed. The clone
is reset to the remote branch before each sync, so this process is expected to
be the branch's only writer.
"""

import asyncio
import logging
import shutil

from aiopath import AsyncPath

logger = logging.getLogger("apphash")


class GitError(RuntimeError):
    pass


async def _git(repo_dir: str, *args: str, check: bool = True) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=repo_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    text = output.decode(errors="replace").strip()
    if check and process.returncode != 0:
        raise GitError(f"git {args[0]} failed with exit code {process.returncode}: {text}")
    return process.returncode, text


async def _prepare_clone(repo_dir: str, remote_url: str, branch: str) -> None:
    """Check the data branch out in ``repo_dir``, matching the remote exactly."""
    path = AsyncPath(repo_dir)
    if not await (path / ".git").exists():
        await path.mkdir(parents=True, exist_ok=True)
        await _git(repo_dir, "init", "--quiet")
        await _git(repo_dir, "remote", "add", "origin", remote_url)

    code, _ = await _git(repo_dir, "fetch", "--quiet", "origin", branch, check=False)
    if code == 0:
        await _git(repo_dir, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        await _git(repo_dir, "reset", "--quiet", "--hard", f"origin/{branch}")
    else:
        # The branch does not exist on the remote yet: start it without history
        # from the code branches.
        code, _ = await _git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if code != 0:
            await _git(repo_dir, "checkout", "--quiet", "--orphan", branch)
    await _git(repo_dir, "clean", "--quiet", "-fdx")


async def publish_app_identity(
    json_cache_folder: str,
    repo_dir: str,
    remote_url: str,
    branch: str,
    author_name: str,
    author_email: str,
) -> bool:
    """Mirror the cached identity files to the data branch; return True if pushed."""
    sources = sorted([p async for p in AsyncPath(json_cache_folder).glob("*.json")])
    if not sources:
        logger.info("No app identity files to publish yet.")
        return False

    await _prepare_clone(repo_dir, remote_url, branch)
    for source in sources:
        shutil.copyfile(source, AsyncPath(repo_dir) / source.name)

    await _git(repo_dir, "add", "--all")
    code, _ = await _git(repo_dir, "diff", "--cached", "--quiet", check=False)
    if code == 0:
        logger.info("Published app identity is already up to date.")
        return False

    _, changed = await _git(repo_dir, "diff", "--cached", "--name-only")
    regions = ", ".join(name.removesuffix(".json") for name in changed.splitlines())
    await _git(
        repo_dir,
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "--quiet",
        "-m",
        f"chore: update app identity for {regions}",
    )
    await _git(repo_dir, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    logger.info(f"Published app identity for {regions} to branch {branch}.")
    return True
