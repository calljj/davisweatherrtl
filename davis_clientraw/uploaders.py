"""Transport-agnostic uploader with connection reuse and atomic per-file upload."""
from __future__ import annotations

import ftplib
import logging
import posixpath
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class UploadError(Exception):
    pass


# How often paramiko sends an SSH-level keepalive over an idle connection.
# Without this, a connection that dies silently (e.g. a NAT/firewall drops
# it without sending RST/FIN) can leave Transport's background read loop
# blocked indefinitely on a socket that will never produce data again --
# the socket-level timeout in connect_first_address() only covers the
# initial connect, not paramiko's internal reads afterward, and paramiko is
# known not to reliably surface a plain socket.timeout on those as a fatal
# error. set_keepalive() operates at the SSH protocol layer instead, so a
# dead connection gets detected and torn down rather than hanging forever.
SSH_KEEPALIVE_SEC = 15


def connect_first_address(host: str, port: int, timeout: float):
    """Connect to only the first DNS-resolved address, with a hard timeout.

    socket.create_connection() (and paramiko/ftplib internally) iterate every
    resolved address -- IPv4 and IPv6 -- at the full timeout each, so an
    unreachable multi-record host (e.g. behind a CDN) can hang for
    timeout * N addresses instead of just timeout. Bounding to one address
    keeps a bad host from blocking the whole reconnect loop (or the web UI's
    synchronous "Test connection" request).
    """
    import socket

    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    family, socktype, proto, _, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    sock.connect(sockaddr)
    return sock


class Uploader(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def put(self, local_path: str, remote_path: str) -> None: ...

    @abstractmethod
    def rename(self, remote_old: str, remote_new: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def atomic_upload(self, local_path: str, remote_dir: str, remote_name: str) -> None:
        tmp_name = remote_name + ".tmp"
        remote_tmp = posixpath.join(remote_dir, tmp_name)
        remote_final = posixpath.join(remote_dir, remote_name)
        self.put(local_path, remote_tmp)
        self.rename(remote_tmp, remote_final)


class SftpUploader(Uploader):
    def __init__(self, host: str, port: int, user: str, password: str = "", key_file: str = ""):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.key_file = key_file
        self._transport = None
        self._sftp = None

    def connect(self, timeout: float = 15.0) -> None:
        import paramiko

        sock = connect_first_address(self.host, self.port, timeout)
        self._transport = paramiko.Transport(sock)
        if self.key_file:
            pkey = paramiko.RSAKey.from_private_key_file(self.key_file)
            self._transport.connect(username=self.user, pkey=pkey)
        else:
            self._transport.connect(username=self.user, password=self.password)
        self._transport.set_keepalive(SSH_KEEPALIVE_SEC)
        self._sftp = paramiko.SFTPClient.from_transport(self._transport)

    def put(self, local_path: str, remote_path: str) -> None:
        self._sftp.put(local_path, remote_path)

    def rename(self, remote_old: str, remote_new: str) -> None:
        try:
            self._sftp.remove(remote_new)
        except IOError:
            pass
        self._sftp.rename(remote_old, remote_new)

    def close(self) -> None:
        if self._sftp:
            self._sftp.close()
        if self._transport:
            self._transport.close()

    def is_alive(self) -> bool:
        return bool(self._transport and self._transport.is_active())


class ScpUploader(Uploader):
    """Uploads over plain SSH exec (cat > file), avoiding a dependency on the
    sftp subsystem or a separate scp library -- only paramiko is required."""

    def __init__(self, host: str, port: int, user: str, key_file: str = "", password: str = ""):
        self.host = host
        self.port = port
        self.user = user
        self.key_file = key_file
        self.password = password
        self._client = None

    def connect(self, timeout: float = 15.0) -> None:
        import paramiko

        sock = connect_first_address(self.host, self.port, timeout)
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {"username": self.user, "sock": sock, "timeout": timeout}
        if self.key_file:
            kwargs["key_filename"] = self.key_file
        else:
            kwargs["password"] = self.password
        self._client.connect(self.host, **kwargs)
        transport = self._client.get_transport()
        if transport is not None:
            transport.set_keepalive(SSH_KEEPALIVE_SEC)

    def put(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as f:
            data = f.read()
        stdin, stdout, stderr = self._client.exec_command(f"cat > {_shquote(remote_path)}")
        stdin.write(data)
        stdin.channel.shutdown_write()
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise UploadError(f"scp put failed: {stderr.read().decode(errors='replace')}")

    def rename(self, remote_old: str, remote_new: str) -> None:
        cmd = f"mv -f {_shquote(remote_old)} {_shquote(remote_new)}"
        stdin, stdout, stderr = self._client.exec_command(cmd)
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise UploadError(f"scp rename failed: {stderr.read().decode(errors='replace')}")

    def close(self) -> None:
        if self._client:
            self._client.close()

    def is_alive(self) -> bool:
        if not self._client:
            return False
        transport = self._client.get_transport()
        return bool(transport and transport.is_active())


class FtpUploader(Uploader):
    def __init__(self, host: str, port: int, user: str, password: str, ftps: bool = False):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.ftps = ftps
        self._ftp = None

    def connect(self, timeout: float = 8.0) -> None:
        # ftplib doesn't accept a pre-connected socket, so unlike Sftp/ScpUploader
        # this can still iterate multiple DNS-resolved addresses internally;
        # kept short so a bad host fails fast rather than hanging the reconnect loop.
        self._ftp = ftplib.FTP_TLS() if self.ftps else ftplib.FTP()
        self._ftp.connect(self.host, self.port, timeout=timeout)
        self._ftp.login(self.user, self.password)
        if self.ftps:
            self._ftp.prot_p()

    def put(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as f:
            self._ftp.storbinary(f"STOR {remote_path}", f)

    def rename(self, remote_old: str, remote_new: str) -> None:
        try:
            self._ftp.delete(remote_new)
        except ftplib.error_perm:
            pass
        self._ftp.rename(remote_old, remote_new)

    def close(self) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                self._ftp.close()

    def is_alive(self) -> bool:
        if not self._ftp:
            return False
        try:
            self._ftp.voidcmd("NOOP")
            return True
        except Exception:
            return False


def _shquote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def build_uploader(config: dict) -> Uploader:
    transport = config["transport"]
    if transport == "sftp":
        c = config["sftp"]
        return SftpUploader(c["host"], c["port"], c["user"], c.get("password", ""), c.get("key_file", ""))
    if transport == "scp":
        c = config["scp"]
        return ScpUploader(c["host"], c["port"], c["user"], c.get("key_file", ""), c.get("password", ""))
    if transport == "ftp":
        c = config["ftp"]
        return FtpUploader(c["host"], c["port"], c["user"], c.get("password", ""), c.get("ftps", False))
    raise ValueError(f"unknown transport: {transport}")


class ReconnectingUploader:
    """Wraps an Uploader, keeping the connection open across many uploads and
    reconnecting with exponential backoff on failure. Intended for the
    clientraw.txt 2-second upload cadence."""

    def __init__(self, config_getter):
        self._config_getter = config_getter
        self._uploader: Uploader | None = None
        self._backoff = 1.0

    def _ensure_connected(self) -> None:
        if self._uploader is not None and getattr(self._uploader, "is_alive", lambda: True)():
            return
        if self._uploader is not None:
            try:
                self._uploader.close()
            except Exception:
                pass
            self._uploader = None
        self._uploader = build_uploader(self._config_getter())
        self._uploader.connect()
        self._backoff = 1.0

    def upload(self, local_path: str, remote_dir: str, remote_name: str) -> bool:
        try:
            self._ensure_connected()
            self._uploader.atomic_upload(local_path, remote_dir, remote_name)
            return True
        except Exception as exc:
            logger.warning("upload of %s failed: %s", remote_name, exc)
            if self._uploader is not None:
                try:
                    self._uploader.close()
                except Exception:
                    pass
                self._uploader = None
            time.sleep(min(self._backoff, 30))
            self._backoff *= 2
            return False

    def close(self) -> None:
        if self._uploader is not None:
            try:
                self._uploader.close()
            except Exception:
                pass
            self._uploader = None
