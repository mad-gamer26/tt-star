"""Locate or interactively install BearWare's TeamTalk 5 SDK.

The SDK is a separately licensed dependency and is never bundled with this
project. Download metadata is discovered from BearWare's official download
page so a new bot release is not required for every TeamTalk SDK update.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from html.parser import HTMLParser
import importlib
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sys
import tempfile
from types import ModuleType
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


DOWNLOAD_PAGE = "https://www.bearware.dk/?page_id=419"
LICENSE_PAGE = "https://bearware.dk/?page_id=316"
SDK_DIR_ENV = "TEAMTALK_SDK_DIR"
USER_AGENT = "tt-star SDK installer/1.0 (+https://github.com/)"
_DLL_HANDLES: list[object] = []


class TeamTalkSDKError(RuntimeError):
    """Raised when TeamTalk cannot be loaded or installed."""


@dataclass(frozen=True)
class SDKDownload:
    version: str
    platform_name: str
    architecture: str
    url: str


class _SDKLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href and re.search(r"/tt5sdk_v[^/]+\.7z(?:\?.*)?$", href, re.IGNORECASE):
            self.links.append(href)


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "tt-star"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tt-star"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "tt-star"


def _platform_selector() -> tuple[str, str, str]:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        if machine not in {"amd64", "x86_64"}:
            raise TeamTalkSDKError("BearWare currently publishes only an x64 Windows SDK.")
        return "windows", "x64", "_win64.7z"
    if sys.platform == "darwin":
        return "macOS", "universal", "_macos_universal.7z"
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "Linux", "x86_64", "_ubuntu22_x86_64.7z"
        if machine in {"aarch64", "arm64"}:
            return "Linux (Raspbian 12)", "arm64", "_raspbian_arm64.7z"
        raise TeamTalkSDKError(
            f"BearWare does not publish a TeamTalk SDK for Linux architecture {machine!r}."
        )
    raise TeamTalkSDKError(f"TeamTalk SDK automatic installation is unsupported on {sys.platform!r}.")


def discover_latest_download(timeout: float = 20.0) -> SDKDownload:
    """Return the current Standard SDK download advertised by BearWare."""
    platform_name, architecture, suffix = _platform_selector()
    request = Request(DOWNLOAD_PAGE, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise TeamTalkSDKError(f"Could not read BearWare's SDK download page: {exc}") from exc

    parser = _SDKLinkParser()
    parser.feed(html)
    candidates = [urljoin(DOWNLOAD_PAGE, link) for link in parser.links if link.lower().endswith(suffix)]
    if not candidates:
        raise TeamTalkSDKError(
            f"BearWare's download page did not contain a Standard SDK for {platform_name} {architecture}."
        )

    url = candidates[0]
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "bearware.dk",
        "www.bearware.dk",
    }:
        raise TeamTalkSDKError(f"Refusing an unexpected SDK download URL: {url}")
    filename = unquote(Path(parsed.path).name)
    match = re.search(r"tt5sdk_v(.+?)_(?:win64|macos_universal|ubuntu22_x86_64|raspbian_arm64)\.7z$", filename)
    if not match:
        raise TeamTalkSDKError(f"Could not determine the SDK version from {filename!r}.")
    return SDKDownload(match.group(1), platform_name, architecture, url)


def _download(download: SDKDownload, destination: Path, timeout: float = 60.0) -> None:
    request = Request(download.url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0"))
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if total:
                    print(f"\rDownloading TeamTalk SDK: {received * 100 // total:3d}%", end="", flush=True)
        if total:
            print()
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise TeamTalkSDKError(f"Could not download the TeamTalk SDK: {exc}") from exc


def _safe_archive_names(names: list[str]) -> None:
    for name in names:
        normalized = name.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise TeamTalkSDKError(f"The SDK archive contains an unsafe path: {name!r}")


def _extract_archive(archive: Path, destination: Path) -> None:
    try:
        import py7zr
    except ImportError as exc:
        raise TeamTalkSDKError(
            "Installing the .7z SDK archive requires py7zr. Run: python -m pip install py7zr"
        ) from exc
    try:
        with py7zr.SevenZipFile(archive, mode="r") as seven_zip:
            _safe_archive_names(seven_zip.getnames())
            seven_zip.extractall(path=destination)
    except TeamTalkSDKError:
        raise
    except Exception as exc:
        raise TeamTalkSDKError(f"Could not extract the TeamTalk SDK: {exc}") from exc


def _find_wrapper(root: Path) -> Path | None:
    matches = list(root.rglob("TeamTalk5.py")) if root.exists() else []
    preferred = [path for path in matches if path.parent.name == "TeamTalkPy"]
    return (preferred or matches or [None])[0]


def install_sdk(download: SDKDownload | None = None, destination: Path | None = None) -> Path:
    """Download and install an SDK after the caller has obtained consent."""
    download = download or discover_latest_download()
    base = destination or (_default_data_dir() / "teamtalk-sdk")
    version_dir = base / download.version
    existing = _find_wrapper(version_dir)
    if existing:
        return existing

    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tt-star-sdk-") as temp_name:
        temp = Path(temp_name)
        archive = temp / "teamtalk-sdk.7z"
        extracted = temp / "extracted"
        extracted.mkdir()
        print(f"Downloading TeamTalk SDK {download.version} from BearWare...")
        _download(download, archive)
        _extract_archive(archive, extracted)
        wrapper = _find_wrapper(extracted)
        if not wrapper:
            raise TeamTalkSDKError("The downloaded SDK did not contain TeamTalk5.py.")
        sdk_root = wrapper.parent.parent.parent
        staging = base / f".{download.version}.installing"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(sdk_root, staging)
        if version_dir.exists():
            shutil.rmtree(version_dir)
        staging.replace(version_dir)

    installed = _find_wrapper(version_dir)
    if not installed:
        raise TeamTalkSDKError("The SDK was extracted, but its Python wrapper could not be found.")
    print(f"Installed TeamTalk SDK {download.version} in {version_dir}")
    return installed


def _native_library_for(wrapper: Path) -> Path:
    sdk_root = wrapper.parent.parent.parent
    names = {
        "win32": "TeamTalk5.dll",
        "darwin": "libTeamTalk5.dylib",
    }
    library_name = names.get(sys.platform, "libTeamTalk5.so")
    matches = list(sdk_root.rglob(library_name))
    if not matches:
        raise TeamTalkSDKError(f"The SDK at {sdk_root} does not contain {library_name}.")
    preferred = [path for path in matches if path.parent.name == "TeamTalk_DLL"]
    return (preferred or matches)[0]


def _import_from_wrapper(wrapper: Path) -> ModuleType:
    native_library = _native_library_for(wrapper)
    native_dir = native_library.parent
    if sys.platform == "win32":
        _DLL_HANDLES.append(os.add_dll_directory(str(native_dir)))
    else:
        mode = getattr(ctypes, "RTLD_GLOBAL", 0)
        _DLL_HANDLES.append(ctypes.CDLL(str(native_library), mode=mode))
    wrapper_dir = str(wrapper.parent)
    if wrapper_dir not in sys.path:
        sys.path.insert(0, wrapper_dir)
    sys.modules.pop("TeamTalk5", None)
    return importlib.import_module("TeamTalk5")


def _configured_wrappers() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get(SDK_DIR_ENV)
    if configured:
        roots.append(Path(configured).expanduser())
    roots.append(_default_data_dir() / "teamtalk-sdk")
    wrappers: list[Path] = []
    for root in roots:
        wrapper = _find_wrapper(root)
        if wrapper:
            wrappers.append(wrapper)
    return wrappers


def _try_existing_sdk() -> tuple[ModuleType | None, Exception | None]:
    last_error: Exception | None = None
    try:
        return importlib.import_module("TeamTalk5"), None
    except (ImportError, OSError) as exc:
        last_error = exc
        sys.modules.pop("TeamTalk5", None)
    for wrapper in _configured_wrappers():
        try:
            return _import_from_wrapper(wrapper), None
        except (ImportError, OSError, TeamTalkSDKError) as exc:
            last_error = exc
            sys.modules.pop("TeamTalk5", None)
    return None, last_error


def _confirm_download(download: SDKDownload, previous_error: Exception | None) -> bool:
    print("The TeamTalk 5 SDK was not found or could not be loaded.", file=sys.stderr)
    if previous_error:
        print(f"Load error: {previous_error}", file=sys.stderr)
    print(
        f"\nBearWare currently offers TeamTalk SDK {download.version} for "
        f"{download.platform_name} {download.architecture}.\n"
        "The download is a 30-day trial. BearWare requires an SDK license for use in an "
        "end-user application, and each developer must comply with BearWare's terms.\n"
        f"License information: {LICENSE_PAGE}\n"
        f"Download source: {download.url}\n"
    )
    try:
        answer = input("Download and install this SDK for your user account? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def load_teamtalk_sdk() -> ModuleType:
    """Load TeamTalk5, offering an official SDK download when it is absent."""
    module, error = _try_existing_sdk()
    if module:
        return module
    download = discover_latest_download()
    if not _confirm_download(download, error):
        raise TeamTalkSDKError(
            f"TeamTalk SDK installation declined. Install it manually from {DOWNLOAD_PAGE} "
            f"or set {SDK_DIR_ENV} to an existing SDK directory."
        )
    wrapper = install_sdk(download)
    try:
        return _import_from_wrapper(wrapper)
    except (ImportError, OSError) as exc:
        raise TeamTalkSDKError(f"The TeamTalk SDK was installed but could not be loaded: {exc}") from exc
