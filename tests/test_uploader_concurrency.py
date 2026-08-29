"""Upload serialisation and stale hidden-file recovery.

One ReconnectingUploader (so one FTP control connection) is shared by the
per-file upload worker and the threads `Send now` spawns, so uploads have
to be serialised -- interleaving two on one connection desynchronises FTP
("Bad sequence of commands", "425 PASV: data transfer in progress"),
collides on the shared .tmp name, and races self._uploader to None.
"""
import ftplib
import threading

import pytest

from davis_clientraw.uploaders import ReconnectingUploader, parse_stale_hidden_file


class _RecordingUploader:
    """Fails loudly if two threads are ever inside an upload at once."""

    def __init__(self):
        self.concurrent = False
        self.uploads = 0
        self._active = 0
        self._guard = threading.Lock()

    def connect(self, timeout: float = 8.0):
        pass

    def is_alive(self):
        return True

    def atomic_upload(self, local_path, remote_dir, remote_name):
        with self._guard:
            self._active += 1
            if self._active > 1:
                self.concurrent = True
        # Long enough that unsynchronised callers would reliably overlap.
        threading.Event().wait(0.02)
        with self._guard:
            self._active -= 1
            self.uploads += 1

    def close(self):
        pass


def test_uploads_are_serialised(monkeypatch):
    recorder = _RecordingUploader()
    monkeypatch.setattr(
        "davis_clientraw.uploaders.build_uploader", lambda cfg: recorder
    )
    ru = ReconnectingUploader(lambda: {})

    threads = [
        threading.Thread(target=ru.upload, args=("/tmp/x", "/remote", "clientraw.txt"))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert recorder.uploads == 6
    assert not recorder.concurrent, "two uploads ran on one connection at once"


def test_failure_does_not_leave_uploader_none_for_other_threads(monkeypatch):
    """The NoneType crash: one thread nulling self._uploader mid-flight while
    another was between _ensure_connected() and atomic_upload()."""
    calls = {"n": 0}

    class Flaky(_RecordingUploader):
        def atomic_upload(self, local_path, remote_dir, remote_name):
            calls["n"] += 1
            if calls["n"] % 2:
                raise OSError("connection reset")
            super().atomic_upload(local_path, remote_dir, remote_name)

    monkeypatch.setattr("davis_clientraw.uploaders.build_uploader", lambda cfg: Flaky())
    monkeypatch.setattr("davis_clientraw.uploaders.time.sleep", lambda s: None)
    ru = ReconnectingUploader(lambda: {})

    errors = []

    def run():
        try:
            ru.upload("/tmp/x", "/remote", "clientraw.txt")
        except Exception as exc:  # pragma: no cover - the bug being guarded
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"upload() raised instead of returning False: {errors}"


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            "550 /domains/kitecheck.co.uk/public_html/clientraw.txt.tmp: Temporary "
            "hidden file /domains/kitecheck.co.uk/public_html/.in.clientraw.txt.tmp. "
            "already exists",
            "/domains/kitecheck.co.uk/public_html/.in.clientraw.txt.tmp.",
        ),
        (
            "550 clientraw.txt.tmp: Temporary hidden file /.in.clientraw.txt.tmp. "
            "already exists",
            "/.in.clientraw.txt.tmp.",
        ),
    ],
)
def test_parse_stale_hidden_file(message, expected):
    assert parse_stale_hidden_file(message) == expected


def test_parse_stale_hidden_file_ignores_unrelated_errors():
    assert parse_stale_hidden_file("550 public_html/x.tmp: No such file or directory") is None
    assert parse_stale_hidden_file("530 Login incorrect.") is None


def test_ftp_put_clears_stale_hidden_file_and_retries(tmp_path):
    from davis_clientraw.uploaders import FtpUploader

    local = tmp_path / "clientraw.txt"
    local.write_text("data")

    class FakeFtp:
        def __init__(self):
            self.deleted = []
            self.stores = []
            self.fail_next = True

        def storbinary(self, cmd, f):
            self.stores.append(cmd)
            if self.fail_next:
                self.fail_next = False
                raise ftplib.error_perm(
                    "550 /pub/clientraw.txt.tmp: Temporary hidden file "
                    "/pub/.in.clientraw.txt.tmp. already exists"
                )

        def delete(self, path):
            self.deleted.append(path)

    up = FtpUploader("h", 21, "u", "p")
    up._ftp = FakeFtp()
    up.put(str(local), "/pub/clientraw.txt.tmp")

    assert up._ftp.deleted == ["/pub/.in.clientraw.txt.tmp."]
    assert len(up._ftp.stores) == 2, "should retry the store after clearing"


def test_ftp_put_reraises_unrelated_perm_errors(tmp_path):
    from davis_clientraw.uploaders import FtpUploader

    local = tmp_path / "clientraw.txt"
    local.write_text("data")

    class FakeFtp:
        def __init__(self):
            self.deleted = []

        def storbinary(self, cmd, f):
            raise ftplib.error_perm("550 public_html/x.tmp: No such file or directory")

        def delete(self, path):  # pragma: no cover - must not be reached
            self.deleted.append(path)

    up = FtpUploader("h", 21, "u", "p")
    up._ftp = FakeFtp()
    with pytest.raises(ftplib.error_perm):
        up.put(str(local), "/pub/clientraw.txt.tmp")
    assert up._ftp.deleted == []
