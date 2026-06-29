# Validation Notes

This repository was split out from the first MVP inside `HighDimProbLiebProvider`.

## Current Validation Targets

| Target | Mode | Indexed files | Lean declarations | Imports | Notes |
| --- | --- | ---: | ---: | ---: | --- |
| `C:\Users\11388\reserach\HighDimProbLiebProvider` | full | 258 | 513 | 341 | Provider repo indexed with tree-sitter in the standalone venv. |
| `C:\Users\11388\reserach\HighDimProbLiebProvider` | incremental | 258 | 513 | 341 | No-op reindex reported `changed_files: 0`, `skipped_files: 258`. |
| `C:\Users\11388\reserach\HighDimProb` | full | 712 | 2144 | 747 | Full main project indexed. Do not use only `HighDimProb\docs` when looking for Lean declarations. |
| `C:\Users\11388\reserach\HighDimProb` | incremental | 712 | 2144 | 747 | No-op reindex reported `changed_files: 0`, `skipped_files: 712`. |
| `C:\Users\11388\reserach\HighDimProbLiebProvider\.lake\packages\mathlib` | full | 8407 | 209309 | 1787 | Provider Mathlib checkout indexed through the `mathlib` alias. |
| `C:\Users\11388\reserach\HighDimProbLiebProvider\.lake\packages\mathlib` | incremental | 8407 | 209309 | 1787 | No-op reindex reported `changed_files: 0`, `skipped_files: 8407`. |

## MCP Protocol Checks

Validated `tools/list` exposes short aliases without duplicate project entries:

```text
provider, HighDimProbLiebProvider -> C:\Users\11388\reserach\HighDimProbLiebProvider
highdimprob, HighDimProb, main -> C:\Users\11388\reserach\HighDimProb
mathlib, Mathlib, provider-mathlib -> C:\Users\11388\reserach\HighDimProbLiebProvider\.lake\packages\mathlib
highdimprob-mathlib -> C:\Users\11388\reserach\HighDimProb\.lake\packages\mathlib
```

Validated `tools/list` exposes these post-MVP tools:

- `cache_status`
- `remove_project`
- `search_theorems`
- `get_context`

Validated theorem search:

```text
project: C-Users-11388-reserach-HighDimProb
query: Matrix Bernstein
limit: 1
first theorem: HighDimProb.matrixBernsteinTraceMGFWithBernsteinCoeff_negRandomMatrixFamily
total theorem-like matches: 964
```

Validated `get_context`:

```text
project: C-Users-11388-reserach-HighDimProb
qualified_name: HighDimProb.matrixBernsteinTraceMGF_under_tropp
conclusion: matrixBernsteinTraceMGFWithBernsteinCoeff_statement P A theta R
includes: imports, local_header, theorem_profile, neighbors, source, source_context
```

Validated Mathlib theorem search and context:

```text
project: mathlib
query: eigenvalues_mem_spectrum_real
first theorem: Matrix.IsHermitian.eigenvalues_mem_spectrum_real
file: Mathlib/Analysis/Matrix/Spectrum.lean:81
conclusion: hA.eigenvalues i in spectrum R A
get_context includes: local_header, theorem_profile, neighbors, source, source_context
```

Validated cache removal in the same MCP process:

```text
repo_path: C:\tmp\lean-local-search-mcp-cache-test
index_repository: declarations=1
remove_project: removed=true
```

Validated cache status:

```text
project: C-Users-11388-reserach-HighDimProb
schema_version: 2
status: ready
is_stale: false
```


Validated API visibility tools:

```text
index_repository project=mathlib topic=Matrix mode=resume: changed_files=0, skipped_files=95
index_status project=mathlib topic=Matrix details=true: current_scope_files=95, indexed_scope_files=95
search_shape project=mathlib shape="Matrix.trace (cfc f A) = _": strict hit count 0, relaxed fallback enabled
proof_probe #check Matrix.IsHermitian.eigenvalues_mem_spectrum_real: ok=true, import=Mathlib.Analysis.Matrix.Spectrum
index_visibility project=provider: tracked_only=false, indexed_untracked_files=4, root=HighDimProbLiebProvider.lean
cross_repo_lookup trace_eq_sum_eigenvalues: found Mathlib declaration and HighDimProb textual users
consumer_fit epsteinAffineLineConcavity_of_cfcLog_hasDerivAt_traceDerivative_nonpos: found provider and HighDimProb shape-near candidates
```

Validated fuzz runner:

```text
python tools/fuzz_mcp.py --iterations 300 --seed 1700001: failures=[]
python tools/fuzz_mcp.py --iterations 80 --seed 1700002 --include-existing: failures=[]
```

Validated scanner-first parser refactor:

```text
internal imports / namespace / end / identifiers / typeclass brackets now use small scanners
remaining regex use is limited to tree-sitter-unavailable declaration fallback and explicit user pattern APIs
python -m py_compile tools/lean_local_search_mcp.py tools/fuzz_mcp.py: ok
python tools/fuzz_mcp.py --iterations 500 --seed 1700101: failures=[]
python tools/fuzz_mcp.py --iterations 120 --seed 1700102 --include-existing: failures=[]
protocol checks: theorem_card, search_shape, proof_probe, index_visibility all returned ok-shaped results
```
## Bugs Fixed During Validation

- `remove_project` initially failed on Windows because the same MCP process still held an open SQLite connection after indexing. Index/status/project lookup code now closes SQLite connections before returning.
- `split_decl` initially treated named arguments such as `(P := P)` as the declaration body delimiter. Declaration splitting now searches for `:=` or `where` only at top level.
- `get_context` initially treated `before: 0`, `after: 0`, and `neighbor_radius: 0` as defaults because of truthy `or` handling. Zero is now respected.
- `list_projects` initially duplicated the default Provider project when its cache database was also present. Project listing is now de-duplicated by root path.
- `search_theorems` initially over-weighted qualified-name/file-path matches. It now ranks short declaration-name matches first for local APIs such as `matrixBernstein...`.

## Original MVP Fixes

- include docs/text files for `search_code`, not only `.lean`;
- store external target caches under the MCP repository's `.lean-local-search` directory;
- rank declaration name and qualified-name matches ahead of proof/source-only matches;
- keep counting raw text matches after the returned result limit is reached;
- force UTF-8 stdout/stderr for Windows Lean Unicode output;
- respect `context: 0` in `search_code`.
