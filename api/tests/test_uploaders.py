"""The file-upload backends. The FTP backend runs against an in-process FTP
server (pyftpdlib) - the one place the actual ftplib STOR is exercised, so
the thin adapter is proven to land the file and to translate a
connection/auth failure into UploadError. The path/credential guards live in
shared helpers (`_connection`, `_safe_target`) and are tested directly - they
protect every upload transport. The SFTP adapter's paramiko wiring is
asserted at its boundary."""

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import paramiko
import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from nimbleship.uploaders import (
    FtpFileUploader,
    SftpFileUploader,
    UploadError,
    _connection,
    _safe_target,
)

USERNAME = "nimbleship"
PASSWORD = "secret-pw"


@pytest.fixture
def ftp_server(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    root = tmp_path / "ftp"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user(USERNAME, PASSWORD, str(root), perm="elradfmw")
    handler = FTPHandler
    handler.authorizer = authorizer
    # Port 0 lets the OS pick a free port - no fixed-port collisions in CI.
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        # Poll the ioloop so shutdown - and close_all - happen on this same
        # thread; closing sockets from the test thread races the loop and
        # raises "Bad file descriptor".
        while not stop.is_set():
            server.serve_forever(timeout=0.1, blocking=False)
        server.close_all()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port, root
    finally:
        stop.set()
        thread.join(timeout=5)


def _config(port: int, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "ftp_host": "127.0.0.1",
        "ftp_port": port,
        "ftp_username": USERNAME,
        "ftp_password": PASSWORD,
    }
    config.update(overrides)
    return config


def test_upload_lands_the_file_with_exact_bytes(
    ftp_server: tuple[int, Path],
) -> None:
    port, root = ftp_server
    content = "LIM2,DMC95000254580,John Doe\r\n"

    FtpFileUploader().upload(_config(port), "/", "DMC95000254580.csv", content)

    landed = root / "DMC95000254580.csv"
    assert landed.exists()
    # Binary transfer preserves the carrier's CRLF exactly, byte for byte.
    assert landed.read_bytes() == content.encode("utf-8")


def test_bad_credentials_raise_upload_error(ftp_server: tuple[int, Path]) -> None:
    port, _ = ftp_server

    with pytest.raises(UploadError):
        FtpFileUploader().upload(
            _config(port, ftp_password="wrong"), "/", "x.csv", "data\r\n"
        )


def test_missing_host_raises_upload_error() -> None:
    with pytest.raises(UploadError, match="ftp_host"):
        FtpFileUploader().upload({}, "/", "x.csv", "data\r\n")


def test_a_control_character_in_the_filename_is_rejected(
    ftp_server: tuple[int, Path],
) -> None:
    # A CRLF in a filename would inject a second FTP command; the uploader
    # rejects it as a failed upload rather than let it reach the wire.
    port, _ = ftp_server
    with pytest.raises(UploadError, match="bare filename"):
        FtpFileUploader().upload(
            _config(port), "/", "evil\r\nDELE other.csv", "data\r\n"
        )


def test_a_control_character_in_the_remote_path_is_rejected(
    ftp_server: tuple[int, Path],
) -> None:
    port, _ = ftp_server
    with pytest.raises(UploadError, match="control character"):
        FtpFileUploader().upload(_config(port), "/out\r\nDELE x", "x.csv", "data\r\n")


def test_a_non_numeric_port_is_an_upload_error_not_a_raw_valueerror() -> None:
    with pytest.raises(UploadError, match="ftp_port"):
        FtpFileUploader().upload(
            {"ftp_host": "127.0.0.1", "ftp_port": "not-a-number"},
            "/",
            "x.csv",
            "data\r\n",
        )


def test_an_empty_remote_directory_is_rejected() -> None:
    with pytest.raises(UploadError, match="remote directory"):
        FtpFileUploader().upload({"ftp_host": "127.0.0.1"}, "", "x.csv", "data\r\n")


def test_a_path_separator_in_the_filename_is_rejected(
    ftp_server: tuple[int, Path],
) -> None:
    # A rendered filename that carries a slash (e.g. "../escaped.csv") would
    # write outside the configured remote directory; only a bare filename is
    # allowed.
    port, root = ftp_server
    with pytest.raises(UploadError, match="bare filename"):
        FtpFileUploader().upload(_config(port), "/", "../escaped.csv", "data\r\n")
    assert not (root.parent / "escaped.csv").exists()


def test_an_out_of_range_port_is_an_upload_error() -> None:
    with pytest.raises(UploadError, match="out of range"):
        FtpFileUploader().upload(
            {"ftp_host": "127.0.0.1", "ftp_port": "99999"}, "/", "x.csv", "data\r\n"
        )


def test_dotdot_in_the_remote_path_is_rejected(
    ftp_server: tuple[int, Path],
) -> None:
    # A `..` segment would land the file in a sibling carrier's directory;
    # the uploader refuses it (defence behind the config.* authoring rule).
    port, root = ftp_server
    with pytest.raises(UploadError, match="escapes"):
        FtpFileUploader().upload(
            _config(port), "incoming/fagans/../other_carrier", "x.csv", "data\r\n"
        )
    assert not (root / "incoming" / "other_carrier" / "x.csv").exists()


# --- Shared validation (protects every upload transport) --------------------


def test_safe_target_joins_directory_and_filename() -> None:
    assert _safe_target("/outbound", "x.csv") == "/outbound/x.csv"
    assert _safe_target("/outbound/", "x.csv") == "/outbound/x.csv"


def test_safe_target_rejects_traversal_and_separators() -> None:
    with pytest.raises(UploadError, match="escapes"):
        _safe_target("incoming/../other", "x.csv")
    with pytest.raises(UploadError, match="bare filename"):
        _safe_target("/outbound", "../x.csv")
    with pytest.raises(UploadError, match="bare filename"):
        _safe_target("/outbound", "a/b.csv")


def test_safe_target_rejects_control_characters_and_empty_dir() -> None:
    with pytest.raises(UploadError, match="control character"):
        _safe_target("/out\r\nDELE x", "x.csv")
    with pytest.raises(UploadError, match="bare filename"):
        _safe_target("/outbound", "evil\r\nDELE y")
    with pytest.raises(UploadError, match="remote directory"):
        _safe_target("", "x.csv")


def test_connection_reads_prefixed_config_and_validates_the_port() -> None:
    config: dict[str, object] = {
        "sftp_host": "sftp.example",
        "sftp_port": "2222",
        "sftp_username": "u",
        "sftp_password": "p",
    }
    assert _connection(config, "sftp", 22) == ("sftp.example", 2222, "u", "p")

    with pytest.raises(UploadError, match="sftp_host"):
        _connection({}, "sftp", 22)
    with pytest.raises(UploadError, match="out of range"):
        _connection({"sftp_host": "h", "sftp_port": "99999"}, "sftp", 22)
    with pytest.raises(UploadError, match="non-numeric"):
        _connection({"sftp_host": "h", "sftp_port": "nope"}, "sftp", 22)


# --- SFTP adapter (paramiko boundary) ---------------------------------------


class _FakeSftp:
    def __init__(self) -> None:
        self.puts: list[tuple[bytes, str]] = []

    def putfo(self, buffer: Any, target: str) -> None:
        self.puts.append((buffer.read(), target))


class _FakeTransport:
    instances: ClassVar[list["_FakeTransport"]] = []

    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address
        self.connected_as: tuple[str, str] | None = None
        self.closed = False
        self.sftp = _FakeSftp()
        _FakeTransport.instances.append(self)

    def connect(self, username: str, password: str) -> None:
        self.connected_as = (username, password)

    def close(self) -> None:
        self.closed = True


SFTP_CONFIG = {
    "sftp_host": "sftp.dachser.example",
    "sftp_port": 22,
    "sftp_username": "nimbleship",
    "sftp_password": "SECRET-PW",
}


@pytest.fixture
def fake_paramiko(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeTransport]]:
    _FakeTransport.instances = []
    monkeypatch.setattr("nimbleship.uploaders.paramiko.Transport", _FakeTransport)
    monkeypatch.setattr(
        "nimbleship.uploaders.paramiko.SFTPClient.from_transport",
        lambda transport: transport.sftp,
    )
    yield _FakeTransport


def test_sftp_upload_connects_and_writes_the_file(
    fake_paramiko: type[_FakeTransport],
) -> None:
    SftpFileUploader().upload(SFTP_CONFIG, "/inbox", "order.xml", "<x/>\r\n")

    [transport] = fake_paramiko.instances
    assert transport.address == ("sftp.dachser.example", 22)
    assert transport.connected_as == ("nimbleship", "SECRET-PW")
    assert transport.sftp.puts == [(b"<x/>\r\n", "/inbox/order.xml")]
    # The transport is always closed, even on the happy path.
    assert transport.closed is True


def test_sftp_upload_translates_a_paramiko_failure_to_upload_error(
    fake_paramiko: type[_FakeTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(self: _FakeSftp, buffer: Any, target: str) -> None:
        raise paramiko.SSHException("permission denied")

    monkeypatch.setattr(_FakeSftp, "putfo", _boom)
    with pytest.raises(UploadError, match="permission denied"):
        SftpFileUploader().upload(SFTP_CONFIG, "/inbox", "order.xml", "<x/>")
    # The transport is still closed in the finally, even when the put fails.
    assert fake_paramiko.instances[-1].closed is True


def test_sftp_upload_rejects_a_traversal_before_connecting(
    fake_paramiko: type[_FakeTransport],
) -> None:
    with pytest.raises(UploadError, match="escapes"):
        SftpFileUploader().upload(SFTP_CONFIG, "inbox/../other", "x.xml", "<x/>")
    # No connection was opened - the guard runs before paramiko.
    assert fake_paramiko.instances == []
