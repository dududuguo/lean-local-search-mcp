# lean-local-search-mcp

No-model local MCP search for Lean repositories. It uses `tree-sitter-lean` plus SQLite FTS for fast local indexing and syntactic theorem search.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the `tree_sitter_lean` binding is missing, rebuild or install the local `tree-sitter-lean` package into this venv.

## Codex MCP

```powershell
codex mcp add lean-local-search -- <mcp-repo>\.venv\Scripts\python.exe <mcp-repo>\tools\lean_local_search_mcp.py --repo <lean-project>
codex mcp get lean-local-search
```

The server also recognizes project aliases such as `provider`, `highdimprob`, `main`, and `mathlib` when those sibling repositories exist next to this repo.

## Main Tools

- `index_repository`: incremental/resumable indexing with `topic`, `path_prefix`, `path_filter`, and `batch_size`.
- `index_visibility`: tracked/untracked status, stale files, and root-import exposure.
- `search_theorems` / `search_shape`: theorem search by name, symbols, conclusion, and type shape.
- `theorem_card`: statement, import, namespace, source location, typeclass hints, and minimal `#check`.
- `proof_probe`: writes a temporary Lean file and runs `lake env lean`; compact output by default, `verbose: true` for full printing.
- `consumer_fit` / `cross_repo_lookup`: provider/main/Mathlib API visibility and migration checks.
- `search_graph`, `search_code`, `get_context`, `cache_status`, `remove_project`.

## Typical Calls

```json
{"name":"index_repository","arguments":{"project":"provider","mode":"full"}}
{"name":"index_repository","arguments":{"project":"mathlib","topic":"Matrix","mode":"resume"}}
{"name":"search_theorems","arguments":{"project":"mathlib","query":"cfc_eq eigenvalues diagonal IsHermitian","limit":5,"cards":true}}
{"name":"search_shape","arguments":{"project":"mathlib","shape":"Matrix.trace (cfc f A) = _","limit":5}}
{"name":"theorem_card","arguments":{"project":"mathlib","qualified_name":"Matrix.IsHermitian.trace_eq_sum_eigenvalues"}}
{"name":"proof_probe","arguments":{"project":"mathlib","run_project":"provider","checks":["Matrix.IsHermitian.trace_eq_sum_eigenvalues"]}}
{"name":"index_visibility","arguments":{"project":"provider","details":true,"detail_limit":10}}
```

## Validation

```powershell
.\.venv\Scripts\python.exe -m py_compile tools\lean_local_search_mcp.py tools\fuzz_mcp.py
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 300 --seed 1700201
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 80 --seed 1700202 --include-existing
```
