# lean-local-search-mcp

No-model local MCP search for Lean repos. It uses `tree-sitter-lean` plus SQLite FTS; it does not use embeddings, LLMs, Lake, or Lean elaboration.

This repo is tuned for my local projects:

- `provider` / `HighDimProbLiebProvider` -> `C:\Users\11388\reserach\HighDimProbLiebProvider`
- `highdimprob` / `HighDimProb` / `main` -> `C:\Users\11388\reserach\HighDimProb`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On this Windows machine, the local `tree-sitter-lean` wheel may miss `_binding.pyd`. If import fails, rebuild the binding in `.venv` from `C:\Users\11388\reserach\tree-sitter-lean`.

## Codex MCP

```powershell
codex mcp add lean-local-search -- C:\Users\11388\reserach\lean-local-search-mcp\.venv\Scripts\python.exe C:\Users\11388\reserach\lean-local-search-mcp\tools\lean_local_search_mcp.py --repo C:\Users\11388\reserach\HighDimProbLiebProvider
codex mcp get lean-local-search
```

## Main Tools

- `index_repository`: incremental index; pass `mode: "full"` to rebuild.
- `cache_status`: cache size, schema, stale status.
- `remove_project`: delete one cache by `project` or `repo_path`.
- `search_theorems`: theorem-like search over `theorem`, `lemma`, and `abbrev` names/conclusions/premises/symbols.
- `search_graph`: general declaration search.
- `search_code`: raw source/doc search.
- `get_context`: imports, local header, theorem profile, neighbors, source context.

## Typical Calls

```json
{"name":"index_repository","arguments":{"project":"highdimprob","mode":"full"}}
{"name":"index_repository","arguments":{"project":"provider","mode":"full"}}
{"name":"search_theorems","arguments":{"project":"highdimprob","query":"Matrix Bernstein","limit":5}}
{"name":"get_context","arguments":{"project":"highdimprob","qualified_name":"HighDimProb.matrixBernsteinTraceMGF_under_tropp","before":10,"after":10,"neighbor_radius":2}}
{"name":"search_code","arguments":{"project":"provider","pattern":"lambdaMaxOrdered","limit":5,"context":1}}
```

## Notes

- Re-index after edits; incremental indexing skips unchanged files by path, mtime, and size.
- `search_theorems` is syntactic and heuristic, but works well for the local theorem-wrapper style.
- A search hit is not a proof check. Use `lake build` for validation.
