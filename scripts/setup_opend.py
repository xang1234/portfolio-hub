#!/usr/bin/env python3
"""Install, check, and start Futu/Moomoo command-line OpenD.

This helper intentionally does not store broker credentials. Configure login
inside OpenD's XML or GUI, then use this script to keep the local install and
portfolio-hub connectivity boring.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple, Sequence


DOC_URL = "https://openapi.moomoo.com/moomoo-api-doc/en/opend/opend-cmd.html"
DEFAULT_INSTALL_DIR = Path(".opend")
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"
DEFAULT_PORT = 11111


class InstallCheck(NamedTuple):
    ok: bool
    install_dir: Path
    executable: Path | None
    config_file: Path | None
    appdata_file: Path | None
    messages: tuple[str, ...]


class ConfigCheck(NamedTuple):
    ok: bool
    config_file: Path
    messages: tuple[str, ...]


def running_in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False
    try:
        text = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(marker in text for marker in ("docker", "kubepods", "containerd"))


def default_check_host() -> str:
    host = os.environ.get("OPEND_HOST") or os.environ.get("FUTU_HOST") or "127.0.0.1"
    if host == "host.docker.internal" and not running_in_container():
        return "127.0.0.1"
    return host


def _candidate_suffixes(system: str) -> tuple[tuple[str, ...], ...]:
    if system == "Darwin":
        return (
            ("FutuOpenD.app", "Contents", "MacOS", "FutuOpenD"),
            ("OpenD.app", "Contents", "MacOS", "OpenD"),
            ("FutuOpenD",),
            ("OpenD",),
        )
    if system == "Windows":
        return (("FutuOpenD.exe",), ("OpenD.exe",))
    return (("FutuOpenD",), ("OpenD",))


def _has_suffix(path: Path, suffix: tuple[str, ...]) -> bool:
    return tuple(path.parts[-len(suffix):]) == suffix


def _find_by_suffix(root: Path, suffixes: Sequence[tuple[str, ...]]) -> Path | None:
    for suffix in suffixes:
        leaf = suffix[-1]
        matches = sorted(
            path for path in root.rglob(leaf)
            if path.is_file() and _has_suffix(path, suffix)
        )
        if matches:
            return matches[0]
    return None


def _find_named_file(root: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        matches = sorted(path for path in root.rglob(name) if path.is_file())
        if matches:
            return matches[0]
    return None


def _find_appledouble_files(root: Path, *, limit: int | None = None) -> tuple[Path, ...]:
    matches: list[Path] = []
    for path in sorted(root.rglob("._*")):
        if path.is_file():
            matches.append(path)
            if limit is not None and len(matches) >= limit:
                break
    return tuple(matches)


def remove_appledouble_files(root: Path | str) -> int:
    removed = 0
    for path in _find_appledouble_files(Path(root)):
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _ensure_executable(path: Path | None, *, system: str) -> None:
    if path is None or system == "Windows":
        return
    try:
        mode = path.stat().st_mode
        if mode & 0o100:
            return
        path.chmod(mode | 0o100)
    except OSError:
        return


def archive_patterns(system: str | None = None) -> tuple[str, ...]:
    system = system or platform.system()
    if system == "Darwin":
        return (
            "*OpenD*.tar.gz",
            "*opend*.tar.gz",
            "*OpenD*.tgz",
            "*opend*.tgz",
        )
    if system == "Windows":
        return ("*OpenD*.zip", "*opend*.zip")
    return (
        "*OpenD*.tar.gz",
        "*opend*.tar.gz",
        "*OpenD*.tgz",
        "*opend*.tgz",
    )


def discover_archive(download_dir: Path | str, *, system: str | None = None) -> Path | None:
    root = Path(download_dir).expanduser()
    if not root.exists():
        return None

    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in archive_patterns(system):
        for path in root.glob(pattern):
            if path.is_file() and path not in seen:
                candidates.append(path)
                seen.add(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def validate_install(install_dir: Path | str, *, system: str | None = None) -> InstallCheck:
    root = Path(install_dir)
    system = system or platform.system()
    messages: list[str] = []

    if not root.exists():
        return InstallCheck(
            ok=False,
            install_dir=root,
            executable=None,
            config_file=None,
            appdata_file=None,
            messages=(f"install directory does not exist: {root}",),
        )

    executable = _find_by_suffix(root, _candidate_suffixes(system))
    config_file = _find_named_file(root, ("FutuOpenD.xml", "OpenD.xml"))
    appdata_file = _find_named_file(root, ("Appdata.dat",))

    if executable is None:
        messages.append("missing OpenD executable")
    if config_file is None:
        messages.append("missing FutuOpenD.xml or OpenD.xml")
    if appdata_file is None:
        messages.append("missing Appdata.dat")
    if system == "Darwin":
        appledouble_files = _find_appledouble_files(root, limit=3)
        if appledouble_files:
            sample = ", ".join(str(path) for path in appledouble_files)
            messages.append(
                "contains macOS AppleDouble metadata files (._*) that can invalidate "
                f"OpenD code signing; remove them or run install again. Examples: {sample}"
            )

    return InstallCheck(
        ok=not messages,
        install_dir=root,
        executable=executable,
        config_file=config_file,
        appdata_file=appdata_file,
        messages=tuple(messages),
    )


def check_config(config_file: Path | str) -> ConfigCheck:
    path = Path(config_file)
    messages: list[str] = []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return ConfigCheck(False, path, (f"could not read OpenD XML config: {exc}",))

    login_account = (root.findtext("login_account") or "").strip()
    login_pwd = (root.findtext("login_pwd") or "").strip()
    login_pwd_md5 = (root.findtext("login_pwd_md5") or "").strip()

    if not login_account:
        messages.append("OpenD.xml has no active login_account value.")
    if not login_pwd and not login_pwd_md5:
        messages.append("OpenD.xml has neither active login_pwd nor login_pwd_md5.")
    if login_account == "100000" and login_pwd == "123456" and not login_pwd_md5:
        messages.append(
            "OpenD.xml still contains the bundled sample login values "
            "(login_account=100000 / login_pwd=123456)."
        )

    return ConfigCheck(not messages, path, tuple(messages))


def install_archive(
    archive: Path | str,
    install_dir: Path | str,
    *,
    force: bool,
    system: str | None = None,
) -> InstallCheck:
    archive_path = Path(archive)
    root = Path(install_dir)
    system = system or platform.system()

    if not archive_path.exists():
        return InstallCheck(False, root, None, None, None, (f"archive not found: {archive_path}",))

    if root.exists() and any(root.iterdir()):
        if not force:
            if system == "Darwin":
                remove_appledouble_files(root)
            existing = validate_install(root, system=system)
            if existing.ok:
                return existing
            return InstallCheck(
                False,
                root,
                None,
                None,
                None,
                (f"{root} already exists; pass --force to replace it",),
            )
        shutil.rmtree(root)

    root.mkdir(parents=True, exist_ok=True)
    try:
        shutil.unpack_archive(str(archive_path), str(root))
    except (shutil.ReadError, ValueError) as exc:
        return InstallCheck(False, root, None, None, None, (f"could not unpack archive: {exc}",))

    if system == "Darwin":
        remove_appledouble_files(root)

    result = validate_install(root, system=system)
    _ensure_executable(result.executable, system=system)
    return validate_install(root, system=system)


def download_archive(download_url: str, destination_dir: Path | str) -> Path:
    dest = Path(destination_dir)
    dest.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(download_url)
    filename = Path(parsed.path).name or "opend-download"
    target = dest / filename
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": "portfolio-hub-opend-helper/1.0"},
    )
    with urllib.request.urlopen(request) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
    return target


def port_is_open(host: str, port: int, *, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def build_start_command(
    install: InstallCheck,
    *,
    api_ip: str,
    api_port: int,
    lang: str,
    console: int | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    if not install.ok or install.executable is None or install.config_file is None:
        raise ValueError("OpenD install is incomplete")
    command = [
        str(install.executable.resolve()),
        f"-cfg_file={install.config_file.resolve()}",
        f"-api_ip={api_ip}",
        f"-api_port={api_port}",
        f"-lang={lang}",
    ]
    if console is not None:
        command.append(f"-console={console}")
    command.extend(extra_args)
    return command


def wait_for_port_or_exit(
    process: subprocess.Popen,
    *,
    host: str,
    port: int,
    timeout_s: float,
) -> tuple[bool, int | None]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_is_open(host, port, timeout_s=0.5):
            return True, process.poll()
        exit_code = process.poll()
        if exit_code is not None:
            return False, exit_code
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return port_is_open(host, port, timeout_s=0.5), process.poll()


def _print_install_check(result: InstallCheck) -> None:
    if result.ok:
        print(f"OpenD install found: {result.install_dir}")
        print(f"  executable: {result.executable}")
        print(f"  config:     {result.config_file}")
        print(f"  app data:   {result.appdata_file}")
        return
    print(f"OpenD install incomplete at {result.install_dir}")
    for message in result.messages:
        print(f"  - {message}")
    print(f"Download command-line OpenD from: {DOC_URL}")


def _print_config_check(result: ConfigCheck) -> None:
    if result.ok:
        return
    print(f"OpenD config needs attention: {result.config_file}")
    for message in result.messages:
        print(f"  - {message}")
    print("Edit OpenD.xml with your real Moomoo/Futu login before starting OpenD.")


def _cmd_check(args: argparse.Namespace) -> int:
    host = args.host or default_check_host()
    port = args.port
    if port_is_open(host, port, timeout_s=args.timeout):
        print(f"OpenD is reachable at {host}:{port}")
        return 0

    print(f"OpenD is not reachable at {host}:{port}")
    result = validate_install(args.install_dir)
    _print_install_check(result)
    if result.ok:
        config = check_config(result.config_file)
        if not config.ok:
            _print_config_check(config)
            return 1
        print("Try starting it with:")
        print(f"  {sys.argv[0]} start --install-dir {result.install_dir}")
    else:
        print("Install it with:")
        print(f"  {sys.argv[0]} install")
    return 1


def _cmd_install(args: argparse.Namespace) -> int:
    archive = Path(args.archive) if args.archive else None
    with tempfile.TemporaryDirectory(prefix="portfolio-hub-opend-") as tmp:
        if args.download_url:
            print(f"Downloading OpenD archive from {args.download_url}")
            archive = download_archive(args.download_url, tmp)
        if archive is None:
            archive = discover_archive(args.download_dir)
            if archive is not None:
                print(f"Using discovered OpenD archive: {archive}")
        if archive is None:
            patterns = ", ".join(archive_patterns())
            print(
                f"no OpenD archive found in {args.download_dir} "
                f"(looked for: {patterns}); pass --archive or --download-url",
                file=sys.stderr,
            )
            return 2

        result = install_archive(archive, args.install_dir, force=args.force)

    _print_install_check(result)
    return 0 if result.ok else 2


def _cmd_start(args: argparse.Namespace) -> int:
    install = validate_install(args.install_dir)
    _print_install_check(install)
    if not install.ok:
        return 2

    config = check_config(install.config_file)
    if not config.ok:
        _print_config_check(config)
        return 2

    system = platform.system()
    _ensure_executable(install.executable, system=system)
    command = build_start_command(
        install,
        api_ip=args.api_ip,
        api_port=args.api_port,
        lang=args.lang,
        console=1 if args.foreground else 0,
    )
    cwd = (install.config_file.parent if install.config_file else install.install_dir).resolve()
    print("Starting OpenD:")
    print("  " + " ".join(command))
    if args.foreground:
        return subprocess.call(command, cwd=str(cwd))

    log_path = install.install_dir / "opend.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT)
    print(f"OpenD started with PID {process.pid}; logs: {log_path}")
    if args.wait_seconds <= 0:
        return 0

    opened, exit_code = wait_for_port_or_exit(
        process,
        host=args.api_ip,
        port=args.api_port,
        timeout_s=args.wait_seconds,
    )
    if opened:
        print(f"OpenD is reachable at {args.api_ip}:{args.api_port}")
        return 0
    if exit_code is not None:
        print(f"OpenD exited before opening {args.api_ip}:{args.api_port} (exit code {exit_code}).")
    else:
        print(
            f"OpenD is still running, but {args.api_ip}:{args.api_port} "
            f"did not open within {args.wait_seconds:g}s."
        )
    print(f"Check logs at: {log_path}")
    if platform.system() == "Darwin":
        print("On macOS, also check System Settings > Privacy & Security for a blocked OpenD launch.")
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install, check, and start Futu/Moomoo command-line OpenD.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Check whether OpenD is reachable.")
    check.add_argument("--host", default=None, help="OpenD host to probe. Defaults to FUTU_HOST/127.0.0.1.")
    check.add_argument("--port", type=int, default=int(os.environ.get("FUTU_PORT", DEFAULT_PORT)))
    check.add_argument("--timeout", type=float, default=2.0)
    check.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    check.set_defaults(func=_cmd_check)

    install = subparsers.add_parser("install", help="Unpack a command-line OpenD archive.")
    source = install.add_mutually_exclusive_group(required=False)
    source.add_argument("--archive", type=Path, help="Downloaded command-line OpenD archive.")
    source.add_argument("--download-url", help="Official command-line OpenD archive URL.")
    install.add_argument(
        "--download-dir",
        type=Path,
        default=DEFAULT_DOWNLOAD_DIR,
        help="Directory to search when --archive is omitted. Default: ~/Downloads.",
    )
    install.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    install.add_argument("--force", action="store_true", help="Replace an existing install directory.")
    install.set_defaults(func=_cmd_install)

    start = subparsers.add_parser("start", help="Start an installed command-line OpenD.")
    start.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    start.add_argument("--api-ip", default=os.environ.get("OPEND_API_IP", "127.0.0.1"))
    start.add_argument("--api-port", type=int, default=int(os.environ.get("FUTU_PORT", DEFAULT_PORT)))
    start.add_argument("--lang", default=os.environ.get("OPEND_LANG", "en"))
    start.add_argument("--foreground", action="store_true", help="Run OpenD in the foreground.")
    start.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("OPEND_START_WAIT_SECONDS", "30")),
        help="Seconds to wait for OpenD to open the API port. Use 0 to return immediately.",
    )
    start.set_defaults(func=_cmd_start)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
