#!/usr/bin/env python
"""Small crash fuzzer for lean_local_search_mcp.py.

Default mode builds a synthetic Lean-ish repo under the system temp directory,
indexes it, then randomly calls MCP tool implementations directly. It avoids
remove_project/proof_probe by default so it is safe to run often.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().with_name("lean_local_search_mcp.py")

try:
    from .validate_release import prepare_fixture, validate_release
except ImportError:
    from validate_release import prepare_fixture, validate_release


def load_server():
    spec = importlib.util.spec_from_file_location("lean_local_search_mcp_under_fuzz", SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SERVER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root: Path) -> None:
    write_file(
        root / "FuzzRepo.lean",
        """import FuzzRepo.Basic
import FuzzRepo.Matrix
""",
    )
    write_file(
        root / "FuzzRepo" / "Basic.lean",
        """namespace Fuzz

def idFun (x : Nat) : Nat := x

def someLongCamelCaseDeclarationName (x : Nat) : Nat := x + 1

theorem add_zero_shape (n : Nat) : n + 0 = n := by
  exact Nat.add_zero n

lemma has_deriv_shape (f : Real -> Real) (x y : Real) : HasDerivAt f y x -> HasDerivAt f y x := by
  intro h
  exact h

namespace Nested

section DecidableEq
variable [DecidableEq Nat]
end DecidableEq

theorem nested_after_section (n : Nat) : n = n := by
  rfl

end Nested

/-- A first theorem with a longer calc proof. -/
theorem calc_range_first (n : Nat) : n = n := by
  calc
    n = n := rfl

/-- A second theorem immediately after a doc comment must be a separate declaration. -/
theorem calc_range_second (n : Nat) : n = n := by
  rfl

end Fuzz
""",
    )
    write_file(
        root / "FuzzRepo" / "Matrix.lean",
        """import FuzzRepo.Basic

namespace Fuzz

opaque MatrixLike : Type
opaque cfc : (Real -> Real) -> MatrixLike -> MatrixLike
opaque trace : MatrixLike -> Real
opaque A : MatrixLike
opaque f : Real -> Real

theorem trace_cfc_shape : trace (cfc f A) = trace (cfc f A) := by
  rfl

lemma inv_affine_shape (g : Real -> MatrixLike) (t : Real) : HasDerivAt (fun s : Real => g s) A t -> HasDerivAt (fun s : Real => g s) A t := by
  intro h
  exact h

end Fuzz
""",
    )
    write_file(
        root / "FuzzRepo" / "Wide.lean",
        "namespace Fuzz\n\n"
        + "\n".join(
            f"def relativeEntropyLiebConcavity_{i} : Nat := {i}"
            for i in range(260)
        )
        + "\n\nend Fuzz\n",
    )
    write_file(root / "docs" / "notes.md", "trace cfc HasDerivAt Matrix random text\n")


def rand_text(rng: random.Random, max_len: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "_ .:/\\-*+()[]{}'\"≤≥∈→⁻¹•"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))


def maybe(rng: random.Random, choices):
    return rng.choice(choices)


def random_args(rng: random.Random, tool: str, repo: Path, existing: bool, existing_repo: Path | None = None):
    names = [
        "Fuzz.trace_cfc_shape",
        "trace_cfc_shape",
        "Fuzz.add_zero_shape",
        "add_zero_shape",
        "Fuzz.has_deriv_shape",
        "missing_name",
        "",
        rand_text(rng),
    ]
    shapes = [
        "trace (cfc f A) = _",
        "HasDerivAt (fun t => g t) A t",
        "n + 0 = n",
        "Matrix.trace (cfc f A) = _",
        rand_text(rng, 40),
        "",
    ]
    patterns = ["trace", "cfc", "HasDerivAt", "Matrix", "", rand_text(rng, 16)]
    scopes = [{}, {"path_prefix": "FuzzRepo"}, {"file_pattern": "*.lean"}, {"path_filter": "Matrix|Basic"}]
    target_repo = existing_repo if existing and existing_repo is not None else repo
    base_project = {"repo_path": str(target_repo)}
    if tool == "index_repository":
        args = {"repo_path": str(target_repo), "mode": rng.choice(["incremental", "resume", "full"]), "batch_size": rng.randint(1, 20)}
        args.update(rng.choice(scopes))
        return args
    if tool == "index_status":
        args = dict(base_project); args.update({"details": rng.choice([True, False]), "detail_limit": rng.randint(1, 10)}); args.update(rng.choice(scopes)); return args
    if tool == "index_visibility":
        args = dict(base_project); args.update({"details": True, "detail_limit": rng.randint(1, 10)}); return args
    if tool == "debug_parse_file":
        if existing:
            return {"repo_path": str(target_repo), "file": "HighDimProb/Concentration/Markov.lean", "pattern": "markov_inequality", "max_errors": 5, "decl_limit": 5, "hit_limit": 3}
        return {"repo_path": str(repo), "file": rng.choice(["FuzzRepo/Basic.lean", "FuzzRepo/Matrix.lean"]), "pattern": rng.choice(["calc_range_second", "trace_cfc_shape", "missing_name"]), "max_errors": 5, "decl_limit": 5, "hit_limit": 3}
    if tool == "cache_status":
        return dict(base_project) if rng.random() < 0.7 else {}
    if tool == "search_graph":
        args = dict(base_project); args.update({"query": rng.choice(patterns), "limit": rng.randint(1, 8), "offset": rng.randint(0, 3), "label": rng.choice([None, "Function", "Type", "theorem"])}); args.update(rng.choice(scopes)); return {k:v for k,v in args.items() if v is not None}
    if tool == "search_theorems":
        args = dict(base_project); args.update({"query": rng.choice(patterns), "limit": rng.randint(1, 8), "cards": rng.choice([True, False]), "include_source": rng.choice([True, False])}); args.update(rng.choice(scopes)); return args
    if tool == "search_shape":
        args = dict(base_project); args.update({"shape": rng.choice(shapes), "limit": rng.randint(1, 8), "cards": rng.choice([True, False]), "strict": rng.choice([True, False])}); args.update(rng.choice(scopes)); return args
    if tool == "theorem_card":
        args = dict(base_project); args.update({"qualified_name": rng.choice(names), "include_source": rng.choice([True, False])}); return args
    if tool in {"get_context", "get_code_snippet"}:
        args = dict(base_project); args.update({"qualified_name": rng.choice(names), "before": rng.randint(0, 5), "after": rng.randint(0, 5), "neighbor_radius": rng.randint(0, 3), "include_neighbors": rng.choice([True, False])}); return args
    if tool == "search_code":
        args = dict(base_project); args.update({"pattern": rng.choice(patterns), "limit": rng.randint(1, 8), "context": rng.randint(0, 3), "mode": rng.choice(["compact", "files", "full"]), "regex": False}); args.update(rng.choice(scopes)); return args
    if tool == "trace_path":
        args = dict(base_project); args.update({"qualified_name": rng.choice(names), "direction": rng.choice(["inbound", "outbound", "both", "weird"]), "depth": rng.randint(0, 3)}); return args
    if tool == "get_architecture":
        return dict(base_project)
    if tool == "project_templates":
        return {}
    if tool == "cross_repo_lookup":
        return {"query": rng.choice(names), "projects": ["default"], "limit": rng.randint(1, 5)}
    if tool == "consumer_fit":
        return {"project": "default", "consumer": rng.choice(["Fuzz.trace_cfc_shape", "trace_cfc_shape", "missing_name"]), "projects": ["default"], "max_obligations": 2, "candidates_per_obligation": 3}
    return {}


def assert_jsonable(value):
    json.dumps(value, ensure_ascii=False)


def stdio_request(proc, message):
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise RuntimeError(
            f"stdio server closed before response: exit={proc.poll()}, stderr={stderr}"
        )
    response = json.loads(line)
    assert "error" not in response, response
    return response


def assert_stdio_pagination_survives(repo: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH), "--repo", str(repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        init = stdio_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        assert "result" in init, init
        for request_id, offset in ((2, 0), (3, 20)):
            response = stdio_request(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "search_graph",
                        "arguments": {
                            "repo_path": str(repo),
                            "query": "relative entropy Lieb concavity",
                            "limit": 20,
                            "offset": offset,
                        },
                    },
                },
            )
            payload = json.loads(response["result"]["content"][0]["text"])
            assert payload["total"] >= 260 and payload["has_more"], payload
            assert len(payload["results"]) == 20, payload
            assert proc.poll() is None
        response = stdio_request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "index_repository",
                    "arguments": {
                        "repo_path": str(repo),
                        "mode": "resume",
                        "path_prefix": "FuzzRepo",
                    },
                },
            },
        )
        assert "result" in response and proc.poll() is None, response
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=10)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        assert proc.returncode == 0, stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=300)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--include-existing", action="store_true", help="Also validate and fuzz the pinned HighDimProb alpha-0.1 fixture.")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()
    seed = args.seed if args.seed is not None else random.randrange(1 << 32)
    rng = random.Random(seed)
    temp_root = Path(tempfile.mkdtemp(prefix="lean-local-search-fuzz-"))
    previous_index_root = os.environ.get("LEAN_SEARCH_INDEX_ROOT")
    os.environ["LEAN_SEARCH_INDEX_ROOT"] = str(temp_root / ".indexes")
    mod = load_server()
    existing_repo = None
    failures = []
    tools = [
        "index_repository", "index_status", "index_visibility", "debug_parse_file", "cache_status", "search_graph",
        "search_theorems", "search_shape", "theorem_card", "get_context", "get_code_snippet",
        "search_code", "trace_path", "get_architecture", "project_templates", "cross_repo_lookup",
        "consumer_fit",
    ]
    try:
        if args.include_existing:
            existing_repo, fixture_action = prepare_fixture()
            release_summary = validate_release(existing_repo, index_dir=temp_root / ".indexes")
            fixture_status = mod.index_status({"repo_path": str(existing_repo)}, existing_repo)
            assert fixture_status["files"] == 342 and fixture_status["declarations"] == 2560, fixture_status
            print(f"Pinned release fixture {fixture_action}: {existing_repo}", file=sys.stderr)
            print(f"Pinned release checks: {', '.join(release_summary['checks'])}", file=sys.stderr)
        make_repo(temp_root)
        initial = mod.index_repository({"mode": "full", "batch_size": 5}, temp_root)
        assert_jsonable(initial)
        assert_stdio_pagination_survives(temp_root)
        con = mod.connect(temp_root)
        try:
            nested = con.execute("SELECT qn FROM decls WHERE name=?", ("nested_after_section",)).fetchone()
            assert nested is not None and nested["qn"] == "Fuzz.Nested.nested_after_section", nested["qn"] if nested else None
            second = con.execute("SELECT qn FROM decls WHERE name=?", ("calc_range_second",)).fetchone()
            assert second is not None and second["qn"] == "Fuzz.calc_range_second", second["qn"] if second else None
            first_src = con.execute("SELECT src FROM decls WHERE name=?", ("calc_range_first",)).fetchone()["src"]
            assert "calc_range_second" not in first_src, first_src
            scoped = mod.index_repository({"mode": "resume", "paths": ["FuzzRepo/Basic.lean", "FuzzRepo/Matrix.lean"], "batch_size": 5}, temp_root)
            assert scoped["scope_indexed_files"] == 2, scoped
            assert scoped["total_indexed_files"] >= scoped["scope_indexed_files"], scoped
            assert scoped["indexed_files"] == scoped["total_indexed_files"], scoped
            card = mod.theorem_card_tool({"repo_path": str(temp_root), "qualified_name": "Fuzz.add_zero_shape"}, temp_root)["card"]
            assert card["kind"] == "theorem" and card["label"] == "Theorem", card
            code_hit = mod.search_code({"repo_path": str(temp_root), "pattern": "add_zero_shape", "limit": 1}, temp_root)["results"][0]
            assert code_hit["qualified_name"] == "Fuzz.add_zero_shape" and code_hit["label"] == "Theorem", code_hit
            camel_qn = "Fuzz.someLongCamelCaseDeclarationName"
            exact_code = mod.get_code_snippet({"repo_path": str(temp_root), "qualified_name": camel_qn}, temp_root)
            assert exact_code["found"] and exact_code["qualified_name"] == camel_qn, exact_code
            for pattern in ["someLongCamelCase", "%someLongCamelCase%", "*someLongCamelCase*"]:
                hits = mod.search_graph({"repo_path": str(temp_root), "name_pattern": pattern, "limit": 10}, temp_root)["results"]
                assert any(hit["qualified_name"] == camel_qn for hit in hits), (pattern, hits)
            for query in ["CamelCaseDeclaration", "Long Camel Case Declaration"]:
                hits = mod.search_graph({"repo_path": str(temp_root), "query": query, "limit": 10}, temp_root)["results"]
                assert any(hit["qualified_name"] == camel_qn for hit in hits), (query, hits)
            try:
                mod.search_graph({"repo_path": str(temp_root), "name_pattern": "[bad", "limit": 1}, temp_root)
            except ValueError as exc:
                assert "name_pattern uses regex syntax" in str(exc), exc
            else:
                raise AssertionError("invalid name_pattern should raise a friendly ValueError")

            project_b = temp_root / "ProjectB"
            make_repo(project_b)
            mod.index_repository({"mode": "full", "batch_size": 5}, project_b)
            project_b_name = mod.project_name(project_b)
            captured = {}
            original_run = mod.subprocess.run

            class FakeCompleted:
                returncode = 1
                stdout = ""
                stderr = "error: unknown module prefix 'FuzzRepo'\nNo directory 'FuzzRepo' in the search path entries:\n  DefaultRepo/.lake"

            def fake_run(cmd, cwd, **kwargs):
                captured["cmd"] = cmd
                captured["cwd"] = cwd
                captured["kwargs"] = kwargs
                return FakeCompleted()

            try:
                mod.subprocess.run = fake_run
                probe = mod.proof_probe({"project": project_b_name, "imports": ["FuzzRepo.Basic"], "code": "#check Fuzz.idFun", "verbose": True}, temp_root)
            finally:
                mod.subprocess.run = original_run
            assert Path(captured["cwd"]).resolve() == project_b.resolve(), (captured, probe)
            assert Path(probe["search_repo"]).resolve() == project_b.resolve(), probe
            assert Path(probe["run_repo"]).resolve() == project_b.resolve(), probe
            assert probe["run_project"] == project_b_name and probe["execution_defaulted_to_search_project"], probe
            assert "missing_import_or_unknown_name" in probe["diagnosis"], probe
            assert "import_error_context" in probe and str(project_b) in probe["import_error_context"], probe
        finally:
            con.close()
        for i in range(args.iterations):
            existing = args.include_existing and rng.random() < 0.25
            tool = rng.choice([t for t in tools if existing or t != "index_repository"])
            if tool in {"cross_repo_lookup", "consumer_fit"}:
                existing = False
            call_args = random_args(rng, tool, temp_root, existing, existing_repo)
            try:
                result = mod.call_tool(tool, call_args, temp_root)
                assert_jsonable(result)
            except Exception as exc:  # noqa: BLE001 - fuzzer records all crashes
                failures.append({
                    "iteration": i,
                    "tool": tool,
                    "args": call_args,
                    "exception": repr(exc),
                    "traceback": traceback.format_exc(),
                })
                if len(failures) >= 10:
                    break
        summary = {"seed": seed, "iterations": args.iterations, "temp_repo": str(temp_root), "failures": failures}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if failures else 0
    finally:
        if previous_index_root is None:
            os.environ.pop("LEAN_SEARCH_INDEX_ROOT", None)
        else:
            os.environ["LEAN_SEARCH_INDEX_ROOT"] = previous_index_root
        if not args.keep_temp and not failures:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
