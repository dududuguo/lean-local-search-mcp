# Validation Notes

This repository was split out from the first MVP inside `HighDimProbLiebProvider`.

## Current Validation Targets

| Target | Mode | Indexed files | Lean declarations | Imports | Notes |
| --- | --- | ---: | ---: | ---: | --- |
| `C:\Users\11388\reserach\HighDimProbLiebProvider` | full | 258 | 513 | 341 | Provider repo indexed with tree-sitter in the standalone venv. |
| `C:\Users\11388\reserach\HighDimProbLiebProvider` | incremental | 258 | 513 | 341 | No-op reindex reported `changed_files: 0`, `skipped_files: 258`. |
| `C:\Users\11388\reserach\HighDimProb` | full | 712 | 2144 | 747 | Full main project indexed. Do not use only `HighDimProb\docs` when looking for Lean declarations. |
| `C:\Users\11388\reserach\HighDimProb` | incremental | 712 | 2144 | 747 | No-op reindex reported `changed_files: 0`, `skipped_files: 712`. |

## MCP Protocol Checks

Validated `tools/list` exposes short aliases without duplicate project entries:

```text
provider, HighDimProbLiebProvider -> C:\Users\11388\reserach\HighDimProbLiebProvider
highdimprob, HighDimProb, main -> C:\Users\11388\reserach\HighDimProb
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
