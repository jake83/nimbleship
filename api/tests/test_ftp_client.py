"""The real FTP backend against an in-process FTP server (pyftpdlib): the
one place the actual ftplib STOR is exercised, so the thin adapter is proven
to land the file and to translate a connection/auth failure into UploadError
- everything else mocks the uploader."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from nimbleship.ftp_client import FtpFileUploader, UploadError

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
