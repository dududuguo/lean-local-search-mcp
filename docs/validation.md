# Validation Notes

This repository was split out from the first MVP inside `HighDimProbLiebProvider`.

Validated local targets:

| Target | Indexed files | Lean declarations | Imports | Notes |
| --- | ---: | ---: | ---: | --- |
| `C:\Users\11388\reserach\HighDimProbLiebProvider` | 257 | 513 | 341 | Provider Lean declarations and docs indexed. |
| `C:\Users\11388\reserach\HighDimProb` | 712 | 2144 | 747 | Full main project indexed. Do not use only `HighDimProb\docs` when looking for Lean declarations. |

Validated declaration search:

```text
project: C-Users-11388-reserach-HighDimProb
query: Matrix Bernstein
limit: 5
first declaration: HighDimProb.matrixBernsteinTwoSidedOptimizedScalarTailRHS
total declaration matches: 845
```

Validated raw text search:

```text
project: C-Users-11388-reserach-HighDimProb
pattern: Matrix Bernstein
limit: 3
context: 1
total_grep_matches: 539
total_results: 539
```

MVP fixes made during validation:

- include docs/text files for `search_code`, not only `.lean`;
- store external target caches under the MCP repository's `.lean-local-search` directory;
- rank declaration name and qualified-name matches ahead of proof/source-only matches;
- keep counting raw text matches after the returned result limit is reached;
- force UTF-8 stdout/stderr for Windows Lean Unicode output;
- respect `context: 0` in `search_code`.