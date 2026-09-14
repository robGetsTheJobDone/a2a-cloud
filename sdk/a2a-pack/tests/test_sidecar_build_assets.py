from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_DIR = ROOT / "docker" / "sidecar"
BUILD_SCRIPT = SIDECAR_DIR / "a2a-sidecar-build"


def _write_dsl(workdir: Path, *, language: str, command: list[str] | None = None) -> None:
    dsl_dir = workdir / ".a2a"
    dsl_dir.mkdir(parents=True)
    (dsl_dir / "agent.dsl.json").write_text(
        json.dumps(
            {
                "schema_version": "2026-06-04",
                "language": language,
                "name": f"{language}-agent",
                "description": "fixture",
                "version": "0.1.0",
                "entrypoint": {
                    "module": None,
                    "class_name": None,
                    "function": None,
                    "command": command or ["./worker"],
                },
                "skills": [
                    {
                        "name": "run",
                        "description": "Run",
                        "handler": "run",
                        "tags": [],
                        "scopes": [],
                        "stream": False,
                        "policy": {},
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                        "output_schema": {"type": "object"},
                    }
                ],
                "capabilities": {},
                "input_modes": ["application/json"],
                "output_modes": ["application/json"],
                "required_secrets": [],
                "required_env": [],
                "consumer_setup": {"fields": []},
                "runtime": {},
                "workspace_access": {"enabled": False},
                "auth": {
                    "model": "NoAuth",
                    "strategy": "public",
                    "principal_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "required": False,
                },
                "metadata": {},
            }
        )
    )


def test_sidecar_build_script_is_executable_and_valid_shell() -> None:
    assert os.access(BUILD_SCRIPT, os.X_OK)
    subprocess.run(["sh", "-n", str(BUILD_SCRIPT)], check=True)


def test_sidecar_dockerfiles_install_build_script_and_a2a_cli() -> None:
    expected = {
        "node.Dockerfile": "FROM node:20-bookworm-slim",
        "go.Dockerfile": "FROM golang:1.22-bookworm",
        "rust.Dockerfile": "FROM rust:1-bookworm",
        "java.Dockerfile": "FROM maven:3.9-eclipse-temurin-21",
        "dotnet.Dockerfile": "FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim",
    }
    for filename, base in expected.items():
        body = (SIDECAR_DIR / filename).read_text()
        assert base in body
        assert "COPY docker/sidecar/a2a-sidecar-build /usr/local/bin/a2a-sidecar-build" in body
        assert "/opt/a2a-sidecar/bin/pip install --no-cache-dir ." in body
        assert "/opt/a2a-sidecar/bin/a2a --help >/dev/null" in body


def test_sidecar_build_script_accepts_supported_dsl_languages_without_build_files(
    tmp_path: Path,
) -> None:
    for language in ("typescript", "javascript", "go", "rust", "java", "dotnet"):
        workdir = tmp_path / language
        workdir.mkdir()
        _write_dsl(workdir, language=language)
        subprocess.run([str(BUILD_SCRIPT), language], cwd=workdir, check=True)


def test_sidecar_build_script_rejects_language_mismatch(tmp_path: Path) -> None:
    _write_dsl(tmp_path, language="typescript", command=["node", "dist/worker.js"])
    result = subprocess.run(
        [str(BUILD_SCRIPT), "go"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "DSL language is typescript, not go" in result.stderr


def test_sidecar_build_script_rejects_missing_dsl(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(BUILD_SCRIPT), "typescript"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "missing Agent DSL" in result.stderr
