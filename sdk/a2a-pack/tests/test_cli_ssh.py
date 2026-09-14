from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from a2a_pack.cli import main as cli_main
from a2a_pack.cli.main import _infer_agent_name, _write_devbox_ssh_config, app

runner = CliRunner()


# --------------------------------------------------------------------------- #
# agent name inference
# --------------------------------------------------------------------------- #


def test_infer_agent_name_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a2a.yaml").write_text("name: my-agent\n")
    monkeypatch.chdir(tmp_path)
    assert _infer_agent_name() == "my-agent"


def test_infer_agent_name_walks_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a2a.yaml").write_text("name: root-agent\n")
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert _infer_agent_name() == "root-agent"


def test_infer_agent_name_none_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _infer_agent_name() is None


def test_ssh_fails_without_agent_or_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ssh"])
    assert result.exit_code != 0
    assert "no a2a.yaml" in result.output


# --------------------------------------------------------------------------- #
# ssh config writer
# --------------------------------------------------------------------------- #


def test_ssh_config_proxycommand_uses_absolute_a2a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli_main, "_a2a_executable", lambda: "/opt/venv/bin/a2a")
    alias = _write_devbox_ssh_config("demo", tmp_path / "key")
    assert alias == "demo.a2a"
    cfg = (tmp_path / ".ssh" / "config").read_text()
    assert 'ProxyCommand "/opt/venv/bin/a2a" ssh-proxy demo' in cfg


# --------------------------------------------------------------------------- #
# connection details helper / --json
# --------------------------------------------------------------------------- #


def test_conn_details_paths_are_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_a2a_executable", lambda: "/opt/venv/bin/a2a")
    info = cli_main._devbox_conn_details("demo", tmp_path / "key", "demo.a2a")
    assert info["user"] == "dev"
    assert info["auth"] == "publickey"
    for field in ("identity_file", "public_key_file", "known_hosts_file", "ssh_config_file"):
        assert Path(info[field]).is_absolute(), field
    assert info["proxy_command"] == '"/opt/venv/bin/a2a" ssh-proxy demo'
    assert "port" not in info


def test_conn_details_with_tunnel_port(tmp_path: Path) -> None:
    info = cli_main._devbox_conn_details("demo", tmp_path / "key", "demo.a2a", port=2222)
    assert info["host"] == "127.0.0.1"
    assert info["port"] == 2222


def test_ssh_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    key = tmp_path / "key"
    key.write_text("PRIVATE")
    monkeypatch.setattr(
        cli_main, "_ensure_devbox_key", lambda agent: (key, "ssh-ed25519 AAA test")
    )
    monkeypatch.setattr(cli_main, "_write_devbox_ssh_config", lambda agent, k: f"{agent}.a2a")
    result = runner.invoke(app, ["ssh", "demo", "--json"])
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["host_alias"] == "demo.a2a"
    assert Path(info["identity_file"]).is_absolute()


# --------------------------------------------------------------------------- #
# credentials preload
# --------------------------------------------------------------------------- #


def test_devbox_credentials_json_from_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from a2a_pack.cli import credentials as creds_mod
    from a2a_pack.cli.credentials import Credentials

    monkeypatch.setattr(
        creds_mod,
        "load",
        lambda: Credentials(
            api_url="https://api.example.com",
            token="tok",
            email="me@example.com",
            refresh_token="ref",
        ),
    )
    data = json.loads(cli_main._devbox_credentials_json())
    assert data["token"] == "tok"
    assert data["refresh_token"] == "ref"
    assert "user_id" not in data  # Nones dropped

    monkeypatch.setattr(creds_mod, "load", lambda: None)
    assert cli_main._devbox_credentials_json() is None


# --------------------------------------------------------------------------- #
# local TCP tunnel
# --------------------------------------------------------------------------- #


class _FakeWS:
    """Echoes every frame sent to it, then closes."""

    def __init__(self) -> None:
        self._frames: list[bytes] = []
        self._event = threading.Event()
        self._closed = False

    def send(self, data: bytes) -> None:
        self._frames.append(bytes(data))
        self._event.set()

    def close(self) -> None:
        self._closed = True
        self._event.set()

    def __iter__(self):
        while True:
            self._event.wait(timeout=5)
            self._event.clear()
            if self._frames:
                yield b"echo:" + self._frames.pop(0)
            if self._closed:
                return


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_tunnel_bridges_tcp_to_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "key"
    key.write_text("PRIVATE")
    monkeypatch.setattr(
        cli_main, "_ensure_devbox_key", lambda agent: (key, "ssh-ed25519 AAA test")
    )

    class _StubClient:
        def agent_ssh(self, *, name: str, public_key: str, **kw) -> dict[str, str]:
            assert name == "demo"
            return {"wss_url": "wss://unit.test/ssh", "access_token": "tok"}

    monkeypatch.setattr(cli_main, "_client", lambda api: _StubClient())
    monkeypatch.setattr(
        cli_main, "_connect_ws_retry", lambda connect, url, token: _FakeWS()
    )

    port = _free_port()
    t = threading.Thread(
        target=cli_main._serve_devbox_tunnel, args=("demo", port, key, None), daemon=True
    )
    t.start()

    conn = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    assert conn is not None, "tunnel never started listening"

    conn.sendall(b"SSH-2.0-test\r\n")
    conn.settimeout(5)
    data = conn.recv(65536)
    assert data == b"echo:SSH-2.0-test\r\n"
    conn.close()
