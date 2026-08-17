from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
REMOTE_ROOT = "/opt/mailcom-manager-public"


class ServerSyncError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ServerSyncResult:
    accounts: int
    aliases: int
    host_key_sha256: str


class ServerSyncService:
    def push(
        self,
        snapshot: dict[str, Any],
        *,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> ServerSyncResult:
        normalized_host = host.strip()
        normalized_username = username.strip()
        if not normalized_host or not HOST_PATTERN.fullmatch(normalized_host):
            raise ServerSyncError("server_host_invalid", "服务器主机格式无效")
        if not 1 <= int(port) <= 65535:
            raise ServerSyncError("server_port_invalid", "SSH 端口无效")
        if not normalized_username:
            raise ServerSyncError("server_username_missing", "服务器用户名不能为空")
        if not password:
            raise ServerSyncError("server_password_missing", "服务器密码不能为空")

        try:
            import paramiko
        except ImportError as exc:
            raise ServerSyncError(
                "server_sync_dependency_missing",
                "缺少 Paramiko，请重新运行 app\\setup.ps1",
            ) from exc

        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        remote_path = f"/tmp/mailcom-sync-{uuid4().hex}.json"
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                normalized_host,
                port=int(port),
                username=normalized_username,
                password=password,
                look_for_keys=False,
                allow_agent=False,
                timeout=12,
                banner_timeout=12,
                auth_timeout=12,
            )
            transport = client.get_transport()
            if transport is None or transport.get_remote_server_key() is None:
                raise ServerSyncError("server_ssh_unavailable", "SSH 会话未建立")
            fingerprint = hashlib.sha256(
                transport.get_remote_server_key().asbytes()
            ).hexdigest()

            sftp = client.open_sftp()
            try:
                with sftp.file(remote_path, "wb") as remote_file:
                    remote_file.write(payload)
                    remote_file.flush()
                sftp.chmod(remote_path, 0o600)
            finally:
                sftp.close()

            quoted_snapshot = shlex.quote(remote_path)
            command = (
                "set -eu; "
                f"test -x {REMOTE_ROOT}/venv/bin/python; "
                f"test -f {REMOTE_ROOT}/sync_snapshot.py; "
                "set -a; . /etc/mailcom-manager-public.env; set +a; "
                f"{REMOTE_ROOT}/venv/bin/python {REMOTE_ROOT}/sync_snapshot.py "
                f"{quoted_snapshot}; rc=$?; rm -f {quoted_snapshot}; exit $rc"
            )
            _, stdout, stderr = client.exec_command(command, timeout=90)
            output = stdout.read().decode("utf-8", errors="replace").strip()
            error_output = stderr.read().decode("utf-8", errors="replace").strip()
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                message = (
                    "服务器接收端尚未部署"
                    if exit_status in {1, 127}
                    else "服务器更新失败"
                )
                raise ServerSyncError(
                    "server_sync_remote_failed",
                    f"{message}（退出码 {exit_status}）",
                )
            try:
                result = json.loads(output)
                accounts = int(result["accounts"])
                aliases = int(result["aliases"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ServerSyncError(
                    "server_sync_response_invalid",
                    "服务器返回了无效的同步结果",
                ) from exc
            if error_output:
                raise ServerSyncError(
                    "server_sync_remote_warning",
                    "服务器同步产生了异常输出",
                )
            return ServerSyncResult(accounts, aliases, fingerprint)
        except ServerSyncError:
            raise
        except Exception as exc:
            raise ServerSyncError(
                "server_sync_connection_failed",
                f"服务器连接失败：{type(exc).__name__}",
            ) from exc
        finally:
            try:
                if client.get_transport() is not None:
                    client.exec_command(f"rm -f {shlex.quote(remote_path)}", timeout=5)
            except Exception:
                pass
            client.close()
