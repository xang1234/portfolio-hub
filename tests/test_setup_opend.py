import importlib.util
import os
import stat
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace


def _load_setup_opend():
    script = Path(__file__).resolve().parents[1] / "scripts" / "setup_opend.py"
    spec = importlib.util.spec_from_file_location("setup_opend", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_file(path: Path, contents: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def test_validate_install_finds_macos_app_layout(tmp_path):
    setup_opend = _load_setup_opend()
    install_dir = tmp_path / ".opend"
    executable = install_dir / "moomoo_OpenD" / "FutuOpenD.app" / "Contents" / "MacOS" / "FutuOpenD"
    _write_file(executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(install_dir / "moomoo_OpenD" / "FutuOpenD.xml", "<config />")
    _write_file(install_dir / "moomoo_OpenD" / "Appdata.dat")

    result = setup_opend.validate_install(install_dir, system="Darwin")

    assert result.ok is True
    assert result.executable == executable
    assert result.config_file == install_dir / "moomoo_OpenD" / "FutuOpenD.xml"
    assert result.appdata_file == install_dir / "moomoo_OpenD" / "Appdata.dat"


def test_install_archive_extracts_and_marks_executable(tmp_path):
    setup_opend = _load_setup_opend()
    archive = tmp_path / "opend.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("moomoo_OpenD/FutuOpenD", "binary")
        zf.writestr("moomoo_OpenD/FutuOpenD.xml", "<config />")
        zf.writestr("moomoo_OpenD/Appdata.dat", "data")

    install_dir = tmp_path / ".opend"
    result = setup_opend.install_archive(archive, install_dir, force=False, system="Linux")

    assert result.ok is True
    assert result.executable == install_dir / "moomoo_OpenD" / "FutuOpenD"
    assert os.access(result.executable, os.X_OK)


def test_ensure_executable_skips_chmod_when_execute_bit_exists():
    setup_opend = _load_setup_opend()
    called = False

    class FakePath:
        def stat(self):
            return SimpleNamespace(st_mode=0o755)

        def chmod(self, mode):
            nonlocal called
            called = True

    setup_opend._ensure_executable(FakePath(), system="Darwin")

    assert called is False


def test_ensure_executable_adds_execute_bit_when_missing():
    setup_opend = _load_setup_opend()
    captured = {}

    class FakePath:
        def stat(self):
            return SimpleNamespace(st_mode=0o644)

        def chmod(self, mode):
            captured["mode"] = mode

    setup_opend._ensure_executable(FakePath(), system="Darwin")

    assert captured["mode"] == 0o744


def test_install_archive_extracts_macos_tar_gz(tmp_path):
    setup_opend = _load_setup_opend()
    source = tmp_path / "source"
    executable = source / "moomoo_OpenD" / "FutuOpenD.app" / "Contents" / "MacOS" / "FutuOpenD"
    _write_file(executable, "binary")
    _write_file(source / "moomoo_OpenD" / "FutuOpenD.xml", "<config />")
    _write_file(source / "moomoo_OpenD" / "Appdata.dat", "data")
    archive = tmp_path / "moomoo_OpenD_mac.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source / "moomoo_OpenD", arcname="moomoo_OpenD")

    install_dir = tmp_path / ".opend"
    result = setup_opend.install_archive(archive, install_dir, force=False, system="Darwin")

    assert result.ok is True
    assert result.executable == (
        install_dir / "moomoo_OpenD" / "FutuOpenD.app" / "Contents" / "MacOS" / "FutuOpenD"
    )


def test_install_archive_removes_macos_appledouble_files(tmp_path):
    setup_opend = _load_setup_opend()
    source = tmp_path / "source"
    executable = source / "moomoo_OpenD" / "OpenD.app" / "Contents" / "MacOS" / "OpenD"
    _write_file(executable, "binary")
    _write_file(source / "moomoo_OpenD" / "OpenD.xml", "<config />")
    _write_file(source / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Resources" / "Appdata.dat", "data")
    _write_file(source / "moomoo_OpenD" / "._OpenD.xml", "metadata")
    _write_file(source / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Frameworks" / "._F3CLogin.framework", "metadata")
    archive = tmp_path / "moomoo_OpenD_mac.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(source / "moomoo_OpenD", arcname="moomoo_OpenD")

    install_dir = tmp_path / ".opend"
    result = setup_opend.install_archive(archive, install_dir, force=False, system="Darwin")

    assert result.ok is True
    assert list(install_dir.rglob("._*")) == []


def test_validate_install_rejects_macos_appledouble_files(tmp_path):
    setup_opend = _load_setup_opend()
    install_dir = tmp_path / ".opend"
    executable = install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "MacOS" / "OpenD"
    _write_file(executable, "binary")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.xml", "<config />")
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Resources" / "Appdata.dat", "data")
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Frameworks" / "._F3CLogin.framework")

    result = setup_opend.validate_install(install_dir, system="Darwin")

    assert result.ok is False
    assert any("AppleDouble" in message for message in result.messages)


def test_install_archive_repairs_existing_macos_appledouble_files_without_force(tmp_path):
    setup_opend = _load_setup_opend()
    archive = tmp_path / "opend.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("moomoo_OpenD/OpenD", "new binary")
        zf.writestr("moomoo_OpenD/OpenD.xml", "<config />")
        zf.writestr("moomoo_OpenD/Appdata.dat", "data")

    install_dir = tmp_path / ".opend"
    executable = install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "MacOS" / "OpenD"
    _write_file(executable, "existing binary")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.xml", "<config />")
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Resources" / "Appdata.dat", "data")
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Frameworks" / "._F3CLogin.framework")

    result = setup_opend.install_archive(archive, install_dir, force=False, system="Darwin")

    assert result.ok is True
    assert result.executable == executable
    assert list(install_dir.rglob("._*")) == []


def test_discover_archive_on_macos_searches_tar_gz_not_zip(tmp_path):
    setup_opend = _load_setup_opend()
    zip_archive = tmp_path / "moomoo_OpenD_mac.zip"
    tar_archive = tmp_path / "moomoo_OpenD_mac.tar.gz"
    _write_file(zip_archive)
    _write_file(tar_archive)
    os.utime(zip_archive, (30, 30))
    os.utime(tar_archive, (10, 10))

    result = setup_opend.discover_archive(tmp_path, system="Darwin")

    assert result == tar_archive


def test_install_archive_existing_valid_install_is_idempotent_without_force(tmp_path):
    setup_opend = _load_setup_opend()
    archive = tmp_path / "opend.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("moomoo_OpenD/FutuOpenD", "new binary")
        zf.writestr("moomoo_OpenD/FutuOpenD.xml", "<config />")
        zf.writestr("moomoo_OpenD/Appdata.dat", "data")

    install_dir = tmp_path / ".opend"
    executable = install_dir / "moomoo_OpenD" / "FutuOpenD"
    _write_file(executable, "existing binary")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(install_dir / "moomoo_OpenD" / "FutuOpenD.xml", "<config />")
    _write_file(install_dir / "moomoo_OpenD" / "Appdata.dat")

    result = setup_opend.install_archive(archive, install_dir, force=False, system="Linux")

    assert result.ok is True
    assert result.executable == executable
    assert executable.read_text(encoding="utf-8") == "existing binary"


def test_config_check_flags_default_sample_credentials(tmp_path):
    setup_opend = _load_setup_opend()
    config = tmp_path / "OpenD.xml"
    _write_file(
        config,
        """
        <moomoo_opend>
          <login_account>100000</login_account>
          <login_pwd>123456</login_pwd>
        </moomoo_opend>
        """,
    )

    result = setup_opend.check_config(config)

    assert result.ok is False
    assert any("sample login values" in message for message in result.messages)


def test_config_check_accepts_non_sample_credentials(tmp_path):
    setup_opend = _load_setup_opend()
    config = tmp_path / "OpenD.xml"
    _write_file(
        config,
        """
        <moomoo_opend>
          <login_account>real-user-id</login_account>
          <login_pwd_md5>0123456789abcdef0123456789abcdef</login_pwd_md5>
        </moomoo_opend>
        """,
    )

    result = setup_opend.check_config(config)

    assert result.ok is True


def test_start_uses_absolute_paths_when_cwd_changes(monkeypatch, tmp_path):
    setup_opend = _load_setup_opend()
    monkeypatch.chdir(tmp_path)
    install_dir = Path(".opend")
    executable = install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "MacOS" / "OpenD"
    config = install_dir / "moomoo_OpenD" / "OpenD.xml"
    _write_file(executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(
        config,
        """
        <moomoo_opend>
          <login_account>real-user-id</login_account>
          <login_pwd_md5>0123456789abcdef0123456789abcdef</login_pwd_md5>
        </moomoo_opend>
        """,
    )
    _write_file(install_dir / "moomoo_OpenD" / "OpenD.app" / "Contents" / "Resources" / "Appdata.dat")
    captured = {}

    class FakeProcess:
        pid = 123

    def fake_popen(command, *, cwd, stdout, stderr):
        captured["command"] = command
        captured["cwd"] = cwd
        return FakeProcess()

    monkeypatch.setattr(setup_opend.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(setup_opend.platform, "system", lambda: "Darwin")

    result = setup_opend._cmd_start(
        SimpleNamespace(
            install_dir=install_dir,
            api_ip="127.0.0.1",
            api_port=11111,
            lang="en",
            foreground=False,
            wait_seconds=0.0,
        )
    )

    assert result == 0
    assert Path(captured["command"][0]).is_absolute()
    assert Path(captured["command"][1].removeprefix("-cfg_file=")).is_absolute()
    assert Path(captured["cwd"]).is_absolute()
    assert "-console=0" in captured["command"]


def test_start_refuses_default_sample_credentials(monkeypatch, tmp_path):
    setup_opend = _load_setup_opend()
    install_dir = tmp_path / ".opend"
    executable = install_dir / "moomoo_OpenD" / "OpenD"
    config = install_dir / "moomoo_OpenD" / "OpenD.xml"
    _write_file(executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(
        config,
        """
        <moomoo_opend>
          <login_account>100000</login_account>
          <login_pwd>123456</login_pwd>
        </moomoo_opend>
        """,
    )
    _write_file(install_dir / "moomoo_OpenD" / "Appdata.dat")

    def fail_popen(*args, **kwargs):
        raise AssertionError("OpenD should not be launched with sample credentials")

    monkeypatch.setattr(setup_opend.subprocess, "Popen", fail_popen)

    result = setup_opend._cmd_start(
        SimpleNamespace(
            install_dir=install_dir,
            api_ip="127.0.0.1",
            api_port=11111,
            lang="en",
            foreground=False,
            wait_seconds=0.0,
        )
    )

    assert result == 2


def test_start_returns_failure_when_port_does_not_open(monkeypatch, tmp_path):
    setup_opend = _load_setup_opend()
    install_dir = tmp_path / ".opend"
    executable = install_dir / "moomoo_OpenD" / "OpenD"
    config = install_dir / "moomoo_OpenD" / "OpenD.xml"
    _write_file(executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(
        config,
        """
        <moomoo_opend>
          <login_account>real-user-id</login_account>
          <login_pwd_md5>0123456789abcdef0123456789abcdef</login_pwd_md5>
        </moomoo_opend>
        """,
    )
    _write_file(install_dir / "moomoo_OpenD" / "Appdata.dat")

    class FakeProcess:
        pid = 123

        def poll(self):
            return None

    monkeypatch.setattr(setup_opend.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(setup_opend, "port_is_open", lambda *args, **kwargs: False)

    result = setup_opend._cmd_start(
        SimpleNamespace(
            install_dir=install_dir,
            api_ip="127.0.0.1",
            api_port=11111,
            lang="en",
            foreground=False,
            wait_seconds=0.01,
        )
    )

    assert result == 1


def test_start_command_uses_configured_xml_without_login_args(tmp_path):
    setup_opend = _load_setup_opend()
    install_dir = tmp_path / ".opend"
    executable = install_dir / "OpenD"
    config = install_dir / "OpenD.xml"
    _write_file(executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    _write_file(config, "<config />")
    _write_file(install_dir / "Appdata.dat")
    install = setup_opend.validate_install(install_dir, system="Linux")

    command = setup_opend.build_start_command(
        install,
        api_ip="127.0.0.1",
        api_port=11111,
        lang="en",
        extra_args=(),
    )

    assert command[:2] == [str(executable), f"-cfg_file={config}"]
    assert "-api_ip=127.0.0.1" in command
    assert "-api_port=11111" in command
    assert "-lang=en" in command
    assert not any("login" in arg.lower() for arg in command)


def test_default_check_host_maps_host_docker_internal_on_host(monkeypatch):
    setup_opend = _load_setup_opend()
    monkeypatch.setenv("FUTU_HOST", "host.docker.internal")
    monkeypatch.setattr(setup_opend, "running_in_container", lambda: False)

    assert setup_opend.default_check_host() == "127.0.0.1"
