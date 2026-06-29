#!/usr/bin/env python
"""Tiny local Lean search MCP server: tree-sitter + SQLite FTS5, no models."""
from __future__ import annotations
import argparse, fnmatch, json, os, re, sqlite3, sys, time
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")

SERVER_NAME = "lean-local-search"
SERVER_VERSION = "0.1.0"
SERVER_REPO = Path(__file__).resolve().parents[1]
EXCLUDES = {".git", ".lake", ".lean-local-search", ".tmp", ".venv", "__pycache__"}
TEXT_EXTENSIONS = {".lean", ".md", ".mmd", ".dot", ".html", ".txt"}
KINDS = {"theorem","lemma","def","abbrev","instance","structure","class","inductive","axiom","opaque","constant","example"}
TYPE_KINDS = {"structure","class","inductive"}
DECL_RE = re.compile(r"(?m)^\s*(?:@[^\n]*\n\s*)*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+)*(theorem|lemma|def|abbrev|instance|structure|class|inductive|axiom|opaque|constant|example)\b\s*([^\s:(\[{]+)?")
IMPORT_RE = re.compile(r"(?m)^\s*import\s+(.+?)\s*$")
NS_RE = re.compile(r"^\s*namespace\s+([A-Za-z0-9_.'`]+)\s*$")
END_RE = re.compile(r"^\s*end(?:\s+[A-Za-z0-9_.'`]+)?\s*$")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_'.]*\b")

def log(msg):
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)

def default_repo():
    return Path(os.environ.get("LEAN_SEARCH_REPO") or SERVER_REPO).resolve()

def project_name(repo):
    return str(repo).replace("\\", "-").replace("/", "-").replace(":", "")

def index_root():
    raw = os.environ.get("LEAN_SEARCH_INDEX_ROOT")
    return Path(raw).resolve() if raw else SERVER_REPO / ".lean-local-search"

def db_path(repo):
    repo = Path(repo).resolve()
    if repo == SERVER_REPO:
        return repo / ".lean-local-search" / "index.sqlite3"
    return index_root() / (project_name(repo) + ".sqlite3")

def connect(repo):
    db_path(repo).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path(repo))
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, content TEXT, line_count INTEGER);
    CREATE TABLE IF NOT EXISTS decls(
      id INTEGER PRIMARY KEY, qn TEXT UNIQUE, name TEXT, kind TEXT, file TEXT,
      start_line INTEGER, end_line INTEGER, start_byte INTEGER, end_byte INTEGER,
      namespace TEXT, stmt TEXT, proof TEXT, src TEXT, doc TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS decl_fts USING fts5(qn,name,kind,file,namespace,stmt,proof,src,doc);
    CREATE INDEX IF NOT EXISTS idx_decls_name ON decls(name);
    CREATE INDEX IF NOT EXISTS idx_decls_file ON decls(file);
    """)
    return con

def repo_path_from_project_name(name):
    candidate = index_root() / (str(name) + ".sqlite3")
    if not candidate.exists():
        return None
    try:
        con = sqlite3.connect(candidate)
        row = con.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
        return Path(row[0]).resolve() if row else None
    except Exception:
        return None


def repo_arg(args, fallback):
    raw = args.get("repo_path") or args.get("path") or args.get("root")
    project = args.get("project")
    if raw:
        return Path(raw).resolve()
    if project:
        project = str(project)
        if project == project_name(fallback):
            return fallback
        if ":" in project or "\\" in project or "/" in project:
            return Path(project).resolve()
        resolved = repo_path_from_project_name(project)
        if resolved:
            return resolved
    return fallback

def indexed_files(repo):
    out = []
    for p in repo.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if any(part in EXCLUDES for part in p.relative_to(repo).parts):
            continue
        out.append(p)
    return sorted(out)

def lean_files(repo):
    return indexed_files(repo)

def line_of(data, byte):
    return data[:max(0, byte)].count(b"\n") + 1

def namespace_at(lines, line_no):
    stack = []
    for line in lines[:max(0, line_no - 1)]:
        m = NS_RE.match(line)
        if m:
            stack.append(m.group(1)); continue
        if END_RE.match(line) and stack:
            stack.pop()
    return ".".join(stack)

def doc_before(lines, start_line):
    i, got, in_block = start_line - 2, [], False
    while i >= 0:
        s = lines[i].strip()
        if not s:
            if got: break
            i -= 1; continue
        if s.startswith("--") or s.startswith("/--") or s.startswith("-/") or in_block:
            got.append(lines[i])
            if s.startswith("-/"): in_block = True
            if s.startswith("/--"): in_block = False
            i -= 1; continue
        break
    return "\n".join(reversed(got)).strip()

def split_decl(src):
    for tok in (":= by", ":=", " where"):
        pos = src.find(tok)
        if pos >= 0:
            return (src[:pos].strip(), src[pos + len(tok):].strip()) if tok != " where" else (src[:pos].strip(), src[pos:].strip())
    return src.strip(), ""

def regex_kind_name(src, fallback_kind, fallback_name):
    m = DECL_RE.search(src)
    return (m.group(1), m.group(2) or fallback_name or "_anonymous") if m else (fallback_kind, fallback_name)

def ts_decls(data):
    try:
        from tree_sitter import Language, Parser, Query, QueryCursor
        import tree_sitter_lean
        lang = Language(tree_sitter_lean.language())
        parser = Parser(lang)
        root = parser.parse(data).root_node
        query = Query(lang, tree_sitter_lean.LOCALS_QUERY)
        cursor = QueryCursor(query)
    except Exception as exc:
        log(f"tree-sitter unavailable, regex fallback: {exc}")
        return []
    out = {}
    decl_node_types = {"def","theorem","abbrev","instance","axiom","opaque","constant","structure","inductive"}
    for _pat, caps in cursor.matches(root):
        for cap, nodes in caps.items():
            if cap not in {"local.definition.function", "local.definition.type"}:
                continue
            for name_node in nodes:
                node = name_node.parent
                while node is not None and node.type not in decl_node_types:
                    node = node.parent
                if node is None:
                    continue
                name = data[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
                out[(node.start_byte, node.end_byte)] = {"kind": node.type, "name": name, "start": node.start_byte, "end": node.end_byte}
    return [out[k] for k in sorted(out)]

def regex_decls(data):
    text = data.decode("utf-8", "replace")
    ms = list(DECL_RE.finditer(text)); out = []
    for i, m in enumerate(ms):
        start = len(text[:m.start()].encode("utf-8"))
        end = len(text[:(ms[i+1].start() if i + 1 < len(ms) else len(text))].encode("utf-8"))
        out.append({"kind": m.group(1), "name": m.group(2) or "_anonymous", "start": start, "end": end})
    return out

def extract_file(repo, path):
    data = path.read_bytes(); text = data.decode("utf-8", "replace")
    lines = text.splitlines(); rel = path.relative_to(repo).as_posix()
    raw = (ts_decls(data) or regex_decls(data)) if path.suffix.lower() == ".lean" else []
    decls, seen = [], set()
    for item in raw:
        src = data[item["start"]:item["end"]].decode("utf-8", "replace").strip()
        kind, name = regex_kind_name(src, item["kind"], item["name"])
        if kind not in KINDS:
            continue
        start_line = line_of(data, item["start"])
        end_line = line_of(data, max(item["start"], item["end"] - 1))
        ns = namespace_at(lines, start_line)
        qn = name if "." in name else ".".join(x for x in [ns, name] if x)
        if not qn:
            qn = f"{rel}:{start_line}"
        if qn in seen:
            qn = f"{qn}@{rel}:{start_line}"
        seen.add(qn)
        stmt, proof = split_decl(src)
        decls.append({"qn": qn, "name": name, "kind": kind, "file": rel, "start_line": start_line,
                      "end_line": end_line, "start_byte": item["start"], "end_byte": item["end"],
                      "namespace": ns, "stmt": stmt, "proof": proof, "src": src, "doc": doc_before(lines, start_line)})
    return text, decls

def reindex(repo):
    t0 = time.time(); con = connect(repo); files = lean_files(repo)
    with con:
        con.execute("DELETE FROM decl_fts"); con.execute("DELETE FROM decls"); con.execute("DELETE FROM files")
        ndecl, nimports = 0, 0
        seen_qn = set()
        for path in files:
            st = path.stat(); text, decls = extract_file(repo, path); rel = path.relative_to(repo).as_posix()
            nimports += sum(1 for _ in IMPORT_RE.finditer(text)) if path.suffix.lower() == ".lean" else 0
            con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (rel, st.st_mtime_ns, st.st_size, text, len(text.splitlines())))
            for d in decls:
                if d["qn"] in seen_qn:
                    d["qn"] = d["qn"] + "@" + d["file"] + ":" + str(d["start_line"])
                seen_qn.add(d["qn"])
                cur = con.execute("""INSERT INTO decls(qn,name,kind,file,start_line,end_line,start_byte,end_byte,namespace,stmt,proof,src,doc)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (d["qn"],d["name"],d["kind"],d["file"],d["start_line"],d["end_line"],d["start_byte"],d["end_byte"],d["namespace"],d["stmt"],d["proof"],d["src"],d["doc"]))
                con.execute("INSERT INTO decl_fts(rowid,qn,name,kind,file,namespace,stmt,proof,src,doc) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (cur.lastrowid,d["qn"],d["name"],d["kind"],d["file"],d["namespace"],d["stmt"],d["proof"],d["src"],d["doc"]))
                ndecl += 1
        con.execute("INSERT OR REPLACE INTO meta VALUES ('indexed_at', ?)", (str(int(time.time())),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('repo_path', ?)", (str(repo),))
    return {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "indexed_files": len(files), "declarations": ndecl, "imports": nimports, "elapsed_ms": round((time.time() - t0) * 1000)}

def ensure_index(repo):
    con = connect(repo)
    if con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone() is None:
        reindex(repo)

def row_result(r):
    return {"name": r["name"], "qualified_name": r["qn"], "label": "Type" if r["kind"] in TYPE_KINDS else "Function", "kind": r["kind"], "file_path": r["file"], "start_line": r["start_line"], "end_line": r["end_line"], "in_degree": 0, "out_degree": 0, "lines": r["end_line"] - r["start_line"] + 1, "is_test": "Test" in r["file"]}

def fts(q):
    toks = re.findall(r"[A-Za-z0-9_'.]+", str(q))
    return " OR ".join(f'"{t}"' for t in toks) if toks else str(q).replace('"', '""')

def search_graph(args, repo):
    ensure_index(repo); con = connect(repo)
    limit = int(args.get("limit") or 50); offset = int(args.get("offset") or 0)
    if args.get("query"):
        rows = list(con.execute("SELECT d.*, bm25(decl_fts) rank FROM decl_fts JOIN decls d ON d.id=decl_fts.rowid WHERE decl_fts MATCH ? ORDER BY rank", (fts(args["query"]),)))
        query_text = str(args["query"]).lower()
        query_tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_'.]+", query_text)]
        def query_priority(row):
            name_text = (row["name"] + " " + row["qn"]).lower()
            stmt_text = row["stmt"].lower()
            if query_tokens and all(t in name_text for t in query_tokens):
                bucket = 0
            elif query_tokens and any(t in name_text for t in query_tokens):
                bucket = 1
            elif query_tokens and any(t in stmt_text for t in query_tokens):
                bucket = 2
            else:
                bucket = 3
            return (bucket, row["rank"])
        rows.sort(key=query_priority)
    else:
        rows = list(con.execute("SELECT * FROM decls ORDER BY file,start_line"))
    name_pat, qn_pat, file_pat, label = args.get("name_pattern"), args.get("qn_pattern"), args.get("file_pattern"), args.get("label")
    def keep(r):
        if label == "Type" and r["kind"] not in TYPE_KINDS: return False
        if label == "Function" and r["kind"] in TYPE_KINDS: return False
        if label and label not in {"Type","Function",r["kind"]}: return False
        if file_pat and not fnmatch.fnmatch(r["file"], str(file_pat)): return False
        if name_pat and not re.search(str(name_pat), r["name"]): return False
        if qn_pat and not re.search(str(qn_pat), r["qn"]): return False
        return True
    filt = [r for r in rows if keep(r)]; page = filt[offset:offset+limit]
    return {"total": len(filt), "results": [row_result(r) for r in page], "has_more": offset + len(page) < len(filt)}

def containing(con, file, line):
    return con.execute("SELECT * FROM decls WHERE file=? AND start_line<=? AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1", (file,line,line)).fetchone()

def search_code(args, repo):
    ensure_index(repo); con = connect(repo)
    pat = str(args.get("pattern") or args.get("query") or "")
    if not pat: return {"results": [], "total_grep_matches": 0, "total_results": 0}
    limit = int(args.get("limit") or 20); ctx = int(args["context"]) if args.get("context") is not None else 1
    regex = bool(args.get("regex", False)); rx = re.compile(pat) if regex else None
    mode = args.get("mode") or "compact"; file_pat = args.get("file_pattern"); path_filter = args.get("path_filter")
    results, total, seen, files = [], 0, set(), set()
    for fr in con.execute("SELECT * FROM files ORDER BY path"):
        fp = fr["path"]
        if file_pat and not fnmatch.fnmatch(fp, str(file_pat)): continue
        if path_filter and not re.search(str(path_filter), fp): continue
        lines = fr["content"].splitlines()
        for i, line in enumerate(lines, 1):
            hit = bool(rx.search(line)) if rx else pat.lower() in line.lower()
            if not hit: continue
            total += 1; files.add(fp)
            if mode == "files": continue
            d = containing(con, fp, i); key = d["qn"] if d else f"{fp}:{i}"
            if key in seen: continue
            seen.add(key)
            if len(results) >= limit: continue
            a,b = max(1,i-ctx), min(len(lines),i+ctx)
            snippet = "\n".join(f"{n}: {lines[n-1]}" for n in range(a,b+1))
            item = {"node": d["name"] if d else None, "qualified_name": d["qn"] if d else None, "label": "Function" if d else "File", "file": fp, "start_line": d["start_line"] if d else i, "end_line": d["end_line"] if d else i, "match_lines": [i], "snippet": snippet}
            if mode == "full" and d: item["source"] = d["src"]
            results.append(item)
    if mode == "files": return {"files": sorted(files)[:limit], "total_grep_matches": total, "total_results": len(files)}
    return {"results": results, "raw_matches": [], "total_grep_matches": total, "total_results": len(seen)}

def get_code_snippet(args, repo):
    ensure_index(repo); con = connect(repo)
    q = str(args.get("qualified_name") or args.get("function_name") or args.get("name") or "")
    rows = con.execute("SELECT * FROM decls WHERE qn=? OR name=? ORDER BY length(qn) LIMIT 5", (q,q)).fetchall()
    if not rows:
        sug = con.execute("SELECT qn qualified_name,name,kind,file file_path,start_line FROM decls WHERE qn LIKE ? OR name LIKE ? LIMIT 10", (f"%{q}%", f"%{q}%")).fetchall()
        return {"found": False, "suggestions": [dict(x) for x in sug]}
    if len(rows) > 1 and rows[0]["qn"] != q:
        return {"found": False, "ambiguous": True, "suggestions": [dict(x) for x in rows]}
    r = rows[0]
    out = {"found": True, "qualified_name": r["qn"], "name": r["name"], "kind": r["kind"], "file_path": r["file"], "start_line": r["start_line"], "end_line": r["end_line"], "statement": r["stmt"], "proof": r["proof"], "source": r["src"], "docstring": r["doc"]}
    if args.get("include_neighbors"):
        ns = con.execute("SELECT qn qualified_name,name,kind,start_line,end_line FROM decls WHERE file=? AND start_line BETWEEN ? AND ? ORDER BY start_line", (r["file"], max(1,r["start_line"]-80), r["end_line"]+80)).fetchall()
        out["neighbors"] = [dict(x) for x in ns]
    return out

def get_architecture(args, repo):
    ensure_index(repo); con = connect(repo)
    kinds = [dict(x) for x in con.execute("SELECT kind,count(*) count FROM decls GROUP BY kind ORDER BY count DESC")]
    files = con.execute("SELECT count(*) count FROM files").fetchone()["count"]
    decls = con.execute("SELECT count(*) count FROM decls").fetchone()["count"]
    imports = set()
    for r in con.execute("SELECT path, content FROM files"):
        if not str(r["path"]).endswith(".lean"):
            continue
        for m in IMPORT_RE.finditer(r["content"]): imports.update(m.group(1).split())
    return {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "files": files, "declarations": decls, "kinds": kinds, "imports": sorted(imports)}

def index_status(args, repo):
    con = connect(repo); meta = con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    return {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "status": "ready" if meta else "not_indexed", "indexed_at": int(meta["value"]) if meta else None, "files": con.execute("SELECT count(*) count FROM files").fetchone()["count"], "declarations": con.execute("SELECT count(*) count FROM decls").fetchone()["count"]}

def indexed_projects(default_repo):
    projects = [{"name": project_name(default_repo), "root_path": str(default_repo), "db_path": str(db_path(default_repo))}]
    root = index_root()
    if root.exists():
        for db in sorted(root.glob("*.sqlite3")):
            try:
                con = sqlite3.connect(db)
                row = con.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
                if row:
                    repo = Path(row[0]).resolve()
                    item = {"name": project_name(repo), "root_path": str(repo), "db_path": str(db)}
                    if item not in projects:
                        projects.append(item)
            except Exception:
                continue
    return projects

def trace_path(args, repo):
    ensure_index(repo); con = connect(repo)
    q = str(args.get("function_name") or args.get("qualified_name") or args.get("name") or "")
    d = con.execute("SELECT * FROM decls WHERE qn=? OR name=? LIMIT 1", (q,q)).fetchone()
    if not d: return {"found": False, "message": "symbol not found"}
    direction = args.get("direction") or "both"; inbound=[]; outbound=[]
    if direction in {"inbound","both"}:
        inbound = [row_result(r) for r in con.execute("SELECT * FROM decls WHERE id!=? AND src LIKE ? LIMIT 100", (d["id"], f"%{d['name']}%"))]
    if direction in {"outbound","both"}:
        names = {x.split(".")[-1] for x in IDENT_RE.findall(d["src"])}
        if names:
            qs = ",".join("?" for _ in names)
            outbound = [row_result(r) for r in con.execute(f"SELECT * FROM decls WHERE name IN ({qs}) LIMIT 100", tuple(names)) if r["id"] != d["id"]]
    return {"found": True, "mode": "approximate_identifier_text", "target": row_result(d), "inbound": inbound, "outbound": outbound}

TOOLS = {
 "index_repository": ("Index a Lean repository into local SQLite FTS.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"mode":{"type":"string"}}}),
 "index_status": ("Return local Lean index status.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "list_projects": ("List configured local Lean project.", {"type":"object","properties":{}}),
 "search_graph": ("Search Lean declarations by FTS query, name_pattern, qn_pattern, file_pattern, or label.", {"type":"object","properties":{"query":{"type":"string"},"name_pattern":{"type":"string"},"qn_pattern":{"type":"string"},"file_pattern":{"type":"string"},"label":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "search_code": ("Search raw Lean source and return declaration-aware hits.", {"type":"object","properties":{"pattern":{"type":"string"},"query":{"type":"string"},"regex":{"type":"boolean"},"file_pattern":{"type":"string"},"path_filter":{"type":"string"},"mode":{"type":"string"},"context":{"type":"integer"},"limit":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "get_code_snippet": ("Read source for an indexed Lean declaration.", {"type":"object","properties":{"qualified_name":{"type":"string"},"name":{"type":"string"},"include_neighbors":{"type":"boolean"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "trace_path": ("Best-effort identifier-text inbound/outbound trace.", {"type":"object","properties":{"function_name":{"type":"string"},"qualified_name":{"type":"string"},"direction":{"type":"string"},"depth":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "get_architecture": ("Return declaration counts, imports, and index metadata.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"}}}),
}

def call_tool(name, args, default_repo):
    repo = repo_arg(args, default_repo)
    if name == "index_repository": return reindex(repo)
    if name == "index_status": return index_status(args, repo)
    if name == "list_projects": return {"projects": indexed_projects(repo)}
    if name == "search_graph": return search_graph(args, repo)
    if name == "search_code": return search_code(args, repo)
    if name == "get_code_snippet": return get_code_snippet(args, repo)
    if name == "trace_path": return trace_path(args, repo)
    if name == "get_architecture": return get_architecture(args, repo)
    raise ValueError(f"unknown tool: {name}")

def write(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, separators=(",",":")) + "\n"); sys.stdout.flush()

def handle(msg, default_repo):
    mid, method = msg.get("id"), msg.get("method")
    try:
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
            write({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":ver,"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":SERVER_NAME,"version":SERVER_VERSION},"instructions":"Use index_repository first, search_graph for declarations, search_code for raw text, get_code_snippet for source."}})
        elif method == "tools/list":
            write({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":n,"description":d,"inputSchema":s} for n,(d,s) in TOOLS.items()]}})
        elif method == "tools/call":
            p = msg.get("params") or {}; res = call_tool(str(p.get("name")), p.get("arguments") or {}, default_repo)
            write({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps(res, ensure_ascii=False, indent=2)}]}})
        elif method in {"notifications/initialized","notifications/cancelled"}:
            return
        elif method == "ping":
            write({"jsonrpc":"2.0","id":mid,"result":{}})
        else:
            write({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":f"method not found: {method}"}})
    except Exception as exc:
        log(f"error handling {method}: {exc}")
        write({"jsonrpc":"2.0","id":mid,"error":{"code":-32000,"message":str(exc)}})

def serve(repo):
    log(f"serving {repo}")
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: handle(json.loads(line), repo)
        except Exception as exc: log(f"bad message: {exc}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(default_repo()))
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--search")
    ns = ap.parse_args(); repo = Path(ns.repo).resolve()
    if ns.index: print(json.dumps(reindex(repo), ensure_ascii=False, indent=2)); return 0
    if ns.status: print(json.dumps(index_status({}, repo), ensure_ascii=False, indent=2)); return 0
    if ns.search: print(json.dumps(search_graph({"query":ns.search}, repo), ensure_ascii=False, indent=2)); return 0
    serve(repo); return 0

if __name__ == "__main__":
    raise SystemExit(main())
