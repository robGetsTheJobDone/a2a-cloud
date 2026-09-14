"""Rot guard for the SDK's examples and its own documentation.

Examples are the highest-trust artifact a developer touches. This module keeps
four cheap, offline invariants true:

* every example under ``examples/`` compiles and imports;
* every example that *can* run offline actually runs to completion, and prints
  what its docstring says it prints;
* every example *project* (``examples/*/a2a.yaml``) loads and publishes skills,
  regardless of what earlier tests left on ``sys.path``;
* every repository path named in the package's own docs actually exists.

Scanning scope for the last one, stated precisely so nobody over-trusts it:
the sources scanned are ``README.md``, ``docs/**/*.md``, ``examples/**/README.md``
and the *module docstrings* of ``examples/**/*.py`` (not their full source --
in-code strings like ``"charts/dashboard.png"`` are workspace keys, not repo
paths). Within those sources a token is checked when it is rooted at one of the
explicit prefix lists below, or when it is written relative (``./`` or ``../``).

Nothing here starts a server, opens a socket, or needs an LLM key. The
subprocess runs inherit a copy of the environment with every ``A2A_*``
variable stripped, so a developer's platform keys cannot mask a failure.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import py_compile
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
EXAMPLES = PACKAGE_ROOT / "examples"

# Example modules that are importable on their own, i.e. `python -m examples.X`
# works from the package root. Anything else is an example *project* and is
# loaded through its own directory (see PROJECT_EXAMPLES).
STANDALONE_EXAMPLES = sorted(
    path for path in EXAMPLES.glob("*.py") if not path.name.startswith("_")
)

# Example projects: a directory with an a2a.yaml whose modules import each
# other by bare name, so the project directory has to be on sys.path.
PROJECT_EXAMPLES = sorted(
    path.parent for path in EXAMPLES.glob("*/a2a.yaml")
)

# How each standalone example is exercised end-to-end. Keyed by module stem,
# valued by the extra argv it needs to complete offline. Every standalone
# example must appear here -- test_every_standalone_example_declares_a_run
# fails when a new one is added without a decision, so "this example has no
# offline run" can never be the silent default.
EXAMPLE_RUNS: dict[str, tuple[str, ...]] = {
    "research_agent": (),
    "multi_agent": (),
    "meta_agent_research": (),
    # The only example that needs a live microVM sandbox; --card is the
    # offline path its docstring advertises.
    "coder_agent": ("--card",),
}

# Documentation surface owned by this package.
DOC_FILES = sorted(
    [PACKAGE_ROOT / "README.md"]
    + list((PACKAGE_ROOT / "docs").rglob("*.md"))
    + list(EXAMPLES.rglob("README.md"))
)

# Path prefixes that must resolve. Kept explicit rather than derived from the
# filesystem so that deleting or renaming one of these directories fails this
# test loudly instead of silently skipping every reference to it.
REPO_PREFIXES = (
    "apps",
    "sdk",
    "control-plane",
    "web",
    "infra",
    "scripts",
    "third_party",
)
PACKAGE_PREFIXES = (
    "a2a_pack",
    "examples",
    "tests",
    "docs",
    "typescript",
    "go",
    "rust",
    "docker",
)
# Prefixes resolved against an example project's own directory, for docs that
# live inside that project (its README says `db/migrations`, meaning its own).
PROJECT_PREFIXES = (
    "frontend",
    "db",
    "src",
    "tests",
)

# A rooted path token: starts with a bare word, contains at least one slash.
# Bounded so it does not run into URLs, backticks, or shell punctuation.
_PATH_TOKEN = re.compile(r"(?<![\w:/.-])([A-Za-z_][\w.-]*(?:/[\w.*-]+)+)")
# A relative path token: `./x`, `../x`, `../../apps/sandbox-runtime`. The
# rooted pattern above cannot see these -- its lookbehind rejects a leading
# dot -- which is how a broken `pip install -e '../../apps/...'` slipped past.
_RELATIVE_TOKEN = re.compile(r"(?<![\w:/-])(\.{1,2}/[\w./-]+)")
_DASH_M_EXAMPLE = re.compile(r"python -m (examples\.[\w.]+)")


def _iter_path_tokens(text: str):
    # A trailing "." is sentence punctuation after a rooted path ("see
    # docs/foo.md.") but load-bearing in a relative one ("cd ../../..").
    for pattern, trailing in (
        (_PATH_TOKEN, ".,;:)]}'\""),
        (_RELATIVE_TOKEN, ",;:)]}'\""),
    ):
        for match in pattern.finditer(text):
            token = match.group(1).rstrip(trailing)
            if token and "*" not in token:
                yield token


def _owning_project(doc: Path) -> Path | None:
    """The example project a doc lives in, if any."""

    for project in PROJECT_EXAMPLES:
        if doc.is_relative_to(project):
            return project
    return None


def _candidate_paths(doc: Path, token: str) -> list[Path]:
    """Every place a documented token is allowed to resolve, or [] to skip it.

    A relative token gets several bases on purpose: shell blocks in the docs
    are written after a ``cd``, so ``../../apps/x`` in an ``examples/`` file
    is relative to the documented working directory, not to the file. Accept
    any base -- the rot worth catching is the target vanishing from all of
    them.
    """

    if token.startswith("."):
        return [
            (base / token).resolve()
            for base in (doc.parent, PACKAGE_ROOT, REPO_ROOT)
        ]

    head = token.split("/", 1)[0]
    if head in REPO_PREFIXES:
        return [REPO_ROOT / token]
    if head in PACKAGE_PREFIXES:
        return [PACKAGE_ROOT / token]
    if head in PROJECT_PREFIXES:
        project = _owning_project(doc)
        return [project / token] if project is not None else []
    return []


def _module_docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def _documented_sources() -> list[tuple[Path, str]]:
    sources = [(path, path.read_text(encoding="utf-8")) for path in DOC_FILES]
    sources += [(path, _module_docstring(path)) for path in EXAMPLES.rglob("*.py")]
    return sources


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_examples_directory_is_not_empty() -> None:
    """A silently emptied examples/ would make every other check vacuous."""

    assert STANDALONE_EXAMPLES, "no standalone examples found under examples/"
    assert PROJECT_EXAMPLES, "no example projects (examples/*/a2a.yaml) found"


def test_every_standalone_example_declares_a_run() -> None:
    """A new example must state how it is exercised, or this fails."""

    declared = set(EXAMPLE_RUNS)
    found = {path.stem for path in STANDALONE_EXAMPLES}
    assert declared == found, (
        "EXAMPLE_RUNS is out of sync with examples/: "
        f"undeclared={sorted(found - declared)} stale={sorted(declared - found)}"
    )


@pytest.mark.parametrize(
    "path",
    sorted(p for p in EXAMPLES.rglob("*.py") if "__pycache__" not in p.parts),
    ids=lambda p: str(p.relative_to(EXAMPLES)),
)
def test_example_compiles(path: Path, tmp_path: Path) -> None:
    py_compile.compile(
        str(path), cfile=str(tmp_path / "out.pyc"), doraise=True
    )


@pytest.mark.parametrize(
    "path", STANDALONE_EXAMPLES, ids=lambda p: p.stem
)
def test_standalone_example_imports(path: Path) -> None:
    """Importing must not need a sandbox, a key, or a network call.

    Module-level code only: every example keeps its side effects inside
    ``main()`` behind ``if __name__ == "__main__"``.
    """

    name = f"_a2a_example_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# end-to-end runs
# ---------------------------------------------------------------------------


def _run_example(
    stem: str,
    args: tuple[str, ...] = (),
    *,
    env_extra: dict[str, str] | None = None,
    extra_pythonpath: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run `python -m examples.<stem>` the way the docs say to run it.

    Every ``A2A_*`` variable is dropped so a developer's platform signing keys
    or MinIO endpoint cannot make a broken example look healthy here.
    """

    env = {k: v for k, v in os.environ.items() if not k.startswith("A2A_")}
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE_ROOT), *extra_pythonpath])
    env["PYTHONUNBUFFERED"] = "1"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", f"examples.{stem}", *args],
        cwd=str(PACKAGE_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _unrelated_public_key() -> str:
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode("ascii")


@pytest.fixture(scope="session")
def example_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, subprocess.CompletedProcess[str]]:
    """Run every example scenario once, concurrently; tests read the results.

    Each run is a fresh interpreter importing the whole SDK, so serialising
    them would put ~20s on the suite. They touch nothing shared -- no cwd
    changes, no writes -- so a thread pool is safe and makes the guard cost
    roughly one run.
    """

    shim = tmp_path_factory.mktemp("broken_sandbox_runtime")
    (shim / "sandbox_runtime.py").write_text(
        "import a2a_definitely_not_installed\nLocalMicrosandboxClient = None\n",
        encoding="utf-8",
    )

    jobs: dict[str, dict] = {
        stem: {"stem": stem, "args": args} for stem, args in EXAMPLE_RUNS.items()
    }
    # A verifying key with no signing key beside it: the SDK prefers the env
    # var over the public half derived from the signing key.
    jobs["multi_agent:stale-verifying-key"] = {
        "stem": "multi_agent",
        "env_extra": {"A2A_GRANT_VERIFYING_KEY": _unrelated_public_key()},
    }
    # coder_agent with no sandbox_runtime at all, then with a broken one.
    jobs["coder_agent:no-sandbox"] = {"stem": "coder_agent"}
    jobs["coder_agent:broken-sandbox"] = {
        "stem": "coder_agent",
        "extra_pythonpath": (str(shim),),
    }

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {
            name: pool.submit(_run_example, **kwargs)
            for name, kwargs in jobs.items()
        }
        return {name: future.result() for name, future in futures.items()}


@pytest.mark.parametrize("stem", sorted(EXAMPLE_RUNS), ids=str)
def test_example_runs_offline(
    stem: str, example_runs: dict[str, subprocess.CompletedProcess[str]]
) -> None:
    """The documented invocation must exit 0 with no platform credentials.

    This is the check that catches the real class of example rot: an example
    whose ``main()`` needs an env var the docs never mention. Importing it is
    not enough -- the failure only shows up when the thing actually runs.
    """

    proc = example_runs[stem]
    assert proc.returncode == 0, (
        f"`python -m examples.{stem} {' '.join(EXAMPLE_RUNS[stem])}`.strip() "
        f"exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr


def test_multi_agent_run_seals_grant_and_receipts(
    example_runs: dict[str, subprocess.CompletedProcess[str]]
) -> None:
    """The delegation demo must really sign, not degrade to an error event."""

    out = example_runs["multi_agent"].stdout
    assert '"receipt_sealed"' in out, out
    assert '"replay_sealed"' in out, out
    assert "receipt_error" not in out, out
    # The whole point of the example: the callee never saw the denied file.
    assert "sales_q1.xlsx" in out and "secrets/.env is NOT in the list" in out
    files_used = out.split('"files_used": ')[1].split("\n")[0]
    assert "secrets" not in files_used, files_used


def test_multi_agent_run_with_a_stale_verifying_key(
    example_runs: dict[str, subprocess.CompletedProcess[str]]
) -> None:
    """A verifying key without its signing key must not break the demo.

    ``A2A_*_VERIFYING_KEY`` wins over the public half derived from the signing
    key, so minting a fresh signing key while leaving somebody else's
    verifying key in place used to fail with `grant signature mismatch`.
    """

    proc = example_runs["multi_agent:stale-verifying-key"]
    assert proc.returncode == 0, (
        f"exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    assert "signature mismatch" not in proc.stdout + proc.stderr
    # Silently overwriting the operator's env var would be worse than failing.
    assert "A2A_GRANT_VERIFYING_KEY was set" in proc.stderr, proc.stderr


def test_research_agent_run_names_the_files_it_could_see(
    example_runs: dict[str, subprocess.CompletedProcess[str]]
) -> None:
    """The docstring promises the allowed file names; printing a count is not that."""

    out = example_runs["research_agent"].stdout
    assert "reading src/quantum.py" in out, out
    assert "reading docs/intro.md" in out, out
    assert "reading secrets/.env" not in out, out


def test_coder_agent_without_a_sandbox_explains_itself(
    example_runs: dict[str, subprocess.CompletedProcess[str]]
) -> None:
    """The one example that cannot run offline must say so, not stack-trace."""

    proc = example_runs["coder_agent:no-sandbox"]
    assert proc.returncode != 0
    assert "cannot run offline" in proc.stderr, proc.stderr
    assert "--card" in proc.stderr, proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr


def test_coder_agent_surfaces_a_transitive_import_error(
    example_runs: dict[str, subprocess.CompletedProcess[str]]
) -> None:
    """`sandbox_runtime` installed but broken is a different problem.

    Catching every ModuleNotFoundError would tell the user to install a
    package they already have and hide the module that is actually missing.
    """

    proc = example_runs["coder_agent:broken-sandbox"]
    assert proc.returncode != 0
    assert "a2a_definitely_not_installed" in proc.stderr, proc.stderr
    assert "cannot run offline" not in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# example projects
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_import_state():
    """Load example projects without inheriting -- or leaking -- import state.

    ``load_agent_class`` prepends the project directory to ``sys.path`` only
    ``if path not in sys.path``, and imports the entrypoint as a top-level
    ``agent`` module. So a temp project directory another test left at
    ``sys.path[0]`` shadows this project's ``agent.py`` and the load resolves
    the wrong class. Sanitizing on the way *out* protects other tests from us;
    sanitizing on the way *in* is what makes this test order-independent.
    """

    saved_path = list(sys.path)
    saved_modules = dict(sys.modules)

    def prepare(project: Path, module_name: str) -> None:
        resolved = str(project.resolve())
        while resolved in sys.path:
            sys.path.remove(resolved)
        sys.path.insert(0, resolved)
        sys.modules.pop(module_name, None)

    try:
        yield prepare
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - set(saved_modules):
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def _entrypoint_module(project: Path) -> str:
    cfg = yaml.safe_load((project / "a2a.yaml").read_text(encoding="utf-8")) or {}
    entrypoint = str(cfg["entrypoint"]).strip()
    return entrypoint.split(":", 1)[0]


def _load_project(project: Path, prepare) -> object:
    from a2a_pack.cli.local import load_local_project

    prepare(project, _entrypoint_module(project))
    return load_local_project(project)


@pytest.mark.parametrize(
    "project", PROJECT_EXAMPLES, ids=lambda p: p.name
)
def test_example_project_loads(
    project: Path, clean_import_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", "sqlite:///:memory:")
    local = _load_project(project, clean_import_state)
    agent = local.agent_cls()
    assert agent.card().skills, f"{project.name} publishes no skills"


@pytest.mark.parametrize(
    "project", PROJECT_EXAMPLES, ids=lambda p: p.name
)
def test_example_project_loads_despite_a_shadowing_sys_path_entry(
    project: Path,
    clean_import_state,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Order-independence, pinned.

    Reproduces the exact mechanism behind `pytest tests/test_procurement_agent.py
    tests/test_openapi.py tests/test_examples.py`: an earlier load already put
    this project on ``sys.path``, so ``load_agent_class`` skips its
    ``if path not in sys.path`` insert -- and another project's directory,
    inserted at ``sys.path[0]`` since then, wins the ``import agent`` race.
    Both halves of the setup are load-bearing; drop either and the loader
    happens to do the right thing for the wrong reason.
    """

    module_name = _entrypoint_module(project)
    decoy = tmp_path / "decoy_project"
    decoy.mkdir()
    (decoy / f"{module_name}.py").write_text(
        "DECOY = True\n", encoding="utf-8"
    )
    sys.path.append(str(project.resolve()))  # an earlier load left it here
    sys.path.insert(0, str(decoy))  # a later one shadowed it
    importlib.import_module(module_name)  # and poisoned the import cache

    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", "sqlite:///:memory:")
    local = _load_project(project, clean_import_state)
    loaded_from = Path(sys.modules[module_name].__file__).resolve()
    assert loaded_from.is_relative_to(project.resolve()), loaded_from
    assert local.agent_cls().card().skills


# ---------------------------------------------------------------------------
# documentation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source", _documented_sources(), ids=lambda item: str(item[0].relative_to(PACKAGE_ROOT))
)
def test_documented_paths_exist(source: tuple[Path, str]) -> None:
    """Every repo path named in the docs must be a real path.

    This is the check that catches a moved directory: the SDK used to live at
    ``apps/a2a`` and every example still told readers to ``cd`` there.
    """

    doc, text = source
    missing = sorted(
        {
            token
            for token in _iter_path_tokens(text)
            if (candidates := _candidate_paths(doc, token))
            and not any(candidate.exists() for candidate in candidates)
        }
    )
    assert not missing, (
        f"{doc.relative_to(PACKAGE_ROOT)} references paths that do not exist: "
        f"{missing}"
    )


def test_documented_path_guard_is_not_vacuous() -> None:
    """The guard is only worth anything if it inspects real tokens.

    Both classes have been blind before: rooted tokens were checked while
    ``../../apps/sandbox-runtime`` and an example project's own
    ``frontend/src/...`` were silently skipped.
    """

    checked: list[str] = []
    relative: list[str] = []
    project_local: list[str] = []
    for doc, text in _documented_sources():
        for token in _iter_path_tokens(text):
            if not _candidate_paths(doc, token):
                continue
            checked.append(token)
            if token.startswith("."):
                relative.append(token)
            elif token.split("/", 1)[0] in PROJECT_PREFIXES:
                project_local.append(token)

    assert len(checked) >= 15, checked
    assert relative, "no relative (./ or ../) documented path is being checked"
    assert project_local, "no example-project-local documented path is checked"


@pytest.mark.parametrize(
    "source", _documented_sources(), ids=lambda item: str(item[0].relative_to(PACKAGE_ROOT))
)
def test_documented_example_modules_exist(source: tuple[Path, str]) -> None:
    """`python -m examples.foo` in the docs must name a file that exists."""

    doc, text = source
    missing = sorted(
        {
            dotted
            for dotted in _DASH_M_EXAMPLE.findall(text)
            if not (PACKAGE_ROOT / (dotted.replace(".", "/") + ".py")).exists()
        }
    )
    assert not missing, (
        f"{doc.relative_to(PACKAGE_ROOT)} runs example modules that do not "
        f"exist: {missing}"
    )
