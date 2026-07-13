"""The outbound file-upload transports, provided as a small seam so the
engine stays transport-agnostic and tests substitute a fake uploader - the
suite never opens a real connection. The engine picks a backend from a
transport->uploader registry (`carrier_uploaders`); a transport with no
entry is refused at execution rather than reaching a carrier over a protocol
the engine cannot speak.

Connection details (host, port, credentials) come from the carrier's config
at execution, never from the rendered request, so the Golden Replay corpus
stays secret-free."""

import ftplib
import io
from typing import Protocol

import paramiko

CONNECT_TIMEOUT_SECONDS = 30.0

# ftplib.all_errors already bundles its own errors plus OSError and EOFError;
# naming it as a typed tuple keeps `except` happy under strict typing.
_FTP_ERRORS: tuple[type[BaseException], ...] = ftplib.all_errors


class UploadError(Exception):
    """A file upload that did not complete: connection, auth, or transfer.
    The engine catches this to mark the carrier call failed - transport
    specifics (ftplib/paramiko/socket errors) are translated here."""


class FileUploader(Protocol):
    def upload(
        self,
        config: dict[str, object],
        remote_path: str,
        filename: str,
        content: str,
    ) -> None:
        """Write `content` to `remote_path/filename` on the server named by
        `config`. Raise UploadError on any failure."""
        ...


def _connection(
    config: dict[str, object], prefix: str, default_port: int
) -> tuple[str, int, str, str]:
    """Read and validate a carrier's `{prefix}_host/port/username/password`
    connection facts, translating a malformed one to UploadError so it lands
    as a booking failure rather than an uncaught error at trailer-close."""
    host = config.get(f"{prefix}_host")
    if not isinstance(host, str) or not host:
        raise UploadError(f"carrier config has no {prefix}_host")
    try:
        port = int(str(config.get(f"{prefix}_port", default_port)))
    except ValueError as error:
        raise UploadError(
            f"carrier config has a non-numeric {prefix}_port: "
            f"{config.get(f'{prefix}_port')!r}"
        ) from error
    if not 0 <= port <= 65535:
        raise UploadError(f"carrier config {prefix}_port is out of range: {port}")
    return (
        host,
        port,
        str(config.get(f"{prefix}_username", "")),
        str(config.get(f"{prefix}_password", "")),
    )


def _safe_target(remote_path: str, filename: str) -> str:
    """Build `remote_path/filename`, guarding the path both render from facts.
    A control character could inject a second protocol command, a `..` segment
    could escape the configured directory, and a slash in the filename could
    do the same - reject any as a failed upload rather than reach the wire.
    (Authoring also pins the remote directory to a config.* source; this is
    the transport's own last line.)"""
    if not remote_path:
        raise UploadError("upload has no remote directory")
    if any(ord(char) < 0x20 for char in remote_path):
        raise UploadError(f"remote path contains a control character: {remote_path!r}")
    if ".." in remote_path.split("/"):
        raise UploadError(f"remote path escapes its directory: {remote_path!r}")
    if (
        any(ord(char) < 0x20 for char in filename)
        or "/" in filename
        or "\\" in filename
    ):
        raise UploadError(f"filename is not a bare filename: {filename!r}")
    return f"{remote_path.rstrip('/')}/{filename}"


class FtpFileUploader:
    """Plain FTP over the standard library. Passive mode, binary transfer of
    the already-rendered bytes (the renderer emits the carrier's CRLF line
    endings, so no ASCII translation is wanted)."""

    def upload(
        self,
        config: dict[str, object],
        remote_path: str,
        filename: str,
        content: str,
    ) -> None:
        host, port, username, password = _connection(config, "ftp", 21)
        target = _safe_target(remote_path, filename)
        ftp = ftplib.FTP()
        try:
            ftp.connect(host, port, timeout=CONNECT_TIMEOUT_SECONDS)
            ftp.login(username, password)
            ftp.set_pasv(True)
            ftp.storbinary(f"STOR {target}", io.BytesIO(content.encode("utf-8")))
        except _FTP_ERRORS as error:
            raise UploadError(f"FTP upload to {target} failed: {error}") from error
        finally:
            try:
                ftp.quit()
            except _FTP_ERRORS:
                ftp.close()


class SftpFileUploader:
    """SFTP over paramiko: password auth, binary write of the rendered bytes.
    Credentials come from the carrier's `sftp_*` config."""

    def upload(
        self,
        config: dict[str, object],
        remote_path: str,
        filename: str,
        content: str,
    ) -> None:
        host, port, username, password = _connection(config, "sftp", 22)
        target = _safe_target(remote_path, filename)
        # Transport((host, port)) opens the socket in its constructor, so it
        # must sit inside the try: a refused connection or DNS failure has to
        # translate to UploadError, not escape as a raw OSError.
        transport: paramiko.Transport | None = None
        try:
            transport = paramiko.Transport((host, port))
            transport.connect(username=username, password=password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            if sftp is None:
                raise UploadError(f"SFTP to {host} could not open a session")
            sftp.putfo(io.BytesIO(content.encode("utf-8")), target)
        except (OSError, paramiko.SSHException) as error:
            raise UploadError(f"SFTP upload to {target} failed: {error}") from error
        finally:
            if transport is not None:
                transport.close()


def carrier_uploaders() -> dict[str, FileUploader]:
    """The transport->uploader registry. Every UPLOAD_TRANSPORTS the schema
    admits must appear here (a test enforces it), so no publishable upload
    transport can reach execution without a backend."""
    return {"ftp_upload": FtpFileUploader(), "sftp_upload": SftpFileUploader()}


def get_carrier_uploaders() -> dict[str, FileUploader]:
    """FastAPI dependency: the request-scoped uploader registry. The backends
    are stateless (they connect per upload), so tests override this to inject
    fakes."""
    return carrier_uploaders()
