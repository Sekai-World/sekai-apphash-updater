import asyncio
from datetime import UTC, datetime
import html
import json
import logging
import re
import tempfile
from urllib.parse import unquote
import zipfile

import aiocron
import aiofiles
import aiohttp
from aiopath import AsyncPath
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm
import UnityPy
import UnityPy.enums
import UnityPy.enums.ClassIDType

import config
from config import (
    APPHASH_CACHE_FOLDER,
    APPVER_CACHE_FOLDER,
    APPVER_JSON_CACHE_FOLDER,
    DEBUG,
    DEFAULT_UNITY_VERSION,
    PROXY,
)
from constants import (
    APKCOMBO_URL_TEMPLATE,
    APKPURE_URL_TEMPLATE,
    CN_APK_URL,
    PACKAGE_NAME_MAP,
    QOOAPP_APP_ID_MAP,
    QOOAPP_URL_TEMPLATE,
    TAPTAP_APP_ID_MAP,
    TAPTAP_CN_URL_TEMPLATE,
    USER_AGENT,
)
from generated import UTTCGen_AsInstance
from generated.Sekai import AndroidPlayerSettingConfig
from helpers import compare_version, enum_candidates, enum_package
from logger import setup_logging_queue
from publisher import publish_app_identity

logger = logging.getLogger("apphash")

# Publishing settings are optional so existing config.py files keep working.
PUBLISH_ENABLED = getattr(config, "PUBLISH_ENABLED", False)
PUBLISH_REPO_DIR = getattr(config, "PUBLISH_REPO_DIR", "cache/data-branch")
PUBLISH_REMOTE_URL = getattr(config, "PUBLISH_REMOTE_URL", "https://github.com/Sekai-World/sekai-apphash-updater.git")
PUBLISH_BRANCH = getattr(config, "PUBLISH_BRANCH", "data")
PUBLISH_AUTHOR_NAME = getattr(config, "PUBLISH_AUTHOR_NAME", "sekai-apphash-updater")
PUBLISH_AUTHOR_EMAIL = getattr(config, "PUBLISH_AUTHOR_EMAIL", "sekai-apphash-updater@users.noreply.github.com")

_update_lock = asyncio.Lock()


async def get_app_ver_from_taptap_cn(app_id: str) -> str:
    """
    Fetches the latest version of the app from TapTap CN.
    Args:
        app_id (str): The app ID for TapTap CN.
    Returns:
        str: The latest version of the app.
    """
    url = TAPTAP_CN_URL_TEMPLATE.format(app_id=app_id)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
            },
            proxy=PROXY,
        ) as response:
            if response.status == 200:
                data = await response.text()
                match = re.search(r'"softwareVersion":"(\d+\.\d+\.\d+)"', data)
                app_ver = match.group(1)

                logger.info(f"Fetched version {app_ver} for app id {app_id} from TapTap CN")

                return app_ver
            else:
                raise Exception(f"Failed to fetch version from TapTap CN: {response.status}")


async def get_app_ver_from_qooapp(app_id: str) -> str:
    """
    Fetches the latest version of the app from QooApp.
    Args:
        app_id (str): The app ID for QooApp.
    Returns:
        str: The latest version of the app.
    """
    url = QOOAPP_URL_TEMPLATE.format(app_id=app_id)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, proxy=PROXY) as response:
            if response.status == 200:
                data = await response.text()
                soup = BeautifulSoup(data, "html.parser")
                app_info_tree = soup.select("ul.app-info.android")[0]
                app_ver = app_info_tree.find_all(class_="row")[1].var.text

                logger.info(f"Fetched version {app_ver} for app id {app_id} from QooApp")

                return app_ver
            else:
                raise Exception(f"Failed to fetch version from QooApp: {response.status}")


def parse_apkcombo_xapk_url(page: str, app_ver: str) -> str | None:
    """
    Finds the XAPK download URL for a version on an APKCombo download page.
    Args:
        page (str): The HTML of the download page.
        app_ver (str): The version that must be offered.
    Returns:
        str: The presigned XAPK URL, or None when that version is not listed.
    """
    # Each variant links to /r2?u=<URL-encoded presigned URL>, labelled like
    # "Project Sekai KR 6.4.0 (22011) XAPK 520 MB ...".
    for match in re.finditer(r'<a href="/r2\?u=([^"]+)" class="variant"[^>]*>(.*?)</a>', page, re.S):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(2)))
        if f" {app_ver} " in f" {label} " and "XAPK" in label:
            return unquote(html.unescape(match.group(1)))
    return None


async def resolve_apkcombo_url(package_name: str, app_ver: str) -> str:
    """
    Resolves the XAPK download URL for a version listed on APKCombo.
    Args:
        package_name (str): The Android package name.
        app_ver (str): The version that must be offered.
    Returns:
        str: The presigned XAPK URL behind the matching download link.
    """
    url = APKCOMBO_URL_TEMPLATE.format(packageName=package_name)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, proxy=PROXY) as response:
            response.raise_for_status()
            page = await response.text()

    xapk_url = parse_apkcombo_xapk_url(page, app_ver)
    if xapk_url is None:
        raise Exception(f"APKCombo does not offer the {app_ver} XAPK for {package_name} yet")
    return xapk_url


async def download_apk(url: str) -> str:
    """
    Downloads an APK file from the given URL and saves it to a temporary file.
    Args:
        url (str): The URL of the APK file to download.
    Returns:
        str: The path to the temporary file where the APK is saved.
    """
    # Use aiohttp to download the APK file asynchronously
    # and tqdm to show a progress bar.
    async with aiohttp.ClientSession() as session:
        async with session.get(url, proxy=PROXY) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("content-length", 0))
            block_size = 1024  # 1 Kibibyte

            # Use delete=False to keep the file after closing
            # and suffix=".apk" to ensure the file has the correct extension.
            with (
                tempfile.NamedTemporaryFile(delete=False, suffix=".apk") as temp_file,
                tqdm(
                    desc="Downloading APK",
                    total=total_size,
                    unit="iB",
                    unit_scale=True,
                    unit_divisor=1024,
                ) as bar,
            ):
                async for data in response.content.iter_chunked(block_size):
                    temp_file.write(data)
                    bar.update(len(data))

            logger.info(f"APK downloaded to temporary file: {temp_file.name}")
            return temp_file.name


async def get_cached_app_ver(region: str) -> str | None:
    """
    Retrieves the cached app version for the specified region.
    Args:
        region (str): The region for which to retrieve the cached app version.
    Returns:
        str: The cached app version.
    """
    cache_file = AsyncPath(APPVER_CACHE_FOLDER) / f"{region}.txt"

    if not await cache_file.exists():
        logger.warning(f"Cache file for {region} does not exist. Returning None.")
        return None

    async with aiofiles.open(cache_file) as f:
        cached_app_ver = await f.read()
        logger.info(f"Cached app version for {region}: {cached_app_ver}")
        return cached_app_ver.strip()


async def save_app_ver(region: str, app_ver: str):
    """
    Saves the app version to the cache for the specified region.
    Args:
        region (str): The region for which to save the app version.
        app_ver (str): The app version to save.
    """
    cache_file = AsyncPath(APPVER_CACHE_FOLDER) / f"{region}.txt"

    if not await cache_file.parent.exists():
        logger.warning(f"Cache folder for {region} does not exist. Creating it.")
        await cache_file.parent.mkdir(parents=True, exist_ok=True)

    async with aiofiles.open(cache_file, "w") as f:
        await f.write(app_ver)
        logger.info(f"Saved app version {app_ver} for {region} to cache.")


async def save_app_hash(region: str, app_hash: str):
    """
    Saves the app hash to the cache for the specified region.
    Args:
        region (str): The region for which to save the app hash.
        app_hash (str): The app hash to save.
    """
    cache_file = AsyncPath(APPHASH_CACHE_FOLDER) / f"{region}.txt"

    if not await cache_file.parent.exists():
        logger.warning(f"Cache folder for {region} does not exist. Creating it.")
        await cache_file.parent.mkdir(parents=True, exist_ok=True)

    async with aiofiles.open(cache_file, "w") as f:
        await f.write(app_hash)
        logger.info(f"Saved app hash {app_hash} for {region} to cache.")


async def save_app_json(region: str, app_ver: str, app_hash: str):
    """
    Saves the app version and app hash json to the cache for the specified region.
    Args:
        region (str): The region for which to save the app version.
        app_ver (str): The app version to save.
        app_hash (str): The app hash to save.
    """
    data = {
        "appVersion": app_ver,
        "appHash": app_hash,
        "updatedAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }

    cache_file = AsyncPath(APPVER_JSON_CACHE_FOLDER) / f"{region}.json"

    if not await cache_file.parent.exists():
        logger.warning(f"Cache folder for {region} does not exist. Creating it.")
        await cache_file.parent.mkdir(parents=True, exist_ok=True)

    async with aiofiles.open(cache_file, "w") as f:
        await f.write(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info(f"Saved app hash {app_hash} for {region} to cache.")


async def extract_app_hash(apk_path: str, expected_app_ver: str) -> str | None:
    """
    Extracts the app hash from the APK file. Thanks to sssekai project for the code.
    Args:
        expected_app_ver: expected app version.
        apk_path (str): The path to the APK file.
    Returns:
        str: The app hash.
    """
    env = UnityPy.Environment()
    with zipfile.ZipFile(apk_path, "r") as zip_ref:
        candidates = [
            candidate
            for package in enum_package(zip_ref)
            for candidate in enum_candidates(
                package,
                lambda fn: fn.split("/")[-1]
                in {
                    "6350e2ec327334c8a9b7f494f344a761",  # PJSK Android
                    "c726e51b6fe37463685916a1687158dd",  # PJSK iOS
                    "data.unity3d",  # TW,KR,CN (ByteDance)
                },
            )
        ]
        for candidate, stream, _ in candidates:
            env.load_file(stream)

    for reader in env.objects:
        if reader.type == UnityPy.enums.ClassIDType.MonoBehaviour:
            pname = reader.peek_name()
            if pname == "production_android":
                clazz = AndroidPlayerSettingConfig
                config = UTTCGen_AsInstance(clazz, reader)

                app_version = f"{config.clientMajorVersion}.{config.clientMinorVersion}.{config.clientBuildVersion}"
                logger.info(f"App version: {app_version}")
                data_version = (
                    f"{config.clientDataMajorVersion}.{config.clientDataMinorVersion}.{config.clientDataBuildVersion}"
                )
                assert compare_version(app_version, expected_app_ver), (
                    f"App version mismatch: {app_version} != {expected_app_ver}"
                )
                logger.info(f"Data version: {data_version}")
                ab_version = f"{config.clientMajorVersion}.{config.clientMinorVersion}.{config.clientDataRevision}"
                logger.info(f"AB version: {ab_version}")

                app_hash = config.clientAppHash
                logger.info(f"App hash: {app_hash}")

                return app_hash


async def download_and_extract_app_hash(apk_url: str, app_ver: str) -> str | None:
    """
    Downloads an APK and extracts its app hash, deleting the download afterwards.
    Args:
        apk_url (str): The URL of the APK to download.
        app_ver (str): The expected app version.
    Returns:
        str: The app hash.
    """
    apk_path = await download_apk(apk_url)
    try:
        return await extract_app_hash(apk_path, app_ver)
    finally:
        # Clean up the temporary APK file
        await AsyncPath(apk_path).unlink()
        logger.info(f"Temporary APK file {apk_path} deleted.")


async def qooapp_region_sources(region: str, app_ver: str) -> list[tuple[str, str]]:
    """
    Lists the APK sources to try for a QooApp region, APKCombo first.
    APKPure stays as the fallback because it does not always carry the latest version promptly.
    Args:
        region (str): The region to download.
        app_ver (str): The version that must be downloaded.
    Returns:
        list: (source name, URL) pairs in the order to try.
    """
    package_name = PACKAGE_NAME_MAP[region]
    sources = []
    try:
        sources.append(("APKCombo", await resolve_apkcombo_url(package_name, app_ver)))
    except Exception as error:
        logger.warning(f"APKCombo source unavailable for {region}: {error}")
    sources.append(("APKPure", APKPURE_URL_TEMPLATE.format(packageName=package_name)))
    return sources


async def update_region(region: str, latest_app_ver: str, sources) -> None:
    """
    Caches the app hash of a region's new version, trying each APK source in order.
    Args:
        region (str): The region to update.
        latest_app_ver (str): The latest app version reported by the store.
        sources: An async callable returning (source name, URL) pairs to try in order.
    """
    cached_app_ver = await get_cached_app_ver(region)
    if cached_app_ver == latest_app_ver:
        return

    logger.info(f"New version available for {region}: {latest_app_ver}")
    for source_name, apk_url in await sources(region, latest_app_ver):
        try:
            app_hash = await download_and_extract_app_hash(apk_url, latest_app_ver)
        except Exception:
            logger.exception(f"Extracting the {region} app hash from {source_name} failed.")
            continue
        if not app_hash:
            logger.error(f"Failed to extract app hash for {region} from the {source_name} APK.")
            continue

        await save_app_hash(region, app_hash)
        await save_app_ver(region, latest_app_ver)
        await save_app_json(region, latest_app_ver, app_hash)
        logger.info(f"App hash for {region} updated to {app_hash} for version {latest_app_ver} from {source_name}")
        return

    raise RuntimeError(f"No APK source produced the {region} {latest_app_ver} app hash")


async def cn_sources(region: str, app_ver: str) -> list[tuple[str, str]]:
    return [("CN mirror", CN_APK_URL)]


@aiocron.crontab("*/5 * * * *", start=False)
async def update_apphash():
    """
    Periodically updates the app hash by downloading the latest APK.
    """
    if _update_lock.locked():
        logger.info("Previous app hash update is still running; skipping this run.")
        return

    async with _update_lock:
        # Log the start time of the update
        logger.info("Starting app hash update...")

        # Check app available in qooapp
        for region, qooapp_id in QOOAPP_APP_ID_MAP.items():
            try:
                latest_app_ver = await get_app_ver_from_qooapp(qooapp_id)
                await update_region(region, latest_app_ver, qooapp_region_sources)
            except Exception:
                logger.exception(f"App hash update failed for {region}.")

        # Check app available in taptap (CN only)
        for region, taptap_id in TAPTAP_APP_ID_MAP.items():
            try:
                latest_app_ver = await get_app_ver_from_taptap_cn(taptap_id)
                await update_region(region, latest_app_ver, cn_sources)
            except Exception:
                logger.exception(f"App hash update failed for {region}.")

        if PUBLISH_ENABLED:
            try:
                await publish_app_identity(
                    APPVER_JSON_CACHE_FOLDER,
                    PUBLISH_REPO_DIR,
                    PUBLISH_REMOTE_URL,
                    PUBLISH_BRANCH,
                    PUBLISH_AUTHOR_NAME,
                    PUBLISH_AUTHOR_EMAIL,
                )
            except Exception:
                logger.exception("Publishing app identity failed.")


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    setup_logging_queue()

    # Set up UnityPy
    UnityPy.config.FALLBACK_VERSION_WARNED = True
    UnityPy.config.FALLBACK_UNITY_VERSION = DEFAULT_UNITY_VERSION
    if not DEBUG:
        loop = asyncio.get_event_loop()
        # Run once right away instead of waiting for the first cron tick
        loop.run_until_complete(update_apphash.func())
        # Start the cron job to update app hash every 5 minutes
        update_apphash.start()
        # Run the event loop
        # This is necessary to keep the script running and allow the cron job to execute
        loop.run_forever()
    else:
        asyncio.run(update_apphash.func())
