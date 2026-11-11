"""Provision a pinned, private LibreOffice without a machine-wide installation."""

from __future__ import annotations

import hashlib
import io
import logging
import lzma
import os
import platform
import shutil
import subprocess
import sysconfig
import tarfile
import tempfile
import time
import zipfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from filelock import FileLock

if TYPE_CHECKING or __package__:
    from . import office_compat, office_fetch
    from .office_fetch import MAX_DOWNLOAD
    from .office_fetch import download as _download
    from .office_fetch import mirror_rate as _mirror_rate
    from .process import (
        DocumentError,
        _cleanup_profile,
        inherit_parent_access,
        new_office_profile,
        office_profile_key,
        office_profile_template,
        run,
    )
else:
    import office_compat
    import office_fetch
    from office_fetch import MAX_DOWNLOAD
    from office_fetch import download as _download
    from office_fetch import mirror_rate as _mirror_rate
    from process import (
        DocumentError,
        _cleanup_profile,
        inherit_parent_access,
        new_office_profile,
        office_profile_key,
        office_profile_template,
        run,
    )

VERSION = "26.2.6"
# Published by The Document Foundation at each download URL's .sha256 endpoint.
ASSETS = {
    ("Windows", "x86_64"): (
        "win/x86_64",
        "Win_x86-64.msi",
        "f9877032fd908beb9c0ddf06df4af5c2e85f419c42e14876c4cce5aae5fb2660",
    ),
    ("Windows", "aarch64"): (
        "win/aarch64",
        "Win_aarch64.msi",
        "13be874da62665756b6d7c8a6afbc50e6465382a885ad90acf4dbe806d51db24",
    ),
    ("Linux", "x86_64"): (
        "deb/x86_64",
        "Linux_x86-64_deb.tar.gz",
        "fd0e8f8f2408dd2e5b90286e60f3f97cf566ba441cd48cfc5bcc68067303e0bc",
    ),
    ("Linux", "aarch64"): (
        "deb/aarch64",
        "Linux_aarch64_deb.tar.gz",
        "f8e8b1d30abde0d530d727ce1b26909ccbedc3d76bcf75d93b3dd5fcd5b8d278",
    ),
    ("Darwin", "x86_64"): (
        "mac/x86_64",
        "MacOS_x86-64.dmg",
        "135b8a95b8133396d54bf8e726dbc0066145efa0d785963fc3d4592acbfcfe5b",
    ),
    ("Darwin", "aarch64"): (
        "mac/aarch64",
        "MacOS_aarch64.dmg",
        "94bb3248df074c225490a8a6d1d9dc87c7d6783dbb7a8e9f0d0c3d94348552af",
    ),
}
# Bundled extensions a headless converter never loads: the Calc NLP solver and the
# MediaWiki publisher (about 3,000 files together).
UNREAD_EXTENSIONS = frozenset({"nlpsolver", "wiki-publisher"})
# A laid-out image's first-start profile, kept beside the image (build_profile_starter).
PROFILE_STARTER = "office-profile.zip"
logger = logging.getLogger(__name__)


def asset() -> tuple[str, str, str]:
    """Return this host's fixed official download URL, filename and SHA-256."""
    machine = platform.machine().lower() or sysconfig.get_platform().split("-")[-1]
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    key = (platform.system(), architecture)
    if key not in ASSETS:
        raise DocumentError(
            f"No private LibreOffice build for {key}; configure CLIO_DOCUMENT_SOFFICE"
        )
    directory, suffix, checksum = ASSETS[key]
    name = f"LibreOffice_{VERSION}_{suffix}"
    url = f"https://download.documentfoundation.org/libreoffice/stable/{VERSION}/{directory}/{name}"
    return url, name, checksum


INSTALLER_DOWNLOADS = "CLIO_INSTALL_DOWNLOADS"
#: Advertised in the release's install plan (``scripts/release_install_plan.py``): this
#: app waits for a download still in progress, so the installer may prepare before
#: its own downloads finish.
INSTALLER_PLAN_CAPABILITY = "waits-for-partial-downloads"
#: A partial download that stops growing for this long is abandoned; the app fetches it.
_INSTALLER_STALL_S = 60.0


def _await_installer_file(candidate: Path, url: str | None = None, checksum: str = "") -> None:
    """Wait while the installer still writes ``NAME.part``, renamed to ``NAME`` when verified.

    With ``url``, a prefetch still running after one floor window is compared once with
    the mirrors' race rate: one growing slower than the download floor (the installer's
    curl drew a slow mirror) is left, and the app fetches the file itself.
    """
    partial = candidate.with_name(candidate.name + ".part")
    began = time.monotonic()
    size, changed = -1, began
    window: deque[tuple[float, int]] = deque()
    probed, reference, total = False, 0.0, 0
    while not candidate.exists():
        try:
            current = partial.stat().st_size
        except OSError:
            return
        now = time.monotonic()
        if current != size:
            size, changed = current, now
        elif now - changed > _INSTALLER_STALL_S:
            logger.warning("installer download %s stalled; fetching it here", candidate.name)
            return
        if now - began > office_fetch.DOWNLOAD_BUDGET_S:
            logger.warning(
                "installer download %s took over %d seconds; fetching it here",
                candidate.name,
                office_fetch.DOWNLOAD_BUDGET_S,
            )
            return
        window.append((now, current))
        while len(window) > 1 and now - window[1][0] >= office_fetch.FLOOR_WINDOW_S:
            window.popleft()
        if url and now - began >= office_fetch.FLOOR_WINDOW_S:
            if not probed:
                probed = True
                reference, total = _mirror_rate(url, checksum) or (0.0, 0)
            growth = (current - window[0][1]) / max(now - window[0][0], 1e-3)
            if (
                reference
                and growth < reference * office_fetch.FLOOR_FRACTION
                and total - current > growth * office_fetch.FLOOR_WINDOW_S
            ):
                logger.warning(
                    "reason=office_prefetch_slow name=%s mb_s=%.2f mirror_mb_s=%.2f; "
                    "fetching it here",
                    candidate.name,
                    growth / 1e6,
                    reference / 1e6,
                )
                return
        time.sleep(0.25)


def installer_download(name: str, checksum: str, url: str | None = None) -> Path | None:
    """The installer's concurrent download of this exact file, when it has one.

    ``install.sh --desktop`` fetches LibreOffice while the app downloads and names the
    folder in ``CLIO_INSTALL_DOWNLOADS``; only a file whose bytes match the pin counts.
    A download still in progress is waited for rather than fetched a second time, unless
    ``url``'s mirrors are much faster than it (see ``_await_installer_file``).
    """
    folder = os.environ.get(INSTALLER_DOWNLOADS, "")
    if not folder or Path(name).name != name:
        return None
    candidate = Path(folder) / name
    _await_installer_file(candidate, url, checksum)
    try:
        if not candidate.is_file() or candidate.is_symlink():
            return None
        with candidate.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None
    return candidate if digest == checksum else None


def executable(root: Path) -> Path | None:
    """Find an executable only inside a private renderer tree."""
    candidates = [
        *([root / office_compat.WRAPPER] if office_compat.supported() else []),
        root / "program" / "soffice.com",
        root / "LibreOffice" / "program" / "soffice.com",
        root / "LibreOffice.app" / "Contents" / "MacOS" / "soffice",
        root / "opt" / "libreoffice26.2" / "program" / "soffice",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _deb_data(stream: BinaryIO) -> bytes:
    if stream.read(8) != b"!<arch>\n":
        raise DocumentError("Invalid LibreOffice Debian archive")
    while header := stream.read(60):
        if len(header) != 60 or header[58:] != b"`\n":
            raise DocumentError("Invalid Debian archive member header")
        size = int(header[48:58])
        if size < 0 or size > MAX_DOWNLOAD:
            raise DocumentError("Debian member exceeds its size limit")
        payload = stream.read(size)
        if len(payload) != size:
            raise DocumentError("Truncated Debian archive member")
        if size % 2:
            stream.read(1)
        if header[:16].decode("ascii").strip().rstrip("/").startswith("data.tar."):
            return payload
    raise DocumentError("Debian archive contains no data payload")


_XZ_MAGIC = b"\xfd7zXZ\x00"


def _deb_tar(package: Path) -> bytes:
    """A package's data tar, xz-decompressed (lzma releases the GIL, so threads overlap).

    Other compressions are returned as they are; ``tarfile`` opens them itself.
    """
    with package.open("rb") as source:
        payload = _deb_data(source)
    return lzma.decompress(payload) if payload.startswith(_XZ_MAGIC) else payload


def _extract_linux(archive: Path, root: Path) -> None:
    """Unpack the /opt application of every package in the archive into ``root``.

    Decompressing the ~40 packages' xz payloads is most of the time (about 16 of 22 s
    on one core), so a few are decompressed ahead on worker threads while one thread
    writes the files in package order. At most ``2 * workers`` decompressed payloads
    are held at once.
    """
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        packages_root = Path(temporary)
        with tarfile.open(archive) as outer:
            outer.extractall(packages_root, filter="data")
        packages = iter(sorted(packages_root.rglob("*.deb")))
        workers = max(1, min(4, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="office-unpack") as pool:
            pending: deque[Future[bytes]] = deque()

            def submit_next() -> None:
                package = next(packages, None)
                if package is not None:
                    pending.append(pool.submit(_deb_tar, package))

            for _ in range(2 * workers):
                submit_next()
            while pending:
                payload = pending.popleft().result()
                submit_next()
                with tarfile.open(fileobj=io.BytesIO(payload)) as contents:
                    # Only the self-contained /opt application, not distro integration.
                    members = [
                        member
                        for member in contents
                        if member.name.removeprefix("./").startswith("opt/")
                    ]
                    contents.extractall(root, members=members, filter="data")


def _copy_app_bundle(source: Path, target: Path) -> None:
    """Copy a signed macOS app out of its image with its signature metadata intact.

    ``shutil.copytree`` cannot copy extended attributes on macOS (``os.listxattr``
    is Linux-only), which drops the ``com.apple.cs.*`` signatures of non-Mach-O
    files. ``ditto`` keeps xattrs, ACLs and symlinks, and is part of base macOS.
    """
    ditto = Path("/usr/bin/ditto")
    if ditto.is_file():
        run([str(ditto), str(source), str(target)], cwd=target.parent, timeout=300)
    else:
        shutil.copytree(source, target, symlinks=True)


def _extract(archive: Path, root: Path) -> None:
    if platform.system() == "Windows":
        from importlib import import_module

        module = import_module(f"{__package__}.windows_msi" if __package__ else "windows_msi")
        module.extract_application(archive, root)
    elif platform.system() == "Darwin":
        with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
            mount = Path(temporary)
            run(
                [
                    "/usr/bin/hdiutil",
                    "attach",
                    str(archive),
                    "-readonly",
                    "-nobrowse",
                    # The image's SHA-256 is already pinned and checked; skip a second full read.
                    "-noverify",
                    "-mountpoint",
                    str(mount),
                ],
                cwd=root,
                timeout=120,
            )
            try:
                _copy_app_bundle(mount / "LibreOffice.app", root / "LibreOffice.app")
            finally:
                run(["/usr/bin/hdiutil", "detach", str(mount)], cwd=root, timeout=30)
    else:
        _extract_linux(archive, root)


def _remove_tree(path: Path) -> int:
    files = sum(len(names) for _, _, names in os.walk(path))
    shutil.rmtree(path)
    return files


def prune_unread(image: Path) -> int:
    """Remove the parts of an extracted LibreOffice that headless conversion never reads.

    Every installed file costs a write and, on Windows, an antivirus scan (gact-tui#739).
    The same list applies to all platforms' layouts: the offline help, the
    ``UNREAD_EXTENSIONS`` and the non-English UI translations (``resource/<lang>``).
    The application, filters, fonts, dictionaries and English (``en*``) stay, so
    conversions are unchanged. Returns the number of files removed.

    macOS is skipped: LibreOffice.app is TDF's notarized bundle, and removing any
    sealed resource fails ``codesign --verify`` and Gatekeeper.
    """
    if platform.system() == "Darwin":
        return 0
    removed = 0
    for directory, names, _ in os.walk(image):
        path = Path(directory)
        if path.name == "extensions":
            for name in sorted(UNREAD_EXTENSIONS.intersection(names)):
                removed += _remove_tree(path / name)
        elif path.name == "resource":
            for name in sorted(names):
                if not name.lower().startswith("en") and (path / name / "LC_MESSAGES").is_dir():
                    removed += _remove_tree(path / name)
        elif "program" in names and "help" in names:
            removed += _remove_tree(path / "help")
        # Translations and extensions are near the top of every layout; skip the rest.
        names[:] = [
            name
            for name in names
            if (path / name).exists() and len((path / name).relative_to(image).parts) < 6
        ]
    return removed


def _seed_document(path: Path) -> None:
    """Write the smallest Word document, so seeding loads the OOXML import and PDF export."""
    package = "http://schemas.openxmlformats.org/package/2006"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            f'<Types xmlns="{package}/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{package}/relationships"><Relationship Id="r1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>CLIO</w:t></w:r></w:p></w:body></w:document>",
        )


def _publish_profile(work: Path, template: Path) -> None:
    """Keep a verified first-start profile under its Office build's key."""
    pdf = work / "seed.pdf"
    settings = work / "profile" / "user" / "registrymodifications.xcu"
    if not pdf.is_file() or not pdf.stat().st_size:
        raise DocumentError("LibreOffice returned without converting the seed document")
    if "MacroSecurityLevel" not in settings.read_text(encoding="utf-8", errors="replace"):
        raise DocumentError("LibreOffice dropped the private profile's macro security setting")
    shutil.rmtree(template, ignore_errors=True)
    template.mkdir(parents=True)
    for attempt in range(20):
        try:
            (work / "profile").rename(template / "profile")
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32} or attempt == 19:
                raise DocumentError(f"Could not keep the seeded Office profile: {error}") from error
            time.sleep(0.1)


def _start_profile(profile: Path, starter: Path | None) -> None:
    """Begin from the profile laid out with this Office image, else from an empty one."""
    if starter is not None:
        try:
            with zipfile.ZipFile(starter) as archive:
                archive.extractall(profile)
            return
        except (OSError, zipfile.BadZipFile) as error:
            logger.warning("reason=office_profile_starter_unusable: %s", error)
            shutil.rmtree(profile, ignore_errors=True)
    new_office_profile(profile)


def build_profile_starter(executable_path: Path, archive: Path) -> int:
    """Archive a first-start profile for an Office image laid out at release time.

    Building the profile's extension registry takes a fresh LibreOffice several
    seconds; prepare starts its verifying conversion from this archive instead.
    The profile holds no paths or device caches, so it relocates with the image.
    """
    with tempfile.TemporaryDirectory(prefix="lo-starter-") as temporary:
        templates = Path(temporary)
        _verify_office(executable_path, cwd=executable_path.parent, profile_templates=templates)
        profile = office_profile_template(executable_path, templates)
        if profile is None:
            raise DocumentError("LibreOffice did not leave a seeded profile")
        partial = archive.with_name(archive.name + ".part")
        members = sorted(path for path in (profile / "user").rglob("*") if path.is_file())
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in members:
                bundle.write(path, path.relative_to(profile).as_posix())
        partial.replace(archive)
        return len(members)


def _verify_office(
    executable_path: Path,
    *,
    cwd: Path,
    profile_templates: Path | None = None,
    profile_starter: Path | None = None,
) -> None:
    """Probe without joining another Office instance or touching the user's profile.

    With ``profile_templates`` the probe is one real conversion instead of
    ``--version``, done once per Office build. That pays the cold first load of
    the conversion libraries during preparation and keeps its first-start profile,
    which every later conversion copies instead of rebuilding. A build that already
    has its seeded profile has converted successfully and is not probed again.
    ``profile_starter`` is the profile archive laid out with this Office image.
    """
    # A real Windows first launch can exhaust the native probe deadline while
    # the same binary verifies promptly after its timed-out process tree retires.
    # A macOS first launch is likewise delayed by the one-time Gatekeeper/XProtect
    # assessment of the freshly copied app. Recover once with a new profile; keep
    # the deadline and fail closed when neither attempt verifies. Other failures
    # and Linux are not retried.
    if profile_templates is not None:
        profile_templates.mkdir(parents=True, exist_ok=True)
        key = office_profile_key(executable_path)
        lock = FileLock(str(profile_templates / f"{key}.lock"), timeout=600)
        lock.acquire()
    try:
        if (
            profile_templates is not None
            and office_profile_template(executable_path, profile_templates) is not None
        ):
            # This exact build already converted a document when it was seeded.
            return
        seed = profile_templates is not None
        attempts = 2 if platform.system() in {"Windows", "Darwin"} else 1
        for attempt in range(attempts):
            temporary = tempfile.TemporaryDirectory(
                prefix="lo-seed-" if seed else "lo-version-",
                dir=profile_templates if seed else None,
            )
            try:
                work = Path(temporary.name).resolve()
                profile = work / "profile"
                if seed:
                    # The kept profile must stay readable to confined document workers.
                    inherit_parent_access(work)
                    _start_profile(profile, profile_starter)
                    _seed_document(work / "seed.docx")
                    arguments = ["--nodefault", "--nofirststartwizard", "--convert-to", "pdf"]
                    arguments += ["--outdir", str(work), str(work / "seed.docx")]
                else:
                    arguments = ["--norestore", "--version"]
                run(
                    [
                        str(executable_path),
                        f"-env:UserInstallation={profile.as_uri()}",
                        "--headless",
                        "--nologo",
                        *arguments,
                    ],
                    cwd=cwd,
                    timeout=120 if seed else 30,
                )
                if seed:
                    _publish_profile(work, profile_templates / key)
                return
            except DocumentError as error:
                if attempt + 1 == attempts or not isinstance(
                    error.__cause__, subprocess.TimeoutExpired
                ):
                    raise
                logger.warning(
                    "reason=office_probe_timeout_retry: %s; "
                    "retrying once with a fresh private profile",
                    error,
                )
            finally:
                _cleanup_profile(temporary)
    finally:
        if profile_templates is not None:
            lock.release()


def seed_profile(executable_path: Path, profile_templates: Path) -> None:
    """Seed a native LibreOffice's first-start profile; conversions still work without it."""
    try:
        if office_profile_template(executable_path, profile_templates) is None:
            _verify_office(
                executable_path,
                cwd=executable_path.parent,
                profile_templates=profile_templates,
            )
    except (DocumentError, OSError) as error:
        logger.warning("reason=office_profile_seed_failed: %s", error)


def _publish_image(image: Path, root: Path) -> None:
    """Allow briefly retained Windows handles to close before publishing the image."""
    for attempt in range(20):
        if root.exists():
            raise DocumentError(f"Incomplete renderer cache at {root}; remove it before retrying")
        try:
            image.rename(root)
            return
        except OSError as error:
            if (
                platform.system() != "Windows"
                or getattr(error, "winerror", None) not in {5, 32}
                or attempt == 19
            ):
                raise
            time.sleep(0.1)


def _office_archive(archive: Path, name: str, checksum: str, url: str) -> Path:
    """The verified archive: the cached copy, the installer's download, or a new download."""
    if archive.is_file():
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == checksum:
                return archive
    prefetched = installer_download(name, checksum, url)
    if prefetched is not None:
        return prefetched
    partial = archive.with_suffix(archive.suffix + ".part")
    _download(url, partial, checksum)
    partial.replace(archive)
    return archive


def ensure_office(
    root: Path,
    *,
    profile_templates: Path | None = None,
    profile_starter: Path | None = None,
) -> str:
    """Download, verify and extract the renderer once under an owned cache root.

    The tree includes LibreOffice's license/notices. On Linux, Office runs on its
    own pinned userland (``office_compat``), so it needs nothing from the host.
    """
    root = root.resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root.parent / f"{root.name}.lock"), timeout=600):
        existing = executable(root)
        if existing:
            if office_compat.supported():
                office_compat.prepare(root, download=_download, deb_data=_deb_data)
                existing = executable(root)
                if existing is None:
                    raise DocumentError(
                        "Private LibreOffice launcher disappeared during preparation"
                    )
            inherit_parent_access(root)
            _verify_office(
                existing,
                cwd=root,
                profile_templates=profile_templates,
                profile_starter=profile_starter,
            )
            return str(existing)
        url, name, checksum = asset()
        downloads = root.parent / "downloads"
        downloads.mkdir(exist_ok=True)
        archive = downloads / name
        with tempfile.TemporaryDirectory(prefix="office-extract-", dir=root.parent) as temporary:
            staging = Path(temporary)
            inherit_parent_access(staging)
            image = staging / "image"
            image.mkdir()
            # The pinned userland (~150 small packages) needs nothing from the archive:
            # it is fetched and unpacked beside the image while the archive downloads
            # and unpacks, and moved in after pruning, which never walks it.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="office-compat") as side:
                userland: Future[None] | None = None
                beside = staging / "userland"
                if office_compat.supported():
                    beside.mkdir()
                    userland = side.submit(
                        office_compat.prepare, beside, download=_download, deb_data=_deb_data
                    )
                source = _office_archive(archive, name, checksum, url)
                try:
                    _extract(source, image)
                    prune_unread(image)
                    if userland is not None:
                        userland.result()
                        directory = office_compat.runtime_directory()
                        (beside / directory).rename(image / directory)
                        # The userland is in place: this only publishes Office's launcher.
                        office_compat.prepare(image, download=_download, deb_data=_deb_data)
                    inherit_parent_access(image)
                    candidate = executable(image)
                    if candidate is None:
                        raise DocumentError("LibreOffice image contains no supported executable")
                    _verify_office(candidate, cwd=image, profile_templates=profile_templates)
                    _publish_image(image, root)
                except (OSError, ValueError, tarfile.TarError) as exc:
                    raise DocumentError(f"LibreOffice extraction failed: {exc}") from exc
        candidate = executable(root)
        if candidate is None:
            raise DocumentError("LibreOffice renderer disappeared after provisioning")
        archive.unlink(missing_ok=True)
        return str(candidate)
