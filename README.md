# lean-local-search-mcp

A small no-model MCP server for fast Lean code search.

It uses `tree-sitter-lean` to extract Lean declarations and SQLite FTS5 to search declarations and raw project text. It does not use embeddings, local models, remote models, Lake, or Lean elaboration.

## Scope

This is a downstream companion tool for Lean development. It is intentionally separate from `tree-sitter-lean`, whose job is parsing Lean syntax.

Good fits:

- find theorem, lemma, def, abbrev, structure, class, and instance declarations;
- search raw `.lean`, `.md`, `.mmd`, `.dot`, `.html`, and `.txt` files;
- retrieve exact declaration snippets for agents;
- keep multiple local Lean repositories indexed for Codex MCP use.

Non-goals:

- semantic search;
- proof generation;
- Lean elaboration;
- replacing `lake build`;
- maintaining a precise proof dependency graph.

## Install

Create a local virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Verify the parser binding:

```powershell
.\.venv\Scripts\python.exe -c "import tree_sitter_lean; print(tree_sitter_lean.language())"
```

If `tree_sitter_lean` imports but cannot load its native binding on Windows, rebuild or reinstall your local `tree-sitter-lean` fork before debugging this MCP server.

## Run As A Local MCP Server

Example Codex registration for a default repository:

```powershell
codex mcp add lean-local-search -- C:\Users\11388\reserach\lean-local-search-mcp\.venv\Scripts\python.exe C:\Users\11388\reserach\lean-local-search-mcp\tools\lean_local_search_mcp.py --repo C:\Users\11388\reserach\HighDimProbLiebProvider
```

Check the registration:

```powershell
codex mcp get lean-local-search
```

## Tools

- `index_repository`: build or rebuild a local SQLite index for a Lean repository or text directory.
- `index_status`: show index readiness and counts.
- `list_projects`: list indexed roots known to the local cache.
- `search_graph`: search indexed Lean declarations.
- `search_code`: search raw source and documentation text.
- `get_code_snippet`: return exact source for one indexed declaration.
- `trace_path`: approximate identifier-text inbound/outbound usage search.
- `get_architecture`: return declaration counts, import names, and metadata.

## Example MCP Calls

Index a repository:

```json
{
  "name": "index_repository",
  "arguments": {
    "repo_path": "C:\\Users\\11388\\reserach\\HighDimProb"
  }
}
```

Search declarations:

```json
{
  "name": "search_graph",
  "arguments": {
    "repo_path": "C:\\Users\\11388\\reserach\\HighDimProb",
    "query": "Matrix Bernstein",
    "limit": 5
  }
}
```

Read exact source after search:

```json
{
  "name": "get_code_snippet",
  "arguments": {
    "project": "C-Users-11388-reserach-HighDimProb",
    "qualified_name": "HighDimProb.matrixBernsteinTwoSidedOptimizedScalarTailRHS",
    "include_neighbors": true
  }
}
```

Search raw text:

```json
{
  "name": "search_code",
  "arguments": {
    "project": "C-Users-11388-reserach-HighDimProb",
    "pattern": "Matrix Bernstein",
    "limit": 3,
    "context": 1
  }
}
```

## Notes For Agents

- Call `index_repository` first after clone, after switching target repositories, or after meaningful edits.
- Use `search_graph` for Lean declarations and `search_code` for docs/comments/literals.
- Use `get_code_snippet` only after `search_graph` returns the exact `qualified_name`.
- `trace_path` is approximate identifier-text search, not a Lean dependency graph.
- A declaration appearing in this index does not mean the proof currently builds. Use `lake build` for validation.
- If Codex just added or restarted this MCP server, an existing thread may need refresh or a new thread before the tool namespace is available.

## Current MVP Limitations

- No incremental indexing yet; indexing rebuilds the target database.
- Project names are path-derived unless the caller uses `repo_path` directly.
- The parser is syntactic; theorem conclusion/premise structure is not normalized yet.
- Multi-project search is manual: search one indexed project at a time.