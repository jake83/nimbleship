"""The outbound file-upload transport, provided as a small seam so the
engine stays transport-agnostic and tests substitute a fake uploader - the
suite never opens an FTP connection.

Connection details (host, port, credentials) come from the carrier's config
at execution, never from the rendered request, so the Golden Replay corpus
stays secret-free. A new file transport (sftp for Dachser) is a new
FileUploader implementation, not a new engine branch."""

import ftplib
import io
from typing import Protocol, runtime_checkable

CONNECT_TIMEOUT_SECONDS = 30.0

# ftplib.all_errors already bundles its own errors plus OSError and EOFError;
# naming it as a typed tuple keeps `except` happy under strict typing.
_FTP_ERRORS: tuple[type[BaseException], ...] = ftplib.all_errors


class UploadError(Exception):
    """A file upload that did not complete: connection, auth, or transfer.
    The engine catches this to mark the carrier call failed - transport
    specifics (ftplib errors, socket errors) are translated here."""


@runtime_checkable
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
        port = int(str(config.get("ftp_port", 21)))
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
