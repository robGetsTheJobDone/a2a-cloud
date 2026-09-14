"""Self-test for the hand-written `a2a.yaml` reference gate.

`check_manifest_reference` and `check_effective_defaults` are the only things
standing between `content/reference/a2a-yaml.md` and silent rot: that page is
hand-written, so nothing regenerates it when the code under it moves. This
verifies the gate actually fires, by reintroducing each mistake a review has
already caught once and asserting the gate rejects it.

The doc regressions run against the *real* source tree, so they prove the
assertion holds for the code as it exists now rather than for a fixture.

Run:

    python web/apps/docs/scripts/gen_selftest.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen  # noqa: E402

# `check_manifest_reference` imports `a2a_pack`; `gen.generate` is what
# normally puts the monorepo SDK ahead of any installed copy. Do it here too,
# so the gate is tested against the SDK in this checkout.
sys.path.insert(0, str(gen.DEFAULT_SDK_PATH))

MANIFEST_REFERENCE = gen.DEFAULT_OUT / "a2a-yaml.md"

# (label, text in the current page, text to put back, substring of the expected
# complaint). Each is a wrong default a reviewer found in `a2a-yaml.md`.
DOC_REGRESSIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "`expose.public` sold as access control",
        "A listing choice, **not access control**.",
        "Unlisted agents get no public URL.",
        "must say 'not access control'",
    ),
    (
        "`expose.public` defaulting to listed",
        "| `public` | bool | `False` |",
        "| `public` | bool | `true` |",
        # The complaint quotes the source literal, so it is `False`, not `false`.
        # Documenting the old `true` default is the dangerous regression now:
        # it would tell users that deleting the block republishes the agent.
        "must say 'False'",
    ),
    (
        "an empty mailbox allowlist documented as open",
        "`[]` (owner only)",
        "`[]` (anyone)",
        "must say 'owner only'",
    ),
    (
        "the mailbox row dropping its default-deny warning",
        "**default-deny**",
        "a plain allowlist",
        "must say 'default-deny'",
    ),
    (
        "the mapping shorthand for `databases` that the control plane rejects",
        "Must be a list, even for one database",
        "A single mapping is read as a one-item list",
        "'one-item list'",
    ),
    (
        "the memory namespace defaulting to the agent name",
        "| `namespace` | string | `notes` |",
        "| `namespace` | string | agent name |",
        "must say 'notes'",
    ),
    (
        "empty memory tiers documented as no tiers",
        "| `tiers` | string \\| list | `files` |",
        "| `tiers` | string \\| list | `[]` |",
        "must say 'files'",
    ),
    (
        "the SDK concurrency default passed off as the hosted one",
        "| `concurrency` | integer | `1` card / `100` hosted |",
        "| `concurrency` | integer | `1` |",
        "must say '100'",
    ),
)


def _fail(label: str, detail: str) -> str:
    return f"{label}: {detail}"


def check_doc_regressions() -> list[str]:
    """Each known-wrong default must be rejected against the real sources."""

    source = MANIFEST_REFERENCE.read_text()
    issues: list[str] = []
    with tempfile.TemporaryDirectory(prefix="a2a-docs-selftest-") as tmp:
        page = Path(tmp) / "a2a-yaml.md"
        for label, current, regressed, expected in DOC_REGRESSIONS:
            occurrences = source.count(current)
            if occurrences != 1:
                issues.append(
                    _fail(
                        label,
                        f"{current!r} appears {occurrences} times in "
                        f"{MANIFEST_REFERENCE.name} (expected 1) — the page was "
                        "reworded, so update this self-test",
                    )
                )
                continue
            page.write_text(source.replace(current, regressed))
            reported = gen.check_effective_defaults(page)
            if not any(expected in issue for issue in reported):
                issues.append(
                    _fail(
                        label,
                        f"reintroducing it was not rejected with {expected!r}; "
                        f"got {reported or 'no issues at all'}",
                    )
                )
    return issues


def check_source_pins() -> list[str]:
    """Every pin must fail when the expression it watches stops existing.

    Without this the pins could silently match nothing and the gate would pass
    on a doc that no code backs any more.
    """

    issues: list[str] = []
    with tempfile.TemporaryDirectory(prefix="a2a-docs-selftest-src-") as tmp:
        root = Path(tmp)
        # Mirror every pinned source first, so the only failure a mutation can
        # produce is the one it was meant to produce.
        for pin in gen.DEFAULT_PINS:
            real = gen.MONOREPO_ROOT / pin.source
            if not real.is_file():
                issues.append(_fail(pin.key, f"pinned source {pin.source} is missing"))
                continue
            copy = root / pin.source
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(real, copy)
        if issues:
            return issues
        if gen.check_effective_defaults(MANIFEST_REFERENCE, root=root):
            return [_fail("mirrored sources", "the unmutated mirror already fails")]

        for pin in gen.DEFAULT_PINS:
            copy = root / pin.source
            pristine = copy.read_text()
            copy.write_text(re.sub(pin.pattern, "", pristine))
            reported = gen.check_effective_defaults(MANIFEST_REFERENCE, root=root)
            # `decides` identifies the pin: two pins share `memory.py`.
            if not any(
                "matched 0 times" in issue and pin.decides in issue
                for issue in reported
            ):
                issues.append(
                    _fail(
                        pin.key,
                        f"deleting the pinned expression from {pin.source} was "
                        f"not reported; got {reported or 'no issues at all'}",
                    )
                )
            copy.write_text(pristine)
    return issues


def check_manifest_keys() -> list[str]:
    """The key contract must reject both a missing key and an invented one."""

    source = MANIFEST_REFERENCE.read_text()
    cases = (
        (
            "a model field that stopped being documented",
            "| `scale_to_zero` | bool | `true` | Let the database suspend when idle. |\n",
            "",
            "does not document AgentDatabase.scale_to_zero",
        ),
        (
            "a documented key no model parses",
            "| `scale_to_zero` | bool | `true` |",
            "| `allow_everything` | bool | `true` |",
            "'allow_everything', which is not a AgentDatabase field",
        ),
    )
    issues: list[str] = []
    with tempfile.TemporaryDirectory(prefix="a2a-docs-selftest-keys-") as tmp:
        page = Path(tmp) / "a2a-yaml.md"
        for label, current, replacement, expected in cases:
            if source.count(current) != 1:
                issues.append(
                    _fail(label, f"{current!r} is not uniquely present; update this test")
                )
                continue
            page.write_text(source.replace(current, replacement))
            reported = gen.check_manifest_reference(page)
            if not any(expected in issue for issue in reported):
                issues.append(
                    _fail(
                        label,
                        f"was not rejected with {expected!r}; "
                        f"got {reported or 'no issues at all'}",
                    )
                )
    return issues


def check_remediation_hints() -> list[str]:
    """A failure must only suggest the fix that can actually clear it."""

    issues: list[str] = []
    generated_only = gen._remediation_hints(["stale generated file: cli.md"], [])
    if generated_only != [gen.REGENERATE_HINT]:
        issues.append(_fail("stale generated page", f"hinted {generated_only}"))

    hand_only = gen._remediation_hints([], ["a2a-yaml.md: undocumented field"])
    if hand_only != [gen.HAND_EDIT_HINT]:
        issues.append(
            _fail(
                "hand-written page out of date",
                f"hinted {hand_only} — regenerating cannot fix a2a-yaml.md",
            )
        )

    both = gen._remediation_hints(["stale"], ["undocumented"])
    if both != [gen.REGENERATE_HINT, gen.HAND_EDIT_HINT]:
        issues.append(_fail("both failure classes", f"hinted {both}"))

    if gen._remediation_hints([], []):
        issues.append(_fail("no failures", "hinted a fix anyway"))
    return issues


def main() -> int:
    issues = [
        *check_doc_regressions(),
        *check_source_pins(),
        *check_manifest_keys(),
        *check_remediation_hints(),
    ]
    if issues:
        print("\n".join(f"error: {issue}" for issue in issues), file=sys.stderr)
        return 1
    print("a2a.yaml reference gate rejects every known-wrong default and key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
