"""The outbound file-upload transport, provided as a small seam so the
engine stays transport-agnostic and tests substitute a fake uploader - the
suite never opens an FTP connection.

Connection details (host, port, credentials) come from the carrier's config
at execution, never from the rendered request, so the Golden Replay corpus
stays secret-free. A new file transport (sftp for Dachser) is a new
FileUploader implementation, not a new engine branch."""

import ftplib
import io
from typing import Protocol

CONNECT_TIMEOUT_SECONDS = 30.0

# ftplib.all_errors already bundles its own errors plus OSError and EOFError;
# naming it as a typed tuple keeps `except` happy under strict typing.
_FTP_ERRORS: tuple[type[BaseException], ...] = ftplib.all_errors


class UploadError(Exception):
    """A file upload that did not complete: connection, auth, or transfer.
    The engine catches this to mark the carrier call failed - transport
    specifics (ftplib errors, socket errors) are translated here."""


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
        host = config.get("ftp_host")
        if not isinstance(host, str) or not host:
            raise UploadError("carrier config has no ftp_host")
        try:
            port = int(str(config.get("ftp_port", 21)))
        except ValueError as error:
            raise UploadError(
                f"carrier config has a non-numeric ftp_port: {config.get('ftp_port')!r}"
            ) from error
        if not 0 <= port <= 65535:
            raise UploadError(f"carrier config ftp_port is out of range: {port}")
        if not remote_path:
            raise UploadError("upload has no remote directory")
        # Guard the path the STOR command builds. The filename and remote
        # directory both render from facts, so a control character could
        # inject a second line onto the FTP control connection, and a `..`
        # segment (or a slash in the filename) could escape the configured
        # directory. Reject any of these as a failed upload rather than let
        # it reach the wire. (Authoring also pins the remote directory to a
        # config.* source; this is the transport's own last line.)
        if any(ord(char) < 0x20 for char in remote_path):
            raise UploadError(
                f"remote path contains a control character: {remote_path!r}"
            )
        if ".." in remote_path.split("/"):
            raise UploadError(f"remote path escapes its directory: {remote_path!r}")
        if (
            any(ord(char) < 0x20 for char in filename)
            or "/" in filename
            or "\\" in filename
        ):
            raise UploadError(f"filename is not a bare filename: {filename!r}")
        username = str(config.get("ftp_username", ""))
        password = str(config.get("ftp_password", ""))
        target = f"{remote_path.rstrip('/')}/{filename}"
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


def carrier_file_uploader() -> FileUploader:
    """The uploader carrier calls use; callers own its lifetime. The engine
    picks the backend per transport once more than one exists."""
    return FtpFileUploader()


def get_file_uploader() -> FileUploader:
    """FastAPI dependency: the request-scoped uploader. Stateless (it
    connects per upload), so tests override this to inject a fake."""
    return carrier_file_uploader()
