"""Locate or interactively install BearWare's TeamTalk 5 SDK.

The SDK is a separately licensed dependency and is never bundled with this
project. Downloads are discovered from BearWare's SDK file listing
(https://www.bearware.dk/teamtalksdk/), with the download page as a fallback, so a
new bot release is not required for every TeamTalk SDK update. BearWare publishes
no SDK files on GitHub and has no download API, so the listing is the source of truth.

Linux x86-64 builds are named after the Ubuntu release they were built on
(tt5sdk_v5.23a_ubuntu26_x86_64.7z). A build made on a newer Ubuntu needs a newer
glibc and cannot load on an older system, so the newest build that this system can
run is chosen, and every download is test-loaded before it is installed.
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
import subprocess
import sys
import tempfile
from types import ModuleType
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


DOWNLOAD_PAGE = "https://www.bearware.dk/?page_id=419"
SDK_INDEX = "https://www.bearware.dk/teamtalksdk/"
ALLOWED_HOSTS = {"bearware.dk", "www.bearware.dk"}
# glibc shipped by each Ubuntu release that BearWare builds Linux x86-64 SDKs on.
UBUNTU_GLIBC = {18: (2, 27), 20: (2, 31), 22: (2, 35), 24: (2, 39), 26: (2, 43)}
MAX_VERSIONS_CHECKED = 4
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


class _HrefParser(HTMLParser):
    """Collects every link on a page (BearWare's file listings are plain HTML)."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


class _SDKLinkParser(_HrefParser):
    """Collects links to Standard SDK archives (not the Pro SDK)."""

    @property
    def links(self) -> list[str]:
        return [h for h in self.hrefs if re.search(r"(?:^|/)tt5sdk_v[^/]+\.7z(?:\?.*)?$", h, re.IGNORECASE)]


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "tt-star"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tt-star"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "tt-star"


def _platform_selector() -> tuple[str, str, str]:
    """(platform name, architecture, regular expression for the archive name's ending)."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        if machine not in {"amd64", "x86_64"}:
            raise TeamTalkSDKError("BearWare currently publishes only an x64 Windows SDK.")
        return "windows", "x64", r"_win64\.7z"
    if sys.platform == "darwin":
        return "macOS", "universal", r"_macos_universal\.7z"
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "Linux", "x86_64", r"_ubuntu(?P<ubuntu>\d+)_x86_64\.7z"
        if machine in {"aarch64", "arm64"}:
            return "Linux (Raspbian 12)", "arm64", r"_raspbian_arm64\.7z"
        raise TeamTalkSDKError(
            f"BearWare does not publish a TeamTalk SDK for Linux architecture {machine!r}."
        )
    raise TeamTalkSDKError(f"TeamTalk SDK automatic installation is unsupported on {sys.platform!r}.")


def _host_glibc() -> tuple[int, int] | None:
    """This system's glibc version, or None if it is not a glibc system."""
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, ValueError, OSError):
        return None
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _ubuntu_build_runs_here(ubuntu: int, host: tuple[int, int] | None = None) -> bool:
    """Whether a build made on Ubuntu `ubuntu` (22, 24, 26...) can load on this system."""
    host = host if host is not None else _host_glibc()
    if host is None:
        return True  # unknown C library: the test load after download decides
    # Releases not in the table: Ubuntu's glibc has gone up by about 2 minor versions a year.
    needed = UBUNTU_GLIBC.get(ubuntu, (2, 35 + 2 * (ubuntu - 22)))
    return host >= needed


def _version_key(version: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"(\d+)\.(\d+)([a-z]*)", version, re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2)), match.group(3).lower()) if match else (0, 0, "")


def _fetch_links(url: str, timeout: float) -> list[str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise TeamTalkSDKError(f"Could not read {url}: {exc}") from exc
    parser = _HrefParser()
    parser.feed(text)
    return [urljoin(url, href) for href in parser.hrefs]


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise TeamTalkSDKError(f"Refusing an unexpected SDK download URL: {url}")


def _pick(urls: list[str], ending: str, skipped: list[str]) -> SDKDownload | None:
    """The best Standard SDK archive among `urls` for this system, or None."""
    platform_name, architecture, _ = _platform_selector()
    pattern = re.compile(rf"tt5sdk_v(?P<version>\d+\.\d+[a-z]*){ending}", re.IGNORECASE)
    best: tuple[int, str, str] | None = None
    for url in urls:
        name = unquote(Path(urlparse(url).path).name)
        match = pattern.fullmatch(name)
        if not match:
            continue
        ubuntu = int(match.group("ubuntu")) if "ubuntu" in match.groupdict() else 0
        if ubuntu and not _ubuntu_build_runs_here(ubuntu):
            skipped.append(f"{name} (built on Ubuntu {ubuntu}, too new for this system)")
            continue
        if best is None or ubuntu > best[0]:
            best = (ubuntu, match.group("version"), url)
    if best is None:
        return None
    _check_url(best[2])
    return SDKDownload(best[1], platform_name, architecture, best[2])


def find_downloads(timeout: float = 20.0) -> list[SDKDownload]:
    """Standard SDK downloads that should run on this system, newest first."""
    platform_name, architecture, ending = _platform_selector()
    skipped: list[str] = []
    downloads: list[SDKDownload] = []
    try:
        folders = [u for u in _fetch_links(SDK_INDEX, timeout)
                   if re.fullmatch(r"v\d+\.\d+[a-z]*", Path(urlparse(u).path).name or "")
                   and urlparse(u).path.endswith("/") and u.startswith(SDK_INDEX)]
    except TeamTalkSDKError:
        folders = []
    folders.sort(key=lambda u: _version_key(Path(urlparse(u).path).name[1:]), reverse=True)
    for folder in folders[:MAX_VERSIONS_CHECKED]:
        try:
            found = _pick(_fetch_links(folder, timeout), ending, skipped)
        except TeamTalkSDKError:
            continue
        if found:
            downloads.append(found)
    if not downloads:
        # The listing could not be read: fall back to the links on the download page.
        try:
            found = _pick(_fetch_links(DOWNLOAD_PAGE, timeout), ending, skipped)
        except TeamTalkSDKError:
            found = None
        if found:
            downloads.append(found)
    if not downloads:
        detail = f" Skipped: {'; '.join(dict.fromkeys(skipped))}." if skipped else ""
        raise TeamTalkSDKError(
            f"BearWare does not offer a Standard SDK for {platform_name} {architecture} "
            f"that runs on this system.{detail}"
        )
    return downloads


def discover_latest_download(timeout: float = 20.0) -> SDKDownload:
    """Return the newest Standard SDK download that should run on this system."""
    return find_downloads(timeout)[0]


def _library_loads(library: Path) -> str:
    """Load the native library in a separate process. Returns "" or the loader's error."""
    if sys.platform == "win32":
        return ""
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import ctypes, sys; ctypes.CDLL(sys.argv[1])", str(library)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        return ""
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit code {result.returncode}"


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
        problem = _library_loads(_native_library_for(wrapper))
        if problem:
            raise TeamTalkSDKError(f"TeamTalk SDK {download.version} cannot run on this system: {problem}")
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
        f"\nThe newest TeamTalk SDK from BearWare that runs on this system is {download.version} "
        f"for {download.platform_name} {download.architecture}.\n"
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
    downloads = find_downloads()
    if not _confirm_download(downloads[0], error):
        raise TeamTalkSDKError(
            f"TeamTalk SDK installation declined. Install it manually from {DOWNLOAD_PAGE} "
            f"or set {SDK_DIR_ENV} to an existing SDK directory."
        )
    failures: list[str] = []
    for download in downloads:
        try:
            wrapper = install_sdk(download)
            return _import_from_wrapper(wrapper)
        except (ImportError, OSError, TeamTalkSDKError) as exc:
            failures.append(f"{download.version}: {exc}")
            sys.modules.pop("TeamTalk5", None)
            print(f"TeamTalk SDK {download.version} could not be used ({exc}).", file=sys.stderr)
    raise TeamTalkSDKError("No TeamTalk SDK could be installed and loaded. " + " | ".join(failures))
