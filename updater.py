import asyncio
import logging
import re
import tempfile
import zipfile

import aiocron
import aiofiles
import aiohttp
import UnityPy
import UnityPy.enums
import UnityPy.enums.ClassIDType
from aiopath import AsyncPath
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm

from config import APPHASH_CACHE_FOLDER, APPVER_CACHE_FOLDER, DEFAULT_UNITY_VERSION
from constants import (
    APKPURE_URL_TEMPLATE,
    CN_APK_URL,
    PACKAGE_NAME_MAP,
    QOOAPP_APP_ID_MAP,
    QOOAPP_URL_TEMPLATE,
    TAPTAP_APP_ID_MAP,
    TAPTAP_CN_URL_TEMPLATE,
    USER_AGENT
)
from generated import UTTCGen_AsInstance
from generated.Sekai import AndroidPlayerSettingConfig
from helpers import enum_candidates, enum_package
from logger import setup_logging_queue

logger = logging.getLogger("apphash")


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
        ) as response:
            if response.status == 200:
                data = await response.text()
                match = re.search(r'"softwareVersion":"(\d+\.\d+\.\d+)"', data)
                app_ver = match.group(1)

                logger.info(
                    f"Fetched version {app_ver} for app id {app_id} from TapTap CN"
                )

                return app_ver
            else:
                raise Exception(
                    f"Failed to fetch version from TapTap CN: {response.status}"
                )


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
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.text()
                soup = BeautifulSoup(data, "html.parser")
                app_info_tree = soup.select("ul.app-info.android")[0]
                app_ver = app_info_tree.find_all(class_="row")[1].var.text

                logger.info(
                    f"Fetched version {app_ver} for app id {app_id} from QooApp"
                )

                return app_ver
            else:
                raise Exception(
                    f"Failed to fetch version from QooApp: {response.status}"
                )


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
        async with session.get(url) as response:
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

            print(f"APK downloaded to temporary file: {temp_file.name}")
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

    async with aiofiles.open(cache_file, "r") as f:
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


async def extract_app_hash(apk_path: str) -> str:
    """
    Extracts the app hash from the APK file. Thanks to sssekai project for the code.
    Args:
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

                app_version = "%s.%s.%s" % (
                    config.clientMajorVersion,
                    config.clientMinorVersion,
                    config.clientBuildVersion,
                )
                logger.info(f"App version: {app_version}")
                data_version = "%s.%s.%s" % (
                    config.clientDataMajorVersion,
                    config.clientDataMinorVersion,
                    config.clientDataBuildVersion,
                )
                logger.info(f"Data version: {data_version}")
                ab_version = "%s.%s.%s" % (
                    config.clientMajorVersion,
                    config.clientMinorVersion,
                    config.clientDataRevision,
                )
                logger.info(f"AB version: {ab_version}")

                app_hash = config.clientAppHash
                logger.info(f"App hash: {app_hash}")

                return app_hash


@aiocron.crontab("*/5 * * * *", start=False)
async def update_apphash():
    """
    Periodically updates the app hash by downloading the latest APK.
    """
    # Log the start time of the update
    logger.info("Starting app hash update...")

    # Check app available in qooapp
    for region, qooapp_id in QOOAPP_APP_ID_MAP.items():
        cached_app_ver = await get_cached_app_ver(region)
        latest_app_ver = await get_app_ver_from_qooapp(qooapp_id)

        if cached_app_ver != latest_app_ver:
            logger.info(f"New version available for {region}: {latest_app_ver}")
            apk_url = APKPURE_URL_TEMPLATE.format(packageName=PACKAGE_NAME_MAP[region])
            apk_path = await download_apk(apk_url)
            app_hash = await extract_app_hash(apk_path)

            await save_app_hash(region, app_hash)
            await save_app_ver(region, latest_app_ver)
            logger.info(
                f"App hash for {region} updated to {app_hash} for version {latest_app_ver}"
            )

            # Clean up the temporary APK file
            await AsyncPath(apk_path).unlink()
            logger.info(f"Temporary APK file {apk_path} deleted.")

    # Check app available in taptap (CN only)
    for region, taptap_id in TAPTAP_APP_ID_MAP.items():
        cached_app_ver = await get_cached_app_ver(region)
        latest_app_ver = await get_app_ver_from_taptap_cn(taptap_id)

        if cached_app_ver != latest_app_ver:
            logger.info(f"New version available for {region}: {latest_app_ver}")
            apk_url = CN_APK_URL
            apk_path = await download_apk(apk_url)
            app_hash = await extract_app_hash(apk_path)

            await save_app_hash(region, app_hash)
            await save_app_ver(region, latest_app_ver)
            logger.info(
                f"App hash for {region} updated to {app_hash} for version {latest_app_ver}"
            )

            # Clean up the temporary APK file
            await AsyncPath(apk_path).unlink()
            logger.info(f"Temporary APK file {apk_path} deleted.")


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

    # Start the cron job to update app hash every 5 minutes
    update_apphash.start()
    # Run the event loop
    # This is necessary to keep the script running and allow the cron job to execute
    asyncio.get_event_loop().run_forever()

    # asyncio.run(update_apphash())
