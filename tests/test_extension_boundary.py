"""The boundary an out-of-tree tool package crosses, proved from both sides.

Two properties have to hold at once, and each is easy to lose without noticing.
An installed distribution must be able to add tools, write contracts and
executors and have them validated exactly as the built-in ones are. This
distribution must remain able to do its job with none installed, and must never
name, import or depend on one.

The end-to-end cases run in a subprocess against a synthetic distribution
written to disk with real ``.dist-info`` metadata, because the thing under test
is entry-point discovery -- a stub that hands the loader a fake entry point
would prove the stub works. The invariant cases construct registries directly,
where a failure can be arranged precisely and read without a subprocess.

Every fixture here is synthetic. Nothing in this file names a real distribution
other than this one.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

from moneybird_mcp._registration import (
    CORE_ORIGIN,
    Registration,
    Registry,
    RegistryError,
)
from moneybird_mcp.tools._extensions import ENTRY_POINT_GROUP
from moneybird_mcp.tools._validation import (
    RegistryValidationError,
    collect_problems,
    validate_registries,
)
from moneybird_mcp.write_contracts import WriteSpec

ROOT = pathlib.Path(__file__).resolve().parent.parent

DISTRIBUTION = "mb-boundary-canary"
VERSION = "1.2.3"
PACKAGE = "mb_boundary_canary"
TOOL_NAME = "canary_probe"
ACTION = "canary_action"

#: A complete extension: a read tool, a guarded write, nothing private.
#:
#: The guarded half is written the way a real one has to be -- staged, dispatched
#: between the two phase markers, verified, then run through the kernel. That is
#: the only way this file proves the seam is *sufficient* rather than merely
#: importable: an extension that could register an action but never execute one
#: would satisfy a registration-only test and fail on first use.
CANARY_MODULE = f'''
from moneybird_mcp.api import (
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    WriteSpec,
    get_client,
    mark_write_dispatch_started,
    mark_write_verifying,
    register_approval_executor,
    register_write_spec,
    run_approved_write,
    stage_write,
    tool,
)

APPLIED = []


@tool(annotations=PREPARE_ANNOTATIONS, tags={{"domain:canary"}})
def {TOOL_NAME}(fail: bool = False, resolve_client: bool = False) -> dict:
    """A synthetic tool that exists only to exercise the extension boundary."""
    if fail:
        raise MoneybirdError("canary refusal")
    if resolve_client:
        return {{"client": type(get_client()).__name__}}
    return {{"ok": True}}


@tool(annotations=PREPARE_ANNOTATIONS, tags={{"domain:canary"}})
def prepare_{ACTION}(note: str) -> dict:
    """Stage the synthetic write; nothing is applied until it is approved."""
    return stage_write(
        "{ACTION}",
        summary="Record the synthetic note " + repr(note),
        payload={{"note": note}},
        preview={{"note": note, "effect": "append one row in memory"}},
    )


def _apply_{ACTION}(client, payload):
    mark_write_dispatch_started()
    APPLIED.append(payload["note"])
    mark_write_verifying()
    return {{
        "recorded": payload["note"],
        "rows": len(APPLIED),
        "_audit_result": "success",
    }}


def {ACTION}_from_approval(approval_id):
    return run_approved_write(get_client(), approval_id, "{ACTION}", _apply_{ACTION})


register_write_spec(
    "{ACTION}",
    WriteSpec(1, "canary precondition", "canary verifier", "canary idempotency", "canary reconciliation"),
)
register_approval_executor("{ACTION}", {ACTION}_from_approval)
'''


def install_extension(
    root: pathlib.Path,
    module_source: str,
    *,
    distribution: str = DISTRIBUTION,
    version: str = VERSION,
    package: str = PACKAGE,
    entry_point_name: str = "canary",
    target: str | None = None,
) -> pathlib.Path:
    """Write an importable distribution with real entry-point metadata."""
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / package / "tools.py").write_text(module_source, encoding="utf-8")

    dist_info = root / f"{package}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        f"[{ENTRY_POINT_GROUP}]\n{entry_point_name} = {target or package + '.tools'}\n",
        encoding="utf-8",
    )
    return root


def run_with_extensions(
    script: str,
    *roots: pathlib.Path,
    capability_mode: str = "read_only",
) -> subprocess.CompletedProcess:
    """Run ``script`` in a fresh interpreter that can see the given distributions."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *(str(root) for root in roots)])
    env["MONEYBIRD_MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="moneybird_boundary_")
    env["MONEYBIRD_CAPABILITY_MODE"] = capability_mode
    # Pin the credential mode too. A developer whose shell selects
    # hosted_request_only would otherwise see the guarded-write probe refused
    # before the executor ran, and the failure would point at the boundary
    # rather than at the environment that caused it.
    env["MONEYBIRD_CREDENTIAL_MODE"] = "local"
    env.pop("MONEYBIRD_TOOL_DISCOVERY", None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=180,
    )


def payload_of(result: subprocess.CompletedProcess) -> dict:
    """The JSON a successful probe printed after its ``RESULT:`` marker."""
    for line in result.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:") :])
    raise AssertionError(
        f"probe printed no result\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def spec(**overrides) -> WriteSpec:
    fields = {
        "schema_version": 1,
        "precondition": "p",
        "verifier": "v",
        "idempotency": "i",
        "reconciliation": "r",
    }
    fields.update(overrides)
    return WriteSpec(**fields)


def registries(
    spec_entries: dict[str, tuple[WriteSpec, str]],
    executor_entries: dict[str, tuple[object, str]],
) -> tuple[Registry, Registry, Registry]:
    """Build the three registries directly, with the origins a test needs."""
    specs = Registry("write spec")
    for action, (value, origin) in spec_entries.items():
        specs.register(action, value, origin=origin)
    executors = Registry("approval executor")
    for action, (value, origin) in executor_entries.items():
        executors.register(action, value, origin=origin)
    return specs, executors, Registry("tool")


def executor(approval_id):  # a correctly shaped executor
    return {"approval_id": approval_id}


# --------------------------------------------------------------------------
# T1 / T2 -- this distribution on its own
# --------------------------------------------------------------------------


class CoreWithoutExtensionsTests(unittest.TestCase):
    """T1: with nothing installed, the surface is exactly this distribution's."""

    def test_every_registration_is_credited_to_this_distribution(self) -> None:
        from moneybird_mcp.tools._registry import TOOL_REGISTRY
        from moneybird_mcp.tools.approvals import APPROVAL_EXECUTOR_REGISTRY
        from moneybird_mcp.write_contracts import WRITE_SPEC_REGISTRY

        for registry in (TOOL_REGISTRY, WRITE_SPEC_REGISTRY, APPROVAL_EXECUTOR_REGISTRY):
            with self.subTest(registry=registry.subject):
                self.assertEqual(registry.origins(), (CORE_ORIGIN,))

    def test_specs_and_executors_agree_and_are_sealed(self) -> None:
        from moneybird_mcp.tools._registry import TOOL_REGISTRY
        from moneybird_mcp.tools.approvals import APPROVAL_EXECUTOR_REGISTRY
        from moneybird_mcp.write_contracts import WRITE_SPEC_REGISTRY

        self.assertEqual(set(WRITE_SPEC_REGISTRY), set(APPROVAL_EXECUTOR_REGISTRY))
        for registry in (TOOL_REGISTRY, WRITE_SPEC_REGISTRY, APPROVAL_EXECUTOR_REGISTRY):
            with self.subTest(registry=registry.subject):
                self.assertTrue(registry.frozen)

    def test_the_assembled_surface_has_no_outstanding_problems(self) -> None:
        self.assertEqual(collect_problems(), [])


class NoPrivateDependencyTests(unittest.TestCase):
    """T2: this distribution cannot name or import what it must not depend on."""

    #: Substrings that would mean the dependency arrow had been reversed. Written
    #: in pieces so this guard is not itself a hit for the scan it performs.
    FORBIDDEN = (
        "moneybird" + "_mcp_advanced",
        "moneybird" + "-mcp-advanced",
        "moneybird" + "_hosted",
        "moneybird" + "-hosted",
    )

    def source_files(self) -> list[pathlib.Path]:
        return sorted((ROOT / "moneybird_mcp").rglob("*.py"))

    def test_no_source_file_mentions_a_private_distribution(self) -> None:
        offenders = []
        for path in self.source_files():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                for forbidden in self.FORBIDDEN:
                    if forbidden in line:
                        offenders.append(f"{path.relative_to(ROOT)}:{number}: {forbidden}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_no_source_file_imports_anything_outside_the_allowed_set(self) -> None:
        """A textual scan misses an import assembled at runtime; the AST does not."""
        offenders = []
        for path in self.source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root_package = name.split(".", 1)[0]
                    if any(root_package.startswith(f.split(".")[0]) and root_package != "moneybird_mcp" for f in self.FORBIDDEN):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_loader_names_a_group_rather_than_a_package(self) -> None:
        self.assertEqual(ENTRY_POINT_GROUP, "moneybird_mcp.tools")


class PublicApiSurfaceTests(unittest.TestCase):
    """The seam an extension is allowed to depend on, pinned."""

    EXPECTED = (
        "API_VERSION",
        "ApprovalId",
        "MoneybirdError",
        "MoneybirdId",
        "PREPARE_ANNOTATIONS",
        "READ_ONLY_ANNOTATIONS",
        "Registration",
        "WRITE_ANNOTATIONS",
        "WriteSpec",
        "duplicate_fingerprint",
        "get_client",
        "mark_write_dispatch_started",
        "mark_write_verifying",
        "register_approval_executor",
        "register_write_spec",
        "run_approved_write",
        "stage_write",
        "tool",
    )

    def test_the_declared_surface_is_exactly_this(self) -> None:
        """Adding a name is a compatible change; removing or renaming one is not."""
        from moneybird_mcp import api

        self.assertEqual(tuple(sorted(api.__all__)), tuple(sorted(self.EXPECTED)))

    def test_every_declared_name_resolves(self) -> None:
        from moneybird_mcp import api

        for name in api.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(api, name))

    def test_registration_helpers_do_not_accept_a_caller_supplied_origin(self) -> None:
        """Provenance is the loader's to state, never the extension's to claim."""
        import inspect

        from moneybird_mcp import api

        for helper in (api.register_write_spec, api.register_approval_executor):
            with self.subTest(helper=helper.__name__):
                parameters = inspect.signature(helper).parameters
                self.assertNotIn("origin", parameters)
                self.assertNotIn("version", parameters)


# --------------------------------------------------------------------------
# Registry primitive
# --------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_a_second_registration_of_one_key_is_refused_with_both_origins(self) -> None:
        registry = Registry("write spec")
        registry.register("a", spec(), origin="one")
        with self.assertRaises(RegistryError) as caught:
            registry.register("a", spec(), origin="two")
        self.assertIn("one", str(caught.exception))
        self.assertIn("two", str(caught.exception))

    def test_registration_after_freeze_is_refused(self) -> None:
        registry = Registry("write spec")
        registry.freeze()
        with self.assertRaises(RegistryError):
            registry.register("a", spec(), origin="one")

    def test_provenance_is_recorded_and_readable(self) -> None:
        registry = Registry("tool")
        registration = registry.register("t", object(), origin="dist", version="4.5.6")
        self.assertIsInstance(registration, Registration)
        self.assertEqual(registry.origin_of("t"), "dist")
        self.assertEqual(registry.registration("t").version, "4.5.6")
        self.assertIn("dist 4.5.6", registry.registration("t").describe())

    def test_the_exposed_mapping_is_a_read_only_live_view(self) -> None:
        registry = Registry("write spec")
        view = registry.as_mapping()
        registry.register("a", spec(), origin="one")
        self.assertIn("a", view)
        with self.assertRaises(TypeError):
            view["b"] = spec()  # type: ignore[index]


# --------------------------------------------------------------------------
# The six validation invariants
# --------------------------------------------------------------------------


class ValidationInvariantTests(unittest.TestCase):
    def assert_refused(self, specs, executors, tools, *expected) -> str:
        with self.assertRaises(RegistryValidationError) as caught:
            validate_registries(specs, executors, tools)
        message = str(caught.exception)
        for fragment in expected:
            self.assertIn(fragment, message)
        for registry in (specs, executors, tools):
            self.assertFalse(registry.frozen, "a refused surface must not be sealed")
        return message

    def test_a_contract_without_an_executor_is_refused(self) -> None:
        specs, executors, tools = registries({"a": (spec(), "ext")}, {})
        self.assert_refused(specs, executors, tools, "'a'", "no executor", "ext")

    def test_an_executor_without_a_contract_is_refused(self) -> None:
        specs, executors, tools = registries({}, {"a": (executor, "ext")})
        self.assert_refused(specs, executors, tools, "'a'", "no write contract")

    def test_a_contract_and_executor_from_different_distributions_are_refused(self) -> None:
        specs, executors, tools = registries({"a": (spec(), "one")}, {"a": (executor, "two")})
        self.assert_refused(specs, executors, tools, "split across distributions", "one", "two")

    def test_an_incomplete_contract_is_refused(self) -> None:
        for field in ("precondition", "verifier", "idempotency", "reconciliation"):
            with self.subTest(field=field):
                specs, executors, tools = registries(
                    {"a": (spec(**{field: "   "}), "ext")}, {"a": (executor, "ext")}
                )
                self.assert_refused(specs, executors, tools, f"empty {field}")

    def test_an_unsupported_contract_schema_is_refused(self) -> None:
        specs, executors, tools = registries(
            {"a": (spec(schema_version=2), "ext")}, {"a": (executor, "ext")}
        )
        self.assert_refused(specs, executors, tools, "schema version 2")

    def test_an_executor_that_is_not_callable_is_refused(self) -> None:
        specs, executors, tools = registries({"a": (spec(), "ext")}, {"a": (object(), "ext")})
        self.assert_refused(specs, executors, tools, "not callable")

    def test_an_executor_with_the_wrong_signature_is_refused(self) -> None:
        def wrong(approval_id, extra):  # extra has no default: unsatisfiable
            return {}

        specs, executors, tools = registries({"a": (spec(), "ext")}, {"a": (wrong, "ext")})
        self.assert_refused(specs, executors, tools, "single approval id")

    def test_an_accepted_surface_is_sealed(self) -> None:
        specs, executors, tools = registries({"a": (spec(), "ext")}, {"a": (executor, "ext")})
        validate_registries(specs, executors, tools)
        for registry in (specs, executors, tools):
            with self.subTest(registry=registry.subject):
                self.assertTrue(registry.frozen)
        with self.assertRaises(RegistryError):
            specs.register("b", spec(), origin="ext")

    def test_the_diagnostic_names_actions_and_distributions_only(self) -> None:
        """A refusal is read from a server log; it must carry no payload."""
        secret = "approval-payload-that-must-not-appear"

        def leaky(approval_id, extra=secret):
            return {}

        specs, executors, tools = registries(
            {"a": (spec(precondition=" "), "ext")}, {"a": (leaky, "ext")}
        )
        message = self.assert_refused(specs, executors, tools, "empty precondition")
        self.assertNotIn(secret, message)


# --------------------------------------------------------------------------
# T3 and the integration pitfalls -- real distributions, real discovery
# --------------------------------------------------------------------------


PROBE = f'''
import asyncio, json
from moneybird_mcp import tools
from moneybird_mcp._registration import RegistryError
from moneybird_mcp.tools._registry import TOOL_REGISTRY, mcp
from moneybird_mcp.tools.approvals import APPROVAL_EXECUTOR_REGISTRY
from moneybird_mcp.write_contracts import WRITE_SPEC_REGISTRY

names = {{t.name for t in asyncio.run(mcp.list_tools())}}
try:
    WRITE_SPEC_REGISTRY.register("late_action", None)
    frozen_refusal = ""
except RegistryError as exc:
    frozen_refusal = str(exc)

print("RESULT:" + json.dumps({{
    "extensions": [
        {{"distribution": e.distribution, "version": e.version, "name": e.name, "value": e.value}}
        for e in tools.LOADED_EXTENSIONS
    ],
    "tool_registered": "{TOOL_NAME}" in TOOL_REGISTRY,
    "tool_origin": TOOL_REGISTRY.registration("{TOOL_NAME}").origin if "{TOOL_NAME}" in TOOL_REGISTRY else None,
    "tool_version": TOOL_REGISTRY.registration("{TOOL_NAME}").version if "{TOOL_NAME}" in TOOL_REGISTRY else None,
    "spec_registered": "{ACTION}" in WRITE_SPEC_REGISTRY,
    "spec_origin": WRITE_SPEC_REGISTRY.registration("{ACTION}").origin if "{ACTION}" in WRITE_SPEC_REGISTRY else None,
    "executor_registered": "{ACTION}" in APPROVAL_EXECUTOR_REGISTRY,
    "executor_origin": APPROVAL_EXECUTOR_REGISTRY.registration("{ACTION}").origin if "{ACTION}" in APPROVAL_EXECUTOR_REGISTRY else None,
    "tool_visible_over_mcp": "{TOOL_NAME}" in names,
    "core_tool_still_visible": "list_contacts" in names,
    "tool_count": len(TOOL_REGISTRY),
    "spec_count": len(WRITE_SPEC_REGISTRY),
    "frozen": [TOOL_REGISTRY.frozen, WRITE_SPEC_REGISTRY.frozen, APPROVAL_EXECUTOR_REGISTRY.frozen],
    "frozen_refusal": frozen_refusal,
    "spec_origins": list(WRITE_SPEC_REGISTRY.origins()),
}}))
'''


class ExtensionEndToEndTests(unittest.TestCase):
    """T3: a real installed distribution, discovered and validated end to end."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="moneybird_boundary_ext_")
        cls.root = install_extension(pathlib.Path(cls._tmp.name), CANARY_MODULE)
        cls.result = run_with_extensions(PROBE, cls.root)
        if cls.result.returncode != 0:
            raise AssertionError(
                f"probe failed\n--- stdout ---\n{cls.result.stdout}\n"
                f"--- stderr ---\n{cls.result.stderr}"
            )
        cls.payload = payload_of(cls.result)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_the_distribution_is_discovered_with_its_version(self) -> None:
        self.assertEqual(
            self.payload["extensions"],
            [
                {
                    "distribution": DISTRIBUTION,
                    "version": VERSION,
                    "name": "canary",
                    "value": f"{PACKAGE}.tools",
                }
            ],
        )

    def test_the_tool_is_registered_and_attributed_to_the_distribution(self) -> None:
        self.assertTrue(self.payload["tool_registered"])
        self.assertEqual(self.payload["tool_origin"], DISTRIBUTION)
        self.assertEqual(self.payload["tool_version"], VERSION)

    def test_the_contract_and_executor_are_registered_and_attributed(self) -> None:
        self.assertTrue(self.payload["spec_registered"])
        self.assertTrue(self.payload["executor_registered"])
        self.assertEqual(self.payload["spec_origin"], DISTRIBUTION)
        self.assertEqual(self.payload["executor_origin"], DISTRIBUTION)

    def test_the_extension_tool_is_served_over_mcp_beside_the_built_in_ones(self) -> None:
        self.assertTrue(self.payload["tool_visible_over_mcp"])
        self.assertTrue(self.payload["core_tool_still_visible"])

    def test_the_surface_grows_by_exactly_what_the_extension_added(self) -> None:
        self.assertEqual(self.payload["tool_count"], 63)
        self.assertEqual(self.payload["spec_count"], 27)
        self.assertEqual(self.payload["spec_origins"], [CORE_ORIGIN, DISTRIBUTION])

    def test_validation_sealed_every_registry(self) -> None:
        self.assertEqual(self.payload["frozen"], [True, True, True])
        self.assertIn("after validation sealed", self.payload["frozen_refusal"])


class ExtensionGuardedWriteTests(unittest.TestCase):
    """The seam has to be enough to run a guarded write, not just declare one.

    An extension that can register an action but cannot execute one would pass a
    registration-only test and fail the first time somebody approved something.
    So this drives the whole flow -- stage, approve, execute -- through the public
    dispatcher, using an extension that imports from ``moneybird_mcp.api`` and
    nothing else.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="moneybird_boundary_write_")
        self.addCleanup(self._tmp.cleanup)
        self.root = install_extension(pathlib.Path(self._tmp.name), CANARY_MODULE)

    def test_a_write_declared_through_the_seam_can_actually_be_executed(self) -> None:
        result = run_with_extensions(
            f'''
            import json
            from unittest import mock

            import moneybird_mcp.tools
            from moneybird_mcp.credentials import set_active_administration_id
            from moneybird_mcp.tools import _context
            from moneybird_mcp.tools.approvals import execute_approved_action

            from {PACKAGE}.tools import APPLIED, prepare_{ACTION}


            class SyntheticClient:
                administration_id = "100000000000000401"


            set_active_administration_id("100000000000000401")
            with mock.patch.object(_context, "get_client", return_value=SyntheticClient()):
                staged = prepare_{ACTION}("canary note")
                applied_before = list(APPLIED)
                executed = execute_approved_action(staged["approval_id"])
                try:
                    execute_approved_action(staged["approval_id"])
                    replay = ""
                except Exception as exc:
                    replay = str(exc)

            print("RESULT:" + json.dumps({{
                "staged_nothing": applied_before == [],
                "recorded": executed.get("recorded"),
                "rows": len(APPLIED),
                "status": executed.get("status"),
                "replay_refused": replay != "",
            }}))
            ''',
            self.root,
            capability_mode="write_enabled",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = payload_of(result)
        self.assertTrue(payload["staged_nothing"], "prepare must not apply anything")
        self.assertEqual(payload["recorded"], "canary note")
        self.assertEqual(payload["rows"], 1)
        self.assertEqual(payload["status"], "done")
        self.assertTrue(payload["replay_refused"], "a consumed approval must not run twice")


class ExtensionIntegrationSeamTests(unittest.TestCase):
    """The two ways an extension silently ends up outside the safety rails."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="moneybird_boundary_seam_")
        self.addCleanup(self._tmp.cleanup)
        self.root = install_extension(pathlib.Path(self._tmp.name), CANARY_MODULE)

    def test_an_extension_tool_gets_the_error_translating_registration(self) -> None:
        """Pitfall A: a raw MCP object would skip the refusal translation."""
        result = run_with_extensions(
            f'''
            import asyncio, json
            from fastmcp.exceptions import ToolError
            from moneybird_mcp import tools
            from moneybird_mcp.config import MoneybirdError
            from moneybird_mcp.tools._registry import mcp

            try:
                asyncio.run(mcp.call_tool("{TOOL_NAME}", {{"fail": True}}))
                raised = "nothing"
            except ToolError as exc:
                raised = "ToolError:" + str(exc)
            except MoneybirdError as exc:
                raised = "MoneybirdError:" + str(exc)
            print("RESULT:" + json.dumps({{"raised": raised}}))
            ''',
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        raised = payload_of(result)["raised"]
        self.assertTrue(raised.startswith("ToolError:"), raised)
        self.assertIn("canary refusal", raised)

    def test_importing_the_seam_before_the_tools_package_still_works(self) -> None:
        """The order an extension author reaches for first must not be the broken one.

        `import moneybird_mcp.api` is line one of any extension, and it is just as
        likely to be line one of the process. If the seam pulled the tools package
        in while executing, that import would load the installed extensions, whose
        own first line reaches back into this half-built module -- so the seam has
        to resolve everything behind that package on use instead of at import.
        """
        result = run_with_extensions(
            f'''
            import json

            import moneybird_mcp.api as api  # deliberately before moneybird_mcp.tools

            names = {{name: hasattr(api, name) for name in api.__all__}}
            import moneybird_mcp.tools  # noqa: E402 - the order is the point
            from moneybird_mcp.tools._registry import TOOL_REGISTRY

            print("RESULT:" + json.dumps({{
                "api_version": api.API_VERSION,
                "every_name_resolves": all(names.values()),
                "missing": sorted(n for n, ok in names.items() if not ok),
                "tool_is_callable": callable(api.tool),
                "extension_registered": "{TOOL_NAME}" in TOOL_REGISTRY,
            }}))
            ''',
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = payload_of(result)
        self.assertEqual(payload["missing"], [])
        self.assertTrue(payload["every_name_resolves"])
        self.assertTrue(payload["tool_is_callable"])
        self.assertTrue(payload["extension_registered"])

    def test_the_seam_imports_nothing_from_the_tools_package_at_module_level(self) -> None:
        """The property behind the test above, asserted where it can regress."""
        import ast

        source = (ROOT / "moneybird_mcp" / "api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in tree.body:  # module level only; function-local imports are fine
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tools"):
                offenders.append(f"api.py:{node.lineno}: from {node.module}")
            if isinstance(node, ast.ImportFrom) and node.level and (node.module or "").startswith("tools"):
                offenders.append(f"api.py:{node.lineno}: relative tools import")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_an_extension_resolves_its_client_through_the_supported_seam(self) -> None:
        """Pitfall B: a direct client would bypass credentials and the patch point.

        Patching ``_context.get_client`` is the one seam the whole suite redirects
        tools through. If it reaches an extension tool, that tool is inside the
        same credential-mode and administration-confinement rules as every
        built-in one; if it did not, the extension had built a client of its own.
        """
        result = run_with_extensions(
            f'''
            import asyncio, json
            from unittest import mock

            from moneybird_mcp import tools
            from moneybird_mcp.tools import _context
            from moneybird_mcp.tools._registry import mcp


            class SeamSentinel:
                """Stands in for the client the supported resolver would return."""


            with mock.patch.object(_context, "get_client", return_value=SeamSentinel()):
                call = mcp.call_tool("{TOOL_NAME}", {{"resolve_client": True}})
                answer = asyncio.run(call)

            resolved = answer.structured_content or answer.content
            print("RESULT:" + json.dumps({{"resolved": str(resolved)}}))
            ''',
            self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SeamSentinel", payload_of(result)["resolved"])


class BrokenExtensionTests(unittest.TestCase):
    """A failing extension must stop the server, not shrink it."""

    def _run_broken(self, module_source: str) -> subprocess.CompletedProcess:
        tmp = tempfile.TemporaryDirectory(prefix="moneybird_boundary_broken_")
        self.addCleanup(tmp.cleanup)
        root = install_extension(pathlib.Path(tmp.name), module_source)
        return run_with_extensions("import moneybird_mcp.tools\nprint('RESULT:{}')", root)

    def test_an_extension_that_fails_to_import_fails_the_server(self) -> None:
        result = self._run_broken("raise RuntimeError('canary import failure')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to load", result.stderr)
        self.assertIn(DISTRIBUTION, result.stderr)
        self.assertIn("canary import failure", result.stderr)

    def test_an_extension_claiming_an_existing_tool_name_fails_the_server(self) -> None:
        result = self._run_broken(
            "from moneybird_mcp.api import READ_ONLY_ANNOTATIONS, tool\n"
            "@tool(annotations=READ_ONLY_ANNOTATIONS)\n"
            "def list_contacts() -> dict:\n"
            "    return {}\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registered twice", result.stderr)
        self.assertIn("list_contacts", result.stderr)

    def test_an_extension_claiming_an_existing_action_fails_the_server(self) -> None:
        result = self._run_broken(
            "from moneybird_mcp.api import WriteSpec, register_write_spec\n"
            "register_write_spec('create_contact', WriteSpec(1, 'p', 'v', 'i', 'r'))\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registered twice", result.stderr)
        self.assertIn("create_contact", result.stderr)

    def test_an_extension_registering_a_contract_without_an_executor_fails(self) -> None:
        result = self._run_broken(
            "from moneybird_mcp.api import WriteSpec, register_write_spec\n"
            "register_write_spec('lonely_action', WriteSpec(1, 'p', 'v', 'i', 'r'))\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lonely_action", result.stderr)
        self.assertIn("no executor", result.stderr)

    def test_an_extension_registering_an_executor_without_a_contract_fails(self) -> None:
        result = self._run_broken(
            "from moneybird_mcp.api import register_approval_executor\n"
            "register_approval_executor('lonely_executor', lambda approval_id: {})\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lonely_executor", result.stderr)
        self.assertIn("no write contract", result.stderr)


if __name__ == "__main__":
    unittest.main()
