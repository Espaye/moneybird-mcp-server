"""Static fail-closed invariants for the manual release workflow."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _trigger_block(text: str) -> str:
    start = text.index("on:\n")
    end = text.index("\npermissions:", start)
    return text[start:end]


def test_release_is_manual_only_and_requires_exact_inputs() -> None:
    text = _workflow_text()
    trigger = _trigger_block(text)

    assert "push:" not in trigger
    assert "workflow_dispatch:" in trigger
    assert "version:" in trigger
    assert "commit_sha:" in trigger
    assert trigger.count("required: true") == 2
    assert "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}" in text
    assert "REQUESTED_VERSION: ${{ inputs.version }}" in text
    assert "REQUESTED_COMMIT_SHA: ${{ inputs.commit_sha }}" in text
    assert "requested_commit != os.environ[\"COMMIT_SHA\"]" in text
    assert "requested_version != version" in text


def test_release_refuses_existing_immutable_state() -> None:
    text = _workflow_text()

    assert 'if version in project.get("releases", {}):' in text
    assert "Tag v{version} already exists" in text
    assert "A GitHub Release for v{version} already exists" in text
    assert "refusing to overwrite, repair, or reuse it" in text
    assert "gh release upload" not in text
    assert "--clobber" not in text


def test_release_uses_exact_artifacts_and_trusted_publishing() -> None:
    text = _workflow_text()

    assert "ref: ${{ needs.check.outputs.source_sha }}" in text
    assert "candidate-dist-${{ needs.check.outputs.version }}" in text
    assert "pypi-dist-${{ needs.check.outputs.version }}" in text
    assert "actions/upload-artifact@" in text
    assert "actions/download-artifact@" in text
    assert "name: pypi" in text
    assert "id-token: write" in text
    assert "pypa/gh-action-pypi-publish@" in text
    assert "attestations: true" in text
    assert "TWINE_PASSWORD" not in text
    assert "password:" not in text


def test_release_reverifies_tag_and_published_artifacts() -> None:
    text = _workflow_text()

    assert text.count("SOURCE_SHA: ${{ needs.check.outputs.source_sha }}") >= 5
    assert "Re-verify tag immediately before PyPI publication" in text
    assert "Re-verify tag after any publication approval delay" in text
    assert "Candidate/PyPI digest mismatch" in text
    assert "pypi-attestations" in text
    assert "Verify final tag and GitHub release artifacts" in text
