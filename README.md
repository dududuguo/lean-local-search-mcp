# lean-local-search-mcp

No-model MCP search for local Lean repositories. It indexes Lean declarations into SQLite/FTS5 and adds lightweight theorem-aware search helpers for names, statements, imports, and parser diagnostics.

## Installation

### Prerequisites

- Python 3.10 or newer and Git. The fixed-release validation also uses `curl` for its first download.
- The Codex CLI if you want to register this server with Codex.
- An existing Lean/Lake project to search. In the commands below, `<lean-project>` means its absolute path.

### Linux and macOS: one-command setup

Clone this repository, enter it, and run:

```bash
./setup.sh
```

The script creates or reuses `.venv`, installs the pinned dependencies, validates the Python entry points, and prints the Codex registration command. It does not change your Codex configuration.

If the executable bit was lost while copying the repository, run `chmod +x setup.sh` first.

### Linux and macOS: manual setup

Use these commands if you prefer to see each installation step:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "pip==26.1.2"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m py_compile tools/lean_local_search_mcp.py tools/fuzz_mcp.py tools/validate_release.py
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "pip==26.1.2"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m py_compile tools\lean_local_search_mcp.py tools\fuzz_mcp.py tools\validate_release.py
```

## Codex MCP

Run the command for your platform from this repository directory. Replace `<lean-project>` with the absolute path to the Lean/Lake project that you want to search.

Linux and macOS:

```bash
codex mcp add lean-local-search -- "$(pwd)/.venv/bin/python" "$(pwd)/tools/lean_local_search_mcp.py" --repo "<lean-project>"
codex mcp get lean-local-search
```

Windows PowerShell:

```powershell
codex mcp add lean-local-search -- "$PWD\.venv\Scripts\python.exe" "$PWD\tools\lean_local_search_mcp.py" --repo "<lean-project>"
codex mcp get lean-local-search
```

Use `repo_path` for explicit repositories. If you configure project aliases, the same tools also accept `project`.

### Install with an AI coding agent

Copy the following prompt into Codex or another coding agent. Start the agent in this repository, or include the repository path in the prompt.

```text
请帮我安装并配置 lean-local-search-mcp。先检查当前操作系统、仓库状态、Python 3、Git 和 Codex CLI，不要覆盖已有文件或已有 MCP 配置。

安装要求：
1. 在 Linux/macOS 上优先运行仓库中的 ./setup.sh；如果脚本不可执行，只添加执行权限后再运行。在 Windows 上按照 README 的 PowerShell 步骤创建 .venv 并安装 requirements.txt。
2. 检查目标 Lean 项目的绝对路径；如果无法从当前上下文确定，只向我询问这个路径，不要猜测。
3. 安装成功后，用虚拟环境中的 Python、tools/lean_local_search_mcp.py 和目标 Lean 项目路径注册名为 lean-local-search 的 Codex MCP 服务。
4. 如果同名 MCP 配置已经存在，先显示现有配置并询问我是否替换，不要直接覆盖。
5. 运行 Python 编译检查和 codex mcp get lean-local-search，最后告诉我实际使用的路径、验证结果和任何仍需手动处理的问题。
```

### Troubleshooting

- **`python3 -m venv` fails on Debian/Ubuntu:** install the matching `python3-venv` package, then rerun `./setup.sh`.
- **`Permission denied: ./setup.sh`:** run `chmod +x setup.sh`.
- **`tree_sitter_lean` cannot be installed or imported:** confirm that Git and build tools are available, then rerun `.venv/bin/python -m pip install -r requirements.txt`. The binding is installed directly from its Git repository.
- **Paths contain spaces:** keep repository and Lean project paths quoted. The examples above already quote them.
- **Codex cannot find the server:** use absolute paths and inspect the saved command with `codex mcp get lean-local-search`.

## Main Tools

- `index_repository`: incremental and resumable indexing with path filters and batch sizes. Returns `total_indexed_files` for the full cache and `scope_indexed_files` for the requested path scope; `indexed_files` is kept as the legacy total-count alias.
- `index_status`, `index_visibility`, `cache_status`, `remove_project`: cache, stale-file, and root-import visibility checks.
- `search_graph`, `search_code`: declaration and source search.
- `search_theorems`, `search_shape`: theorem search by name, symbols, conclusion, premises, and syntactic type shape.
- `theorem_card`, `get_context`, `get_code_snippet`: proof-oriented declaration context.
- `proof_probe`: writes a temporary Lean file and runs `lake env lean`; compact output by default.
- `consumer_fit`, `cross_repo_lookup`: heuristic API matching across indexed repositories.
- `debug_parse_file`: compare scanner, tree-sitter, and regex declaration ranges for one Lean file.

## Search Semantics

- `search_graph.query` tokenizes CamelCase, snake_case, qualified names, and words, then ranks matches across declaration name, qualified name, statement/signature, docstring, file path, and head symbols.
- `name_pattern` and `qn_pattern` use Python regex by default. Plain substrings such as `trace` work because matching is unanchored.
- Common compatibility patterns are also accepted: SQL-LIKE `%trace%` and glob-style `*trace*` / `trace*`.
- Invalid regexes return an actionable error suggesting `foo`, `.*foo.*`, `%foo%`, or `*foo*`.
- `file_pattern` is glob-style, with `%` accepted as a SQL-LIKE alias for `*`.

## Proof Probe Semantics

- `proof_probe.project` selects the lookup/index project.
- If `run_project` and `run_repo_path` are omitted, `proof_probe` also executes `lake env lean` in that same selected project.
- Use `run_project` or `run_repo_path` only when the probe should execute in a different Lean package than the lookup/index project.
- Probe results report both `search_repo` and `run_repo`; import failures include the execution repo that supplied the Lean search path when relevant.

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

Linux and macOS:

```bash
.venv/bin/python -m py_compile tools/lean_local_search_mcp.py tools/fuzz_mcp.py tools/validate_release.py
.venv/bin/python tools/validate_release.py
.venv/bin/python tools/fuzz_mcp.py --iterations 300 --seed 1700201
.venv/bin/python tools/fuzz_mcp.py --iterations 80 --seed 1700202 --include-existing
```

`validate_release.py` downloads the fixed `HighDimProb` `alpha-0.1` source archive on its first run, verifies its pinned SHA-256, caches it under `.lean-local-search/fixtures`, builds an isolated index, and checks exact counts plus representative search APIs. Later runs reuse the verified cache. `--include-existing` uses this same pinned fixture instead of relying on sibling provider or Mathlib repositories, so it is reproducible across machines and does not touch normal MCP indexes.

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m py_compile tools\lean_local_search_mcp.py tools\fuzz_mcp.py tools\validate_release.py
.\.venv\Scripts\python.exe tools\validate_release.py
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 300 --seed 1700201
.\.venv\Scripts\python.exe tools\fuzz_mcp.py --iterations 80 --seed 1700202 --include-existing
```

## License

MIT. See `LICENSE`.
