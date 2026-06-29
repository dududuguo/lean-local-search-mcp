# lean-local-search-mcp

No-model local MCP search for Lean repos. It uses `tree-sitter-lean` plus SQLite FTS; it does not use embeddings, LLMs, Lake, or Lean elaboration.

This repo is tuned for my local projects:

- `provider` / `HighDimProbLiebProvider` -> `C:\Users\11388\reserach\HighDimProbLiebProvider`
- `highdimprob` / `HighDimProb` / `main` -> `C:\Users\11388\reserach\HighDimProb`
- `mathlib` / `Mathlib` -> Provider's `.lake/packages/mathlib`
- `provider-mathlib` / `highdimprob-mathlib` -> exact local Mathlib checkouts

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

- `index_repository`: incremental/resumable index; supports `background`, `mode: "resume"`, `topic`, `path_prefix`, `path_filter`, `batch_size`.
- `index_visibility`: tracked/untracked status, stale files, root-import exposure.
- `search_shape`: theorem type-shape search for patterns like `Matrix.trace (cfc f A) = _`.
- `search_theorems`: name/text theorem search; pass `cards: true` for rich theorem cards.
- `theorem_card`: import, source location, namespace, typeclass hints, minimal `#check`.
- `proof_probe`: writes a temp Lean probe and runs `lake env lean`.
- `consumer_fit` / `cross_repo_lookup`: heuristic provider/main/Mathlib API visibility.
- `search_graph`, `search_code`, `get_context`, `cache_status`, `remove_project`.

## Typical Calls

```json
{"name":"index_repository","arguments":{"project":"highdimprob","mode":"full"}}
{"name":"index_repository","arguments":{"project":"provider","mode":"full"}}
{"name":"index_repository","arguments":{"project":"mathlib","topic":"Matrix","mode":"resume"}}
{"name":"search_shape","arguments":{"project":"mathlib","shape":"HasDerivAt (fun t => (A + t • C)⁻¹) _ _","limit":5}}
{"name":"theorem_card","arguments":{"project":"mathlib","qualified_name":"Matrix.IsHermitian.eigenvalues_mem_spectrum_real"}}
{"name":"proof_probe","arguments":{"project":"mathlib","run_project":"provider","checks":["Matrix.IsHermitian.eigenvalues_mem_spectrum_real"]}}
{"name":"search_theorems","arguments":{"project":"mathlib","query":"eigenvalues_mem_spectrum_real","limit":5,"cards":true}}
{"name":"search_theorems","arguments":{"project":"highdimprob","query":"Matrix Bernstein","limit":5}}
{"name":"get_context","arguments":{"project":"highdimprob","qualified_name":"HighDimProb.matrixBernsteinTraceMGF_under_tropp","before":10,"after":10,"neighbor_radius":2}}
{"name":"search_code","arguments":{"project":"provider","pattern":"lambdaMaxOrdered","limit":5,"context":1}}
```

## Fuzz

```powershell
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 300 --seed 1700001
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 80 --seed 1700002 --include-existing
```
## Notes

- Re-index after edits; incremental indexing skips unchanged files by path, mtime, and size.
- `search_theorems` is syntactic and heuristic, but works well for the local theorem-wrapper style.
- A search hit is not a proof check. Use `lake build` for validation.
