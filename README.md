# lean-local-search-mcp

No-model MCP search for local Lean repositories. It indexes Lean declarations into SQLite/FTS5 and adds lightweight theorem-aware search helpers for names, statements, imports, and parser diagnostics.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the `tree_sitter_lean` binding is missing, rebuild or install a compatible `tree-sitter-lean` package into this venv.

## Codex MCP

```powershell
codex mcp add lean-local-search -- <mcp-repo>\.venv\Scripts\python.exe <mcp-repo>\tools\lean_local_search_mcp.py --repo <lean-project>
codex mcp get lean-local-search
```

Use `repo_path` for explicit repositories. If you configure project aliases, the same tools also accept `project`.

## Main Tools

- `index_repository`: incremental and resumable indexing with path filters and batch sizes.
- `index_status`, `index_visibility`, `cache_status`, `remove_project`: cache, stale-file, and root-import visibility checks.
- `search_graph`, `search_code`: declaration and source search.
- `search_theorems`, `search_shape`: theorem search by name, symbols, conclusion, premises, and syntactic type shape.
- `theorem_card`, `get_context`, `get_code_snippet`: proof-oriented declaration context.
- `proof_probe`: writes a temporary Lean file and runs `lake env lean`; compact output by default.
- `consumer_fit`, `cross_repo_lookup`: heuristic API matching across indexed repositories.
- `debug_parse_file`: compare scanner, tree-sitter, and regex declaration ranges for one Lean file.

## Typical Calls

```json
{"name":"index_repository","arguments":{"repo_path":"<lean-project>","mode":"incremental"}}
{"name":"index_repository","arguments":{"repo_path":"<large-lean-project>","path_prefix":"Mathlib/LinearAlgebra","mode":"resume","batch_size":200}}
{"name":"search_graph","arguments":{"repo_path":"<lean-project>","name_pattern":".*trace.*","limit":10}}
{"name":"search_theorems","arguments":{"repo_path":"<lean-project>","query":"trace cfc eigenvalues","limit":5,"cards":true}}
{"name":"search_shape","arguments":{"repo_path":"<lean-project>","shape":"Matrix.trace (cfc f A) = _","limit":5}}
{"name":"theorem_card","arguments":{"repo_path":"<lean-project>","qualified_name":"<Namespace.theorem_name>"}}
{"name":"proof_probe","arguments":{"repo_path":"<lean-project>","checks":["<Namespace.theorem_name>"]}}
{"name":"debug_parse_file","arguments":{"repo_path":"<lean-project>","file":"<path/to/File.lean>","pattern":"<decl_name>","max_errors":12}}
```

## Validation

```powershell
.\.venv\Scripts\python.exe -m py_compile tools\lean_local_search_mcp.py tools\fuzz_mcp.py
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 300 --seed 1700201
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 80 --seed 1700202 --include-existing
```

## License

MIT. See `LICENSE`.
