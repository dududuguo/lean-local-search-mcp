#!/usr/bin/env python
"""Tiny local Lean search MCP server: tree-sitter + SQLite FTS5, no models."""
from __future__ import annotations
import argparse, fnmatch, json, os, re, sqlite3, sys, time
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"): sys.stderr.reconfigure(encoding="utf-8")

SERVER_NAME = "lean-local-search"
SERVER_VERSION = "0.2.0"
SCHEMA_VERSION = "2"
SERVER_REPO = Path(__file__).resolve().parents[1]
EXCLUDES = {".git", ".lake", ".lean-local-search", ".tmp", ".venv", "__pycache__"}
TEXT_EXTENSIONS = {".lean", ".md", ".mmd", ".dot", ".html", ".txt"}
KINDS = {"theorem","lemma","def","abbrev","instance","structure","class","inductive","axiom","opaque","constant","example"}
THEOREM_KINDS = {"theorem","lemma","abbrev"}
TYPE_KINDS = {"structure","class","inductive"}
STOP_SYMBOLS = {"theorem","lemma","def","abbrev","instance","structure","class","inductive","axiom","opaque","constant","example","by","where","let","have","show","fun","forall","Prop","Type","Sort","True","False","Nat","Int","Real","Complex"}
RELATION_MARKERS = [" = "," < "," > ","<=",">=","->","\\le","\\ge","\\in","\u2264","\u2265","\u2208","\u2192","\u2194"]
DECL_RE = re.compile(r"(?m)^\s*(?:@[^\n]*\n\s*)*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+)*(theorem|lemma|def|abbrev|instance|structure|class|inductive|axiom|opaque|constant|example)\b\s*([^\s:(\[{]+)?")
IMPORT_RE = re.compile(r"(?m)^\s*import\s+(.+?)\s*$")
NS_RE = re.compile(r"^\s*namespace\s+([A-Za-z0-9_.'`]+)\s*$")
END_RE = re.compile(r"^\s*end(?:\s+[A-Za-z0-9_.'`]+)?\s*$")
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_'.]*\b")

def log(msg):
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)

def local_repo_aliases():
    base = SERVER_REPO.parent
    return {
        "provider": base / "HighDimProbLiebProvider",
        "HighDimProbLiebProvider": base / "HighDimProbLiebProvider",
        "highdimprob": base / "HighDimProb",
        "HighDimProb": base / "HighDimProb",
        "main": base / "HighDimProb",
    }


def alias_repo(name):
    repo = local_repo_aliases().get(str(name))
    return repo.resolve() if repo and repo.exists() else None


def aliases_for_repo(repo):
    repo = Path(repo).resolve()
    return sorted(k for k, v in local_repo_aliases().items() if v.exists() and v.resolve() == repo)

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

def ensure_schema(con):
    cols = {r["name"] for r in con.execute("PRAGMA table_info(decls)")}
    for name, typ in {
        "signature": "TEXT",
        "binders_json": "TEXT",
        "premises": "TEXT",
        "conclusion": "TEXT",
        "head_symbols": "TEXT",
    }.items():
        if name not in cols:
            con.execute(f"ALTER TABLE decls ADD COLUMN {name} {typ}")
    expected = {"qn","name","kind","file","namespace","stmt","proof","src","doc","signature","premises","conclusion","head_symbols"}
    fts_cols = {r["name"] for r in con.execute("PRAGMA table_info(decl_fts)")}
    if not expected.issubset(fts_cols):
        con.execute("DROP TABLE IF EXISTS decl_fts")
        con.execute("CREATE VIRTUAL TABLE decl_fts USING fts5(qn,name,kind,file,namespace,stmt,proof,src,doc,signature,premises,conclusion,head_symbols)")


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
      namespace TEXT, stmt TEXT, proof TEXT, src TEXT, doc TEXT,
      signature TEXT, binders_json TEXT, premises TEXT, conclusion TEXT, head_symbols TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_decls_name ON decls(name);
    CREATE INDEX IF NOT EXISTS idx_decls_file ON decls(file);
    """)
    ensure_schema(con)
    return con

def repo_path_from_project_name(name):
    candidate = index_root() / (str(name) + ".sqlite3")
    if not candidate.exists():
        return None
    try:
        con = sqlite3.connect(candidate)
        row = con.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
        con.close()
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
        aliased = alias_repo(project)
        if aliased:
            return aliased
        if project == project_name(fallback):
            return fallback
        if ":" in project or "\\" in project or "/" in project:
            return Path(project).resolve()
        resolved = repo_path_from_project_name(project)
        if resolved:
            return resolved
    return fallback

def db_path_from_args(args, fallback):
    project = args.get("project")
    raw = args.get("repo_path") or args.get("path") or args.get("root")
    if project and not raw and not (":" in str(project) or "\\" in str(project) or "/" in str(project)):
        candidate = index_root() / (str(project) + ".sqlite3")
        if candidate.exists():
            return candidate.resolve()
    return db_path(repo_arg(args, fallback)).resolve()

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

def find_top_level_token(s, tokens):
    pairs = {"(": ")", "{": "}", "[": "]"}
    closing = set(pairs.values())
    stack = []
    in_string = False
    escaped = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in closing and stack and stack[-1] == ch:
            stack.pop()
        elif not stack:
            for tok in tokens:
                if s.startswith(tok, i):
                    return i, tok
        i += 1
    return -1, ""


def split_decl(src):
    pos, tok = find_top_level_token(src, (":= by", ":=", " where"))
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

def find_top_level_colon(s):
    pairs = {"(": ")", "{": "}", "[": "]"}
    closing = set(pairs.values())
    stack = []
    in_string = False
    escaped = False
    for i, ch in enumerate(s):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in closing and stack and stack[-1] == ch:
            stack.pop()
        elif ch == ":" and not stack:
            return i
    return -1


def top_level_binder_groups(s):
    pairs = {"(": ")", "{": "}", "[": "]"}
    groups = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch not in pairs:
            i += 1
            continue
        opener, closer = ch, pairs[ch]
        depth, j = 1, i + 1
        in_string, escaped = False, False
        while j < len(s):
            c = s[j]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
            elif c == '"':
                in_string = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    groups.append({"bracket": opener + closer, "text": s[i + 1:j].strip()})
                    i = j
                    break
            j += 1
        i += 1
    return groups

def symbol_list(text, limit=80):
    seen, out = set(), []
    for ident in IDENT_RE.findall(text or ""):
        short = ident.split(".")[-1]
        if ident in STOP_SYMBOLS or short in STOP_SYMBOLS:
            continue
        if len(short) == 1 and short.islower():
            continue
        if ident not in seen:
            seen.add(ident)
            out.append(ident)
        if len(out) >= limit:
            break
    return out


def looks_like_premise(names, typ):
    lowered = [n.lower() for n in names]
    if any(n.startswith("h") for n in lowered):
        return True
    t = " " + " ".join(str(typ).split()) + " "
    if " Prop " in t or t.strip() == "Prop":
        return True
    return any(marker in t for marker in RELATION_MARKERS)


def analyze_statement(kind, name, stmt):
    if kind not in KINDS:
        return {"signature": "", "binders": [], "premises": [], "conclusion": "", "head_symbols": []}
    m = DECL_RE.search(stmt or "")
    rest = (stmt[m.end():] if m else stmt or "").strip()
    colon = find_top_level_colon(rest)
    if colon >= 0:
        left, conclusion = rest[:colon].strip(), rest[colon + 1:].strip()
    else:
        left, conclusion = rest, ""
    binders, premises = [], []
    for group in top_level_binder_groups(left):
        text = " ".join(group["text"].split())
        split = find_top_level_colon(text)
        if split < 0:
            continue
        raw_names = text[:split].strip()
        typ = text[split + 1:].strip()
        names = [n for n in re.split(r"[\s,]+", raw_names) if n and n != "_"]
        item = {"names": names, "type": typ, "bracket": group["bracket"]}
        binders.append(item)
        if looks_like_premise(names, typ):
            premises.append(text)
    profile_text = "\n".join([name or "", conclusion, "\n".join(premises)])
    return {"signature": rest, "binders": binders, "premises": premises, "conclusion": conclusion, "head_symbols": symbol_list(profile_text)}

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
        profile = analyze_statement(kind, name, stmt)
        decls.append({"qn": qn, "name": name, "kind": kind, "file": rel, "start_line": start_line,
                      "end_line": end_line, "start_byte": item["start"], "end_byte": item["end"],
                      "namespace": ns, "stmt": stmt, "proof": proof, "src": src, "doc": doc_before(lines, start_line),
                      "signature": profile["signature"], "binders_json": json.dumps(profile["binders"], ensure_ascii=False),
                      "premises": "\n".join(profile["premises"]), "conclusion": profile["conclusion"],
                      "head_symbols": " ".join(profile["head_symbols"])})
    return text, decls

def delete_file_index(con, rel):
    ids = [r["id"] for r in con.execute("SELECT id FROM decls WHERE file=?", (rel,))]
    if ids:
        qs = ",".join("?" for _ in ids)
        con.execute(f"DELETE FROM decl_fts WHERE rowid IN ({qs})", ids)
    con.execute("DELETE FROM decls WHERE file=?", (rel,))
    con.execute("DELETE FROM files WHERE path=?", (rel,))


def unique_qn(con, qn, seen_qn, file, start_line):
    base = qn
    if base not in seen_qn and con.execute("SELECT 1 FROM decls WHERE qn=?", (base,)).fetchone() is None:
        seen_qn.add(base)
        return base
    suffix = f"@{file}:{start_line}"
    candidate = base + suffix
    n = 2
    while candidate in seen_qn or con.execute("SELECT 1 FROM decls WHERE qn=?", (candidate,)).fetchone() is not None:
        candidate = f"{base}{suffix}:{n}"
        n += 1
    seen_qn.add(candidate)
    return candidate


def insert_file_index(con, repo, path, seen_qn):
    st = path.stat(); text, decls = extract_file(repo, path); rel = path.relative_to(repo).as_posix()
    con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (rel, st.st_mtime_ns, st.st_size, text, len(text.splitlines())))
    ndecl = 0
    for d in decls:
        d["qn"] = unique_qn(con, d["qn"], seen_qn, d["file"], d["start_line"])
        cur = con.execute("""INSERT INTO decls(qn,name,kind,file,start_line,end_line,start_byte,end_byte,namespace,stmt,proof,src,doc,signature,binders_json,premises,conclusion,head_symbols)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (d["qn"],d["name"],d["kind"],d["file"],d["start_line"],d["end_line"],d["start_byte"],d["end_byte"],d["namespace"],d["stmt"],d["proof"],d["src"],d["doc"],d["signature"],d["binders_json"],d["premises"],d["conclusion"],d["head_symbols"]))
        con.execute("INSERT INTO decl_fts(rowid,qn,name,kind,file,namespace,stmt,proof,src,doc,signature,premises,conclusion,head_symbols) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cur.lastrowid,d["qn"],d["name"],d["kind"],d["file"],d["namespace"],d["stmt"],d["proof"],d["src"],d["doc"],d["signature"],d["premises"],d["conclusion"],d["head_symbols"]))
        ndecl += 1
    return ndecl


def table_count(con, table):
    return con.execute(f"SELECT count(*) count FROM {table}").fetchone()["count"]


def total_imports(con):
    nimports = 0
    for r in con.execute("SELECT path, content FROM files"):
        if str(r["path"]).endswith(".lean"):
            nimports += sum(1 for _ in IMPORT_RE.finditer(r["content"]))
    return nimports

def stale_summary(repo, con):
    current = {}
    if Path(repo).exists():
        for p in indexed_files(repo):
            st = p.stat()
            current[p.relative_to(repo).as_posix()] = (st.st_mtime_ns, st.st_size)
    indexed = {r["path"]: (r["mtime_ns"], r["size"]) for r in con.execute("SELECT path,mtime_ns,size FROM files")}
    removed = sorted(set(indexed) - set(current))
    added = sorted(set(current) - set(indexed))
    changed = sorted(rel for rel in set(current) & set(indexed) if current[rel] != indexed[rel])
    return {"added_files": len(added), "changed_files": len(changed), "removed_files": len(removed), "is_stale": bool(added or changed or removed)}


def index_repository(args, repo):
    t0 = time.time(); con = connect(repo)
    mode = str(args.get("mode") or "incremental").lower()
    current_paths = indexed_files(repo)
    current = {}
    for p in current_paths:
        st = p.stat()
        current[p.relative_to(repo).as_posix()] = {"path": p, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    meta_indexed = con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    meta_schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    full = mode in {"full", "rebuild", "force"} or meta_indexed is None or (meta_schema is None or meta_schema["value"] != SCHEMA_VERSION)
    changed_rels, removed_rels = [], []
    with con:
        if full:
            con.execute("DELETE FROM decl_fts"); con.execute("DELETE FROM decls"); con.execute("DELETE FROM files")
            changed_rels = sorted(current)
        else:
            indexed = {r["path"]: (r["mtime_ns"], r["size"]) for r in con.execute("SELECT path,mtime_ns,size FROM files")}
            removed_rels = sorted(set(indexed) - set(current))
            changed_rels = sorted(rel for rel, st in current.items() if rel not in indexed or indexed[rel] != (st["mtime_ns"], st["size"]))
            for rel in removed_rels + changed_rels:
                delete_file_index(con, rel)
        seen_qn = set(r["qn"] for r in con.execute("SELECT qn FROM decls"))
        nchanged_decls = 0
        for rel in changed_rels:
            nchanged_decls += insert_file_index(con, repo, current[rel]["path"], seen_qn)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('indexed_at', ?)", (str(int(time.time())),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('repo_path', ?)", (str(repo),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    result = {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "mode": "full" if full else "incremental", "indexed_files": table_count(con, "files"), "declarations": table_count(con, "decls"), "imports": total_imports(con), "changed_files": len(changed_rels), "changed_declarations": nchanged_decls, "removed_files": len(removed_rels), "skipped_files": max(0, len(current) - len(changed_rels)), "elapsed_ms": round((time.time() - t0) * 1000)}
    con.close()
    return result


def reindex(repo):
    return index_repository({"mode": "full"}, repo)


def ensure_index(repo):
    con = connect(repo)
    meta = con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    needs_index = meta is None or schema is None or schema["value"] != SCHEMA_VERSION
    con.close()
    if needs_index:
        index_repository({"mode": "full"}, repo)

def row_result(r):
    return {"name": r["name"], "qualified_name": r["qn"], "label": "Type" if r["kind"] in TYPE_KINDS else "Function", "kind": r["kind"], "file_path": r["file"], "start_line": r["start_line"], "end_line": r["end_line"], "in_degree": 0, "out_degree": 0, "lines": r["end_line"] - r["start_line"] + 1, "is_test": "Test" in r["file"]}

def theorem_result(r):
    out = row_result(r)
    out.update({"statement": r["stmt"], "conclusion": r["conclusion"] or "", "premises": [x for x in (r["premises"] or "").splitlines() if x.strip()], "head_symbols": [x for x in (r["head_symbols"] or "").split() if x.strip()]})
    return out

def fts(q):
    toks = re.findall(r"[A-Za-z0-9_'.]+", str(q))
    return " OR ".join(f'"{t}"' for t in toks) if toks else str(q).replace('"', '""')

def query_tokens(value):
    return [t.lower() for t in re.findall(r"[A-Za-z0-9_'.]+", str(value or ""))]


def contains_all(text, toks):
    lower = (text or "").lower()
    return all(t in lower for t in toks)


def contains_any(text, toks):
    lower = (text or "").lower()
    return any(t in lower for t in toks)


def search_graph(args, repo):
    ensure_index(repo); con = connect(repo)
    limit = int(args.get("limit") or 50); offset = int(args.get("offset") or 0)
    if args.get("query"):
        rows = list(con.execute("SELECT d.*, bm25(decl_fts) rank FROM decl_fts JOIN decls d ON d.id=decl_fts.rowid WHERE decl_fts MATCH ? ORDER BY rank", (fts(args["query"]),)))
        toks = query_tokens(args["query"])
        def query_priority(row):
            name_text = (row["name"] + " " + row["qn"]).lower()
            theorem_text = "\n".join([row["conclusion"] or "", row["premises"] or "", row["head_symbols"] or ""]).lower()
            stmt_text = (row["stmt"] or "").lower()
            if toks and all(t in name_text for t in toks):
                bucket = 0
            elif toks and any(t in name_text for t in toks):
                bucket = 1
            elif toks and any(t in theorem_text for t in toks):
                bucket = 2
            elif toks and any(t in stmt_text for t in toks):
                bucket = 3
            else:
                bucket = 4
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

def search_theorems(args, repo):
    ensure_index(repo); con = connect(repo)
    limit = int(args.get("limit") or 50); offset = int(args.get("offset") or 0)
    toks = query_tokens(args.get("query"))
    conclusion_toks = query_tokens(args.get("conclusion"))
    premise_toks = query_tokens(args.get("premise") or args.get("premises"))
    symbol_toks = query_tokens(args.get("symbol") or args.get("symbols"))
    name_pat, qn_pat, file_pat = args.get("name_pattern"), args.get("qn_pattern"), args.get("file_pattern")
    rows = list(con.execute("SELECT * FROM decls WHERE kind IN ('theorem','lemma','abbrev')"))
    scored = []
    for r in rows:
        if file_pat and not fnmatch.fnmatch(r["file"], str(file_pat)): continue
        if name_pat and not re.search(str(name_pat), r["name"]): continue
        if qn_pat and not re.search(str(qn_pat), r["qn"]): continue
        short_name = r["name"] or ""
        qn_text = r["qn"] or ""
        name_text = f"{short_name} {qn_text}"
        theorem_text = "\n".join([r["conclusion"] or "", r["premises"] or "", r["head_symbols"] or "", r["stmt"] or ""])
        if toks and not contains_any(name_text + "\n" + theorem_text, toks): continue
        if conclusion_toks and not contains_all(r["conclusion"] or "", conclusion_toks): continue
        if premise_toks and not contains_any(r["premises"] or "", premise_toks): continue
        if symbol_toks and not contains_all(r["head_symbols"] or "", symbol_toks): continue
        score = 0
        if toks:
            compact = "".join(toks)
            short_lower = short_name.lower()
            qn_lower = qn_text.lower()
            if compact and short_lower.startswith(compact): score += 260
            elif compact and compact in short_lower: score += 220
            elif contains_all(short_name, toks): score += 180
            elif contains_any(short_name, toks): score += 120
            elif contains_all(qn_text, toks): score += 80
            elif contains_any(qn_text, toks): score += 45
            if contains_all(r["conclusion"] or "", toks): score += 70
            elif contains_any(r["conclusion"] or "", toks): score += 45
            if contains_any(r["head_symbols"] or "", toks): score += 35
            if contains_any(r["premises"] or "", toks): score += 20
            if contains_any(r["stmt"] or "", toks): score += 10
        if conclusion_toks: score += 60
        if premise_toks: score += 25
        if symbol_toks: score += 40
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["kind"] != "theorem", x[1]["file"], x[1]["start_line"]))
    filt = [r for _, r in scored]
    page = filt[offset:offset+limit]
    return {"total": len(filt), "results": [theorem_result(r) for r in page], "has_more": offset + len(page) < len(filt)}

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

def lookup_decl(con, q):
    rows = con.execute("SELECT * FROM decls WHERE qn=? OR name=? ORDER BY length(qn) LIMIT 5", (q, q)).fetchall()
    if not rows:
        return None, []
    if len(rows) > 1 and rows[0]["qn"] != q:
        return None, rows
    return rows[0], rows


def file_decl_neighbors(con, file, start_line, radius):
    prev_rows = list(con.execute("SELECT qn qualified_name,name,kind,start_line,end_line FROM decls WHERE file=? AND start_line<? ORDER BY start_line DESC LIMIT ?", (file, start_line, radius)))
    next_rows = list(con.execute("SELECT qn qualified_name,name,kind,start_line,end_line FROM decls WHERE file=? AND start_line>? ORDER BY start_line LIMIT ?", (file, start_line, radius)))
    return {"previous": [dict(x) for x in reversed(prev_rows)], "next": [dict(x) for x in next_rows]}


def get_context(args, repo):
    ensure_index(repo); con = connect(repo)
    q = str(args.get("qualified_name") or args.get("function_name") or args.get("name") or "")
    r, rows = lookup_decl(con, q)
    if r is None and not rows:
        sug = con.execute("SELECT qn qualified_name,name,kind,file file_path,start_line FROM decls WHERE qn LIKE ? OR name LIKE ? LIMIT 10", (f"%{q}%", f"%{q}%")).fetchall()
        return {"found": False, "suggestions": [dict(x) for x in sug]}
    if r is None:
        return {"found": False, "ambiguous": True, "suggestions": [dict(x) for x in rows]}
    fr = con.execute("SELECT * FROM files WHERE path=?", (r["file"],)).fetchone()
    lines = fr["content"].splitlines() if fr else []
    before_value = args.get("before") if args.get("before") is not None else args.get("lines_before")
    after_value = args.get("after") if args.get("after") is not None else args.get("lines_after")
    before = int(before_value) if before_value is not None else 25
    after = int(after_value) if after_value is not None else 25
    radius = int(args["neighbor_radius"]) if args.get("neighbor_radius") is not None else 5
    a, b = max(1, r["start_line"] - before), min(len(lines), r["end_line"] + after)
    source_context = "\n".join(f"{n}: {lines[n - 1]}" for n in range(a, b + 1)) if lines else ""
    imports = [m.group(1) for m in IMPORT_RE.finditer(fr["content"] if fr else "")]
    header_lines = []
    for i, line in enumerate(lines[:max(0, r["start_line"] - 1)], 1):
        stripped = line.strip()
        if stripped.startswith(("open ", "variable ", "variables ", "section ", "namespace ", "local ", "attribute ")):
            header_lines.append({"line": i, "text": line})
    return {"found": True, "target": theorem_result(r) if r["kind"] in THEOREM_KINDS else row_result(r), "repo_path": str(repo), "file_path": r["file"], "imports": imports, "namespace": r["namespace"], "docstring": r["doc"], "local_header": header_lines[-40:], "theorem_profile": {"signature": r["signature"] or "", "binders": json.loads(r["binders_json"] or "[]"), "premises": [x for x in (r["premises"] or "").splitlines() if x.strip()], "conclusion": r["conclusion"] or "", "head_symbols": [x for x in (r["head_symbols"] or "").split() if x.strip()]}, "neighbors": file_decl_neighbors(con, r["file"], r["start_line"], radius), "source": r["src"], "source_context": source_context}

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
    schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    db = db_path(repo)
    status = "ready" if meta and schema and schema["value"] == SCHEMA_VERSION else "not_indexed" if meta is None else "needs_reindex"
    out = {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db), "schema_version": schema["value"] if schema else None, "status": status, "indexed_at": int(meta["value"]) if meta else None, "files": table_count(con, "files"), "declarations": table_count(con, "decls"), "cache_bytes": db.stat().st_size if db.exists() else 0}
    out.update(stale_summary(repo, con))
    con.close()
    return out


def indexed_projects(default_repo):
    projects, seen = [], set()
    def add(repo, db):
        repo = Path(repo).resolve()
        key = str(repo).lower()
        if key in seen:
            return
        seen.add(key)
        item = {"name": project_name(repo), "aliases": aliases_for_repo(repo), "root_path": str(repo), "db_path": str(db)}
        if Path(db).exists():
            item["cache_bytes"] = Path(db).stat().st_size
        projects.append(item)
    add(default_repo, db_path(default_repo))
    root = index_root()
    if root.exists():
        for db in sorted(root.glob("*.sqlite3")):
            try:
                con = sqlite3.connect(db)
                row = con.execute("SELECT value FROM meta WHERE key='repo_path'").fetchone()
                con.close()
                if row:
                    add(Path(row[0]).resolve(), db)
            except Exception:
                continue
    return projects

def cache_status(args, default_repo):
    if args.get("project") or args.get("repo_path") or args.get("path") or args.get("root"):
        return index_status(args, repo_arg(args, default_repo))
    return {"index_root": str(index_root()), "projects": indexed_projects(default_repo)}


def remove_project(args, default_repo):
    if not any(args.get(k) for k in ("project", "repo_path", "path", "root")) and not args.get("allow_default"):
        return {"removed": False, "message": "pass project or repo_path, or allow_default=true to remove the default repo cache"}
    db = db_path_from_args(args, default_repo)
    removed, missing = [], []
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink(); removed.append(str(p))
        else:
            missing.append(str(p))
    return {"removed": bool(removed), "paths": removed, "missing": missing}

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
 "index_repository": ("Incrementally index a Lean repository into local SQLite FTS. Use mode='full' to rebuild.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"mode":{"type":"string"}}}),
 "index_status": ("Return local Lean index status, staleness, and cache size.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "cache_status": ("List cache databases or inspect one project's cache status.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "remove_project": ("Delete one indexed project cache database. Requires project or repo_path unless allow_default is true.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"allow_default":{"type":"boolean"}}}),
 "list_projects": ("List configured local Lean projects.", {"type":"object","properties":{}}),
 "search_graph": ("Search Lean declarations by FTS query, name_pattern, qn_pattern, file_pattern, or label.", {"type":"object","properties":{"query":{"type":"string"},"name_pattern":{"type":"string"},"qn_pattern":{"type":"string"},"file_pattern":{"type":"string"},"label":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "search_theorems": ("Lean-aware theorem-like search over theorem/lemma/abbrev names, conclusions, premises, and head symbols.", {"type":"object","properties":{"query":{"type":"string"},"conclusion":{"type":"string"},"premise":{"type":"string"},"symbol":{"type":"string"},"name_pattern":{"type":"string"},"qn_pattern":{"type":"string"},"file_pattern":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "search_code": ("Search raw Lean source and return declaration-aware hits.", {"type":"object","properties":{"pattern":{"type":"string"},"query":{"type":"string"},"regex":{"type":"boolean"},"file_pattern":{"type":"string"},"path_filter":{"type":"string"},"mode":{"type":"string"},"context":{"type":"integer"},"limit":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "get_code_snippet": ("Read source for an indexed Lean declaration.", {"type":"object","properties":{"qualified_name":{"type":"string"},"name":{"type":"string"},"include_neighbors":{"type":"boolean"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "get_context": ("Return proof-oriented Lean context for one declaration: imports, local variables, theorem profile, neighbors, and source context.", {"type":"object","properties":{"qualified_name":{"type":"string"},"name":{"type":"string"},"before":{"type":"integer"},"after":{"type":"integer"},"neighbor_radius":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "trace_path": ("Best-effort identifier-text inbound/outbound trace.", {"type":"object","properties":{"function_name":{"type":"string"},"qualified_name":{"type":"string"},"direction":{"type":"string"},"depth":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "get_architecture": ("Return declaration counts, imports, and index metadata.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"}}}),
}

def call_tool(name, args, default_repo):
    if name == "list_projects": return {"projects": indexed_projects(default_repo)}
    if name == "cache_status": return cache_status(args, default_repo)
    if name == "remove_project": return remove_project(args, default_repo)
    repo = repo_arg(args, default_repo)
    if name == "index_repository": return index_repository(args, repo)
    if name == "index_status": return index_status(args, repo)
    if name == "search_graph": return search_graph(args, repo)
    if name == "search_theorems": return search_theorems(args, repo)
    if name == "search_code": return search_code(args, repo)
    if name == "get_code_snippet": return get_code_snippet(args, repo)
    if name == "get_context": return get_context(args, repo)
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
            write({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":ver,"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":SERVER_NAME,"version":SERVER_VERSION},"instructions":"Use index_repository first. search_theorems for theorem/lemma discovery, search_graph for declarations, search_code for raw text, get_context for proof-local context."}})
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
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--search")
    ns = ap.parse_args(); repo = Path(ns.repo).resolve()
    if ns.index: print(json.dumps(index_repository({"mode":"full" if ns.full else "incremental"}, repo), ensure_ascii=False, indent=2)); return 0
    if ns.status: print(json.dumps(index_status({}, repo), ensure_ascii=False, indent=2)); return 0
    if ns.search: print(json.dumps(search_graph({"query":ns.search}, repo), ensure_ascii=False, indent=2)); return 0
    serve(repo); return 0

if __name__ == "__main__":
    raise SystemExit(main())
