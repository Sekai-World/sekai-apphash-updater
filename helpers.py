from io import BytesIO
import zipfile


def enum_candidates(zip_file, filter):
    """
    Enumerate files in a zip file that match a given filter.
    Args:
        zip_file (zipfile.ZipFile): The zip file to enumerate.
        filter (callable): A function that takes a filename and returns True if it matches the filter.
    Returns:
        tuple: A tuple containing the filename, file object, and zip file object.
    """
    return ((f, zip_file.open(f), zip_file) for f in zip_file.filelist if filter(f.filename))


def enum_package(zip_file):
    """
    Enumerate APK files in a zip file.
    Args:
        zip_file (zipfile.ZipFile): The zip file to enumerate.
    Yields:
        zipfile.ZipFile: The APK file object.
    """
    yield zip_file
    for f in zip_file.filelist:
        if f.filename.lower().endswith(".apk"):
            yield zipfile.ZipFile(zip_file.open(f))
