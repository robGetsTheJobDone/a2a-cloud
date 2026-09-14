"""`a2a deploy` must never change an agent's registry listing by accident.

The control plane's ``from-tarball`` route takes ``public`` as a form field it
writes onto the agent row on every upload, and that field defaults to True. So
the CLI decides the listing, and the two ways to get it wrong are symmetric:

- assume "public" when a2a.yaml says nothing, and a practice agent lands in the
  platform public registry;
- assume "private" when a2a.yaml says nothing, and a redeploy silently unlists
  an agent someone published on purpose.

These tests pin the third answer: an absent ``expose.public`` is *unspecified*,
so the CLI reuses the listing the agent already has and only defaults to
unlisted for an agent the registry has never seen. Explicit flags win, explicit
manifest wins over the lookup, and every deploy says out loud what it did.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from a2a_pack.cli import main as cli_main
from a2a_pack.cli.api_client import ApiError
from a2a_pack.cli.credentials import Credentials
from a2a_pack.cli.main import app

runner = CliRunner()


def _flat(text: str) -> str:
    """Rich hard-wraps at the console width; match on flattened output."""
    return " ".join(text.replace("│", " ").split())


class FakeClient:
    """Control plane that knows about zero or one agent named ``demo``."""

    def __init__(
        self,
        *,
        agent: dict[str, Any] | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self.agent = agent
        self.lookup_error = lookup_error
        self.get_calls: list[str] = []
        self.tarball_kwargs: dict[str, Any] | None = None

    def get_agent(self, name: str) -> dict[str, Any]:
        self.get_calls.append(name)
        if self.lookup_error is not None:
            raise self.lookup_error
        if self.agent is None:
            raise ApiError(404, "agent not found")
        return self.agent

    def from_tarball(self, **kwargs: Any) -> dict[str, Any]:
        self.tarball_kwargs = kwargs
        return {
            "name": "demo",
            "version": "0.1.0",
            "status": "building",
            "url": "https://demo.a2acloud.io",
            "head_sha": "abc123",
            "deployment_id": "dep-1",
        }


@pytest.fixture
def _logged_in(monkeypatch):
    creds = Credentials(
        api_url="https://api.example.test", token="token", email="dev@example.test"
    )
    monkeypatch.setattr(cli_main.credentials, "load", lambda: creds)


def _stub_project(monkeypatch, client: FakeClient, cfg: dict[str, Any]) -> None:
    class Dsl:
        name = "demo"
        version = "0.1.0"
        description = "demo"

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            return {"name": self.name}

    monkeypatch.setattr(
        cli_main,
        "_compile_project_dsl",
        lambda project_dir: ({"entrypoint": "agent:Demo", **cfg}, Dsl()),
    )
    monkeypatch.setattr(cli_main, "_make_tarball", lambda project_dir, agent_dsl: b"tgz")
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)


def _deploy(tmp_path, *args: str):
    return runner.invoke(app, ["deploy", str(tmp_path), "--no-wait", *args])


# --- explicit declarations --------------------------------------------------


def test_expose_public_true_lists_the_agent(tmp_path, monkeypatch, _logged_in):
    client = FakeClient()
    _stub_project(monkeypatch, client, {"expose": {"public": True}})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is True
    # A declared value is the answer; no need to ask the control plane.
    assert client.get_calls == []
    flat = _flat(result.output)
    assert "listed in the public registry at " in flat
    assert "expose.public: true" in flat


def test_expose_public_false_unlists_the_agent(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {"expose": {"public": False}})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is False
    assert client.get_calls == []
    flat = _flat(result.output)
    assert "kept out of the public registry" in flat
    assert "expose.public: false" in flat


# --- absent key -------------------------------------------------------------


def test_absent_expose_on_a_new_agent_does_not_publish(tmp_path, monkeypatch, _logged_in):
    """The bug this file exists for: an omitted key is not consent to publish."""
    client = FakeClient(agent=None)  # 404: the registry has never seen it
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is False
    assert client.get_calls == ["demo"]
    flat = _flat(result.output)
    assert "kept out of the public registry" in flat
    assert "not in the registry yet" in flat
    assert "a2a deploy --public" in flat


def test_absent_expose_keeps_an_already_listed_agent_listed(
    tmp_path, monkeypatch, _logged_in
):
    """The regression that matters: no silent unlisting on redeploy.

    A user who published an agent before `expose` existed in their a2a.yaml must
    not lose the listing just because they ran `a2a deploy` again.
    """
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is True
    assert client.get_calls == ["demo"]
    flat = _flat(result.output)
    assert "listed in the public registry at " in flat
    assert "kept the listing the agent already had" in flat


def test_absent_expose_keeps_an_unlisted_agent_unlisted(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(agent={"name": "demo", "public": False})
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is False
    flat = _flat(result.output)
    assert "kept out of the public registry" in flat
    assert "kept the listing the agent already had" in flat


def test_expose_without_a_public_key_is_still_unspecified(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {"expose": {"port": 8080}})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is True
    assert client.get_calls == ["demo"]


def test_a_name_owned_by_someone_else_is_treated_as_new(tmp_path, monkeypatch, _logged_in):
    """403 on lookup: not ours to list. The upload fails its own access check."""
    client = FakeClient(lookup_error=ApiError(403, "agent action denied"))
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is False


def test_an_unreadable_listing_stops_the_deploy_instead_of_guessing(
    tmp_path, monkeypatch, _logged_in
):
    """A 5xx is the one case where the flags actually unblock the deploy.

    The lookup route is down; the upload route may well not be, and either flag
    removes the need for the lookup entirely.
    """
    client = FakeClient(lookup_error=ApiError(500, "internal server error"))
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 1, result.output
    assert client.tarball_kwargs is None  # nothing was uploaded
    flat = _flat(result.output)
    assert "could not read the current registry listing" in flat
    assert "a2a deploy --public" in flat
    assert "a2a deploy --private" in flat


def test_a_transport_failure_also_stops_the_deploy(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(lookup_error=RuntimeError("connection refused"))
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 1, result.output
    assert client.tarball_kwargs is None
    assert "connection refused" in _flat(result.output)


def test_an_unreachable_control_plane_does_not_advertise_the_flags(
    tmp_path, monkeypatch, _logged_in
):
    """Advice that cannot work is worse than no advice.

    If the control plane is unreachable, re-running with `--public` walks
    straight into the same failure one step later, at the upload. Say what is
    actually wrong instead.
    """
    client = FakeClient(lookup_error=RuntimeError("connection refused"))
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "control plane could not be reached" in flat
    assert "a2a deploy --public" not in flat
    assert "a2a deploy --private" not in flat


def test_an_expired_session_points_at_login_not_at_the_flags(
    tmp_path, monkeypatch, _logged_in
):
    """401 on the lookup means the upload would 401 too; a flag fixes nothing."""
    client = FakeClient(lookup_error=ApiError(401, "not authenticated"))
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path)

    assert result.exit_code == 1, result.output
    assert client.tarball_kwargs is None
    flat = _flat(result.output)
    assert "Your session is not valid" in flat
    assert "a2a login" in flat
    assert "a2a deploy --public" not in flat
    assert "a2a deploy --private" not in flat


# --- ambiguous declarations -------------------------------------------------


def test_a_quoted_false_is_rejected_rather_than_read_as_true(
    tmp_path, monkeypatch, _logged_in
):
    """`public: "false"` is a non-empty string; bool() would PUBLISH the agent.

    Worse, the CLI would then print "a2a.yaml sets `expose.public: true`" about
    a file that says the opposite. Refuse instead of announcing a lie.
    """
    client = FakeClient(agent=None)
    _stub_project(monkeypatch, client, {"expose": {"public": "false"}})

    result = _deploy(tmp_path)

    assert result.exit_code == 1, result.output
    assert client.tarball_kwargs is None  # nothing was uploaded
    assert client.get_calls == []
    flat = _flat(result.output)
    assert "`expose.public` must be true or false" in flat
    assert "'false'" in flat  # the offending literal, quoted back
    assert "expose.public: true" not in flat


@pytest.mark.parametrize("value", ["no", "yes", "true", 1, 0, [], {"a": 1}])
def test_every_non_boolean_expose_public_is_rejected(
    tmp_path, monkeypatch, _logged_in, value
):
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {"expose": {"public": value}})

    result = _deploy(tmp_path)

    assert result.exit_code == 1, result.output
    assert client.tarball_kwargs is None
    assert "must be true or false" in _flat(result.output)


def test_a_valueless_public_key_means_unspecified(tmp_path, monkeypatch, _logged_in):
    """`public:` with nothing after it is YAML null - no value was written.

    Reading it as False would unlist an already-listed agent on the strength of
    a typo, so it falls through to the current listing like an absent key.
    """
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {"expose": {"public": None}})

    result = _deploy(tmp_path)

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is True
    assert client.get_calls == ["demo"]
    assert "kept the listing the agent already had" in _flat(result.output)


# --- flags win --------------------------------------------------------------


def test_public_flag_overrides_a_private_manifest(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(agent={"name": "demo", "public": False})
    _stub_project(monkeypatch, client, {"expose": {"public": False}})

    result = _deploy(tmp_path, "--public")

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is True
    assert client.get_calls == []
    flat = _flat(result.output)
    assert "listed in the public registry" in flat
    assert "you passed `--public`" in flat


def test_private_flag_overrides_a_public_manifest(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {"expose": {"public": True}})

    result = _deploy(tmp_path, "--private")

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is False
    flat = _flat(result.output)
    assert "kept out of the public registry" in flat
    assert "you passed `--private`" in flat


def test_private_flag_overrides_the_current_listing(tmp_path, monkeypatch, _logged_in):
    """An explicit flag is a decision, so it does not need the lookup at all."""
    client = FakeClient(agent={"name": "demo", "public": True})
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path, "--private")

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is False
    assert client.get_calls == []


def test_public_flag_publishes_an_agent_with_no_manifest_key(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(agent=None)
    _stub_project(monkeypatch, client, {})

    result = _deploy(tmp_path, "--public")

    assert result.exit_code == 0, result.output
    assert client.tarball_kwargs["public"] is True
    assert client.get_calls == []


# --- the pure decision ------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "declared", "registered", "expected"),
    [
        (True, False, False, (True, "flag")),
        (False, True, True, (False, "flag")),
        (None, True, False, (True, "manifest")),
        (None, False, True, (False, "manifest")),
        (None, None, True, (True, "unchanged")),
        (None, None, False, (False, "unchanged")),
        (None, None, None, (False, "new")),
    ],
)
def test_resolve_listing_precedence(flag, declared, registered, expected):
    assert (
        cli_main._resolve_listing(
            flag=flag, declared=declared, registered=registered
        )
        == expected
    )


def test_nothing_declared_anywhere_means_unlisted():
    """The single value the a2a.yaml reference documents as the default.

    `web/apps/docs/scripts/gen.py` pins this branch of `_resolve_listing` to the
    `expose.public` Default cell, so flipping it here without updating the docs
    is meant to fail the docs gate. Pinning it here too means the flip is caught
    by the SDK suite as well, not only by a docs job in another package.
    """
    assert cli_main._resolve_listing(
        flag=None, declared=None, registered=None
    ) == (False, "new")


def test_declared_listing_reads_only_an_explicit_key():
    assert cli_main._declared_listing({"expose": {"public": True}}) is True
    assert cli_main._declared_listing({"expose": {"public": False}}) is False
    assert cli_main._declared_listing({"expose": {}}) is None
    assert cli_main._declared_listing({"expose": None}) is None
    assert cli_main._declared_listing({}) is None
    # YAML null: the key is there but no value was written.
    assert cli_main._declared_listing({"expose": {"public": None}}) is None


@pytest.mark.parametrize("value", ["false", "true", "no", "on", 1, 0, 1.0, [], {}])
def test_declared_listing_refuses_to_coerce_a_non_boolean(value):
    with pytest.raises(cli_main.ListingDeclarationError, match="must be true or false"):
        cli_main._declared_listing({"expose": {"public": value}})


def test_declared_listing_accepts_yaml_boolean_spellings():
    """`yes`/`no`/`on`/`off` are real YAML 1.1 booleans, not strings."""
    import yaml

    assert cli_main._declared_listing(yaml.safe_load("expose:\n  public: yes\n")) is True
    assert cli_main._declared_listing(yaml.safe_load("expose:\n  public: no\n")) is False
    assert cli_main._declared_listing(yaml.safe_load("expose:\n  public: on\n")) is True
    assert cli_main._declared_listing(yaml.safe_load("expose:\n  public:\n")) is None
    # ...but the quoted forms are strings, and strings are refused.
    with pytest.raises(cli_main.ListingDeclarationError):
        cli_main._declared_listing(yaml.safe_load('expose:\n  public: "no"\n'))


# --- lookup failure messages ------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected", "forbidden"),
    [
        (ApiError(401, "not authenticated"), "a2a login", "a2a deploy --public"),
        (ApiError(500, "boom"), "a2a deploy --public", "a2a login"),
        (ApiError(502, "bad gateway"), "a2a deploy --private", "a2a login"),
        (RuntimeError("connection refused"), "could not be reached", "a2a deploy --public"),
    ],
)
def test_listing_lookup_failure_only_offers_advice_that_can_work(
    exc, expected, forbidden
):
    message = cli_main._listing_lookup_failure("demo", exc)
    assert expected in message
    assert forbidden not in message


# --- `a2a openapi generate` -------------------------------------------------
#
# `POST /v1/agents/from-openapi` writes `public` straight onto the agent row,
# and the CLI never sends `refresh_existing`, so this command only ever creates
# agents. Creating one is not consent to publish it.


class FakeOpenAPIClient:
    def __init__(self) -> None:
        self.generate_kwargs: dict[str, Any] | None = None
        self.preview_kwargs: dict[str, Any] | None = None

    def from_openapi(self, **kwargs: Any) -> dict[str, Any]:
        self.generate_kwargs = kwargs
        return {
            "name": "petstore",
            "version": "0.1.0",
            "status": "building",
            "repo_url": "https://git.example.test/petstore",
            "expected_url": None,
            "deployment_id": "dep-1",
            "preview": {"operation_count": 3},
        }

    def preview_openapi_agent(self, **kwargs: Any) -> dict[str, Any]:
        self.preview_kwargs = kwargs
        return {"name": "petstore", "operation_count": 3}


@pytest.fixture
def _openapi_client(monkeypatch, _logged_in):
    client = FakeOpenAPIClient()
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)
    return client


def test_openapi_generate_does_not_publish_by_default(_openapi_client):
    result = runner.invoke(app, ["openapi", "generate", "https://spec.test/openapi.json"])

    assert result.exit_code == 0, result.output
    assert _openapi_client.generate_kwargs["public"] is False
    flat = _flat(result.output)
    assert "kept out of the public registry" in flat
    assert "a generated agent starts unlisted" in flat
    assert "a2a openapi generate --public" in flat


def test_openapi_generate_publishes_only_when_asked(_openapi_client):
    result = runner.invoke(
        app, ["openapi", "generate", "https://spec.test/openapi.json", "--public"]
    )

    assert result.exit_code == 0, result.output
    assert _openapi_client.generate_kwargs["public"] is True
    flat = _flat(result.output)
    assert "listed in the public registry" in flat
    assert "you passed `--public`" in flat


def test_openapi_generate_never_claims_a_flag_you_did_not_pass(_openapi_client):
    """`--private` and the default agree on the value but not on the reason."""
    result = runner.invoke(
        app, ["openapi", "generate", "https://spec.test/openapi.json", "--private"]
    )

    assert result.exit_code == 0, result.output
    assert _openapi_client.generate_kwargs["public"] is False
    assert "you passed `--private`" in _flat(result.output)


def test_openapi_preview_mirrors_the_generate_default(_openapi_client):
    result = runner.invoke(app, ["openapi", "preview", "https://spec.test/openapi.json"])

    assert result.exit_code == 0, result.output
    assert _openapi_client.preview_kwargs["public"] is False


def test_the_client_library_never_publishes_by_omission():
    """A caller that says nothing about listing must not get a public agent.

    The CLI always passes `public` explicitly, so these defaults only bind
    non-CLI callers of `ControlPlaneClient` — exactly the callers least likely
    to have thought about the registry.
    """
    import inspect

    from a2a_pack.cli.api_client import ControlPlaneClient

    for method in ("from_openapi", "preview_openapi_agent"):
        parameter = inspect.signature(
            getattr(ControlPlaneClient, method)
        ).parameters["public"]
        assert parameter.default is False, method


# --- `a2a local-deploy` -----------------------------------------------------


def test_local_deploy_states_its_listing_outcome(monkeypatch, _logged_in, tmp_path):
    """The local path resolves the listing the same way, so it must say so too."""
    from a2a_pack.cli import local_harness

    result_obj = local_harness.LocalDeployResult(
        agent="demo",
        version="0.1.0",
        status="building",
        url="http://demo.localhost",
        head_sha=None,
        deployment_id=None,
        api_url="http://localhost:8000",
        owner_email="local@example.com",
        tarball_bytes=3,
        listing_public=True,
        listing_why="unchanged",
    )
    monkeypatch.setattr(
        local_harness, "deploy_local_agent", lambda *a, **kw: result_obj
    )

    result = runner.invoke(app, ["local-deploy", str(tmp_path)])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "listed in the public registry at " in flat
    assert "kept the listing the agent already had" in flat
    assert "a2a local-deploy --private" in flat


def test_local_deploy_json_carries_the_listing_decision(
    monkeypatch, _logged_in, tmp_path
):
    from a2a_pack.cli import local_harness

    result_obj = local_harness.LocalDeployResult(
        agent="demo",
        version="0.1.0",
        status="building",
        url=None,
        head_sha=None,
        deployment_id=None,
        api_url="http://localhost:8000",
        owner_email="local@example.com",
        tarball_bytes=3,
        listing_public=False,
        listing_why="new",
    )
    monkeypatch.setattr(
        local_harness, "deploy_local_agent", lambda *a, **kw: result_obj
    )

    result = runner.invoke(app, ["local-deploy", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["listing_public"] is False
    assert payload["listing_why"] == "new"
