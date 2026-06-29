#!/usr/bin/env python
"""Tiny local Lean search MCP server: tree-sitter + SQLite FTS5, no models."""
from __future__ import annotations
import argparse, fnmatch, json, os, re, sqlite3, subprocess, sys, time
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
TOPIC_PATHS = {
    "matrix": ["Mathlib/Analysis/Matrix", "Mathlib/LinearAlgebra/Matrix", "Mathlib/Data/Matrix"],
    "cfc": ["Mathlib/Analysis/CStarAlgebra/ContinuousFunctionalCalculus"],
    "intervalintegral": ["Mathlib/MeasureTheory/Integral/IntervalIntegral"],
    "interval_integral": ["Mathlib/MeasureTheory/Integral/IntervalIntegral"],
    "measuretheory": ["Mathlib/MeasureTheory"],
    "measure_theory": ["Mathlib/MeasureTheory"],
    "specialfunctions": ["Mathlib/Analysis/SpecialFunctions"],
    "special_functions": ["Mathlib/Analysis/SpecialFunctions"],
    "resolvent": ["Mathlib/Analysis/Matrix", "Mathlib/LinearAlgebra/Matrix", "Mathlib/Analysis/CStarAlgebra"],
}
DEFAULT_CROSS_PROJECTS = ["provider", "highdimprob", "mathlib"]
DECL_RE = re.compile(r"(?m)^\s*(?:@[^\n]*\n\s*)*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+)*(theorem|lemma|def|abbrev|instance|structure|class|inductive|axiom|opaque|constant|example)\b\s*([^\s:(\[{]+)?")

def log(msg):
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


def lean_ident_char(ch, first=False):
    return ch == "_" or ch.isalpha() or (not first and (ch.isdigit() or ch in ".'`"))


def lean_identifiers(text):
    text = str(text or "")
    out = []
    i = 0
    while i < len(text):
        if not lean_ident_char(text[i], first=True):
            i += 1
            continue
        j = i + 1
        while j < len(text) and lean_ident_char(text[j]):
            j += 1
        token = text[i:j].strip(".")
        if token:
            out.append(token)
        i = j
    return out


def split_decl_names(raw_names):
    return [name for name in raw_names.replace(",", " ").split() if name and name != "_"]


def import_modules(content):
    modules = []
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            modules.extend(part for part in stripped[len("import "):].split() if part)
    return modules


def scoped_keyword_name(line, keyword, allow_unnamed=False):
    stripped = line.strip()
    if not stripped or stripped.startswith("--"):
        return None
    if stripped == keyword:
        return "" if allow_unnamed else None
    prefix = keyword + " "
    if not stripped.startswith(prefix):
        return None
    rest = stripped[len(prefix):].strip()
    if not rest or rest.startswith("--"):
        return "" if allow_unnamed else None
    return rest.split()[0]


def namespace_line(line):
    return scoped_keyword_name(line, "namespace") or ""


def section_line(line):
    return scoped_keyword_name(line, "section", allow_unnamed=True)


def end_line_name(line):
    return scoped_keyword_name(line, "end", allow_unnamed=True)


def is_end_line(line):
    return end_line_name(line) is not None

DECL_MODIFIERS = {"private", "protected", "noncomputable", "unsafe", "partial"}


def decl_kind_name_from_header(src, fallback_kind, fallback_name):
    for line in str(src or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("@["):
            continue
        tokens = stripped.replace("(", " ").replace("{", " ").replace("[", " ").split()
        for i, tok in enumerate(tokens):
            if tok in DECL_MODIFIERS:
                continue
            if tok in KINDS:
                name = fallback_name or "_anonymous"
                if tok != "example" and i + 1 < len(tokens):
                    candidate = tokens[i + 1].strip()
                    if candidate and candidate[0] not in ":=({[":
                        name = candidate
                return tok, name
    return fallback_kind, fallback_name or "_anonymous"


def declaration_signature_rest(stmt, kind, name):
    stmt = str(stmt or "")
    lines = stmt.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("@["):
            continue
        tokens = stripped.split()
        if kind not in tokens:
            return stmt.strip()
        kind_pos = line.find(kind)
        if kind_pos < 0:
            return stmt.strip()
        after = line[kind_pos + len(kind):]
        if name and name != "_anonymous":
            stripped_after = after.lstrip()
            if stripped_after.startswith(name):
                after = stripped_after[len(name):]
        rest_lines = [after.strip()]
        rest_lines.extend(lines[idx + 1:])
        return "\n".join(rest_lines).strip()
    return stmt.strip()


def bracket_groups(text, opener="[", closer="]", limit=20):
    groups = []
    depth = 0
    start = None
    in_string = False
    escaped = False
    for i, ch in enumerate(str(text or "")):
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
            continue
        if ch == opener:
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == closer and depth:
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(text[start:i].strip())
                if len(groups) >= limit:
                    break
    return groups

def local_mathlib_roots():
    base = SERVER_REPO.parent
    roots = [
        ("provider-mathlib", base / "HighDimProbLiebProvider" / ".lake" / "packages" / "mathlib"),
        ("highdimprob-mathlib", base / "HighDimProb" / ".lake" / "packages" / "mathlib"),
    ]
    return [(name, path.resolve()) for name, path in roots if path.exists()]


def default_mathlib_root():
    roots = local_mathlib_roots()
    return roots[0][1] if roots else None


def local_repo_aliases():
    base = SERVER_REPO.parent
    aliases = {
        "provider": base / "HighDimProbLiebProvider",
        "HighDimProbLiebProvider": base / "HighDimProbLiebProvider",
        "highdimprob": base / "HighDimProb",
        "HighDimProb": base / "HighDimProb",
        "main": base / "HighDimProb",
    }
    mathlib = default_mathlib_root()
    if mathlib:
        aliases["mathlib"] = mathlib
        aliases["Mathlib"] = mathlib
    for name, path in local_mathlib_roots():
        aliases[name] = path
    return aliases


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
    if project and not raw:
        aliased = alias_repo(project)
        if aliased:
            return db_path(aliased).resolve()
    if project and not raw and not (":" in str(project) or "\\" in str(project) or "/" in str(project)):
        candidate = index_root() / (str(project) + ".sqlite3")
        if candidate.exists():
            return candidate.resolve()
    return db_path(repo_arg(args, fallback)).resolve()

def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "\n" in text:
        return [x.strip() for x in text.splitlines() if x.strip()]
    return [x.strip() for x in text.split(",") if x.strip()] if "," in text else [text]


def norm_rel(value):
    return str(value or "").replace("\\", "/").lstrip("./")


def topic_prefixes(args):
    prefixes = []
    for topic in as_list(args.get("topic") or args.get("topics")):
        prefixes.extend(TOPIC_PATHS.get(topic.lower(), [topic]))
    return [norm_rel(x).rstrip("/") for x in prefixes if str(x).strip()]


def scope_prefixes(args):
    prefixes = []
    prefixes.extend(as_list(args.get("path_prefix") or args.get("prefix")))
    prefixes.extend(as_list(args.get("paths")))
    prefixes.extend(topic_prefixes(args))
    return [norm_rel(x).rstrip("/") for x in prefixes if str(x).strip()]


def has_scope(args):
    return bool(args and (args.get("file_pattern") or args.get("path_filter") or args.get("path_prefix") or args.get("prefix") or args.get("paths") or args.get("topic") or args.get("topics")))


def file_in_scope(rel, args):
    if not args:
        return True
    rel = norm_rel(rel)
    file_pat = args.get("file_pattern")
    if file_pat and not fnmatch.fnmatch(rel, str(file_pat)):
        return False
    path_filter = args.get("path_filter")
    if path_filter and not re.search(str(path_filter), rel):
        return False
    prefixes = scope_prefixes(args)
    if prefixes:
        ok = False
        for prefix in prefixes:
            if rel == prefix or rel.startswith(prefix + "/") or fnmatch.fnmatch(rel, prefix):
                ok = True
                break
        if not ok:
            return False
    return True


def scope_description(args):
    return {"topics": as_list(args.get("topic") or args.get("topics")), "prefixes": scope_prefixes(args), "file_pattern": args.get("file_pattern"), "path_filter": args.get("path_filter")}


def indexed_files(repo, args=None):
    out = []
    for p in repo.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        rel_parts = p.relative_to(repo).parts
        if any(part in EXCLUDES for part in rel_parts):
            continue
        rel = Path(*rel_parts).as_posix()
        if not file_in_scope(rel, args or {}):
            continue
        out.append(p)
    return sorted(out)


def lean_files(repo):
    return indexed_files(repo)
def line_of(data, byte):
    return data[:max(0, byte)].count(b"\n") + 1

def close_scope(stack, name):
    if not stack:
        return
    if name:
        for idx in range(len(stack) - 1, -1, -1):
            scope_name = stack[idx][1]
            if scope_name == name or scope_name.split(".")[-1] == name:
                del stack[idx:]
                return
        return
    stack.pop()


def namespace_at(lines, line_no):
    stack = []
    for line in lines[:max(0, line_no - 1)]:
        ns = namespace_line(line)
        if ns:
            stack.append(("namespace", ns))
            continue
        section = section_line(line)
        if section is not None:
            stack.append(("section", section))
            continue
        ended = end_line_name(line)
        if ended is not None:
            close_scope(stack, ended)
    names = [name for kind, name in stack if kind == "namespace" and name and name != "_root_"]
    return ".".join(names)

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

def decl_kind_name(src, fallback_kind, fallback_name):
    return decl_kind_name_from_header(src, fallback_kind, fallback_name)

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
    for ident in lean_identifiers(text):
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
    rest = declaration_signature_rest(stmt, kind, name)
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
        names = split_decl_names(raw_names)
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
        kind, name = decl_kind_name(src, item["kind"], item["name"])
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
            nimports += len(import_modules(r["content"]))
    return nimports

def stale_summary(repo, con, args=None, details=False):
    current = {}
    args = args or {}
    if Path(repo).exists():
        for p in indexed_files(repo, args):
            st = p.stat()
            current[p.relative_to(repo).as_posix()] = (st.st_mtime_ns, st.st_size)
    indexed_all = {r["path"]: (r["mtime_ns"], r["size"]) for r in con.execute("SELECT path,mtime_ns,size FROM files")}
    indexed = {rel: stamp for rel, stamp in indexed_all.items() if file_in_scope(rel, args)}
    removed = sorted(set(indexed) - set(current))
    added = sorted(set(current) - set(indexed))
    changed = sorted(rel for rel in set(current) & set(indexed) if current[rel] != indexed[rel])
    out = {"added_files": len(added), "changed_files": len(changed), "removed_files": len(removed), "is_stale": bool(added or changed or removed)}
    if details:
        limit = int(args.get("detail_limit") or args.get("limit") or 30)
        out.update({"scope": scope_description(args), "added": added[:limit], "changed": changed[:limit], "removed": removed[:limit], "current_scope_files": len(current), "indexed_scope_files": len(indexed)})
    return out


def background_index(args, repo):
    bg_args = dict(args)
    bg_args.pop("background", None)
    index_root().mkdir(parents=True, exist_ok=True)
    log_path = index_root() / (project_name(repo) + ".index.log")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--repo", str(repo), "--index-args", json.dumps(bg_args, ensure_ascii=False)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    out = open(log_path, "ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(repo), stdout=out, stderr=out, creationflags=flags)
    finally:
        out.close()
    return {"background": True, "pid": proc.pid, "project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "log_path": str(log_path), "args": bg_args}


def index_repository(args, repo):
    if args.get("background"):
        return background_index(args, repo)
    t0 = time.time(); con = connect(repo)
    mode = str(args.get("mode") or "incremental").lower()
    resume = mode in {"resume", "continue"} or bool(args.get("resume"))
    batch_size = max(1, int(args.get("batch_size") or 200))
    current_paths = indexed_files(repo, args)
    current = {}
    for p in current_paths:
        st = p.stat()
        current[p.relative_to(repo).as_posix()] = {"path": p, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    meta_indexed = con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    meta_schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    scoped = has_scope(args)
    full_requested = mode in {"full", "rebuild", "force"}
    full = (full_requested and not resume) or meta_indexed is None or (meta_schema is None or meta_schema["value"] != SCHEMA_VERSION)
    indexed = {r["path"]: (r["mtime_ns"], r["size"]) for r in con.execute("SELECT path,mtime_ns,size FROM files")}
    changed_rels, removed_rels = [], []
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('repo_path', ?)", (str(repo),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('index_phase', 'running')")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('index_started_at', ?)", (str(int(time.time())),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('index_scope', ?)", (json.dumps(scope_description(args), ensure_ascii=False),))
        if full and not scoped:
            con.execute("DELETE FROM decl_fts"); con.execute("DELETE FROM decls"); con.execute("DELETE FROM files")
            changed_rels = sorted(current)
        elif full and scoped:
            indexed_in_scope = {rel for rel in indexed if file_in_scope(rel, args)}
            removed_rels = sorted(indexed_in_scope - set(current))
            changed_rels = sorted(current)
            for rel in removed_rels + changed_rels:
                delete_file_index(con, rel)
        else:
            indexed_in_scope = {rel: stamp for rel, stamp in indexed.items() if file_in_scope(rel, args)}
            removed_rels = sorted(set(indexed_in_scope) - set(current))
            changed_rels = sorted(rel for rel, st in current.items() if rel not in indexed_in_scope or indexed_in_scope[rel] != (st["mtime_ns"], st["size"]))
            for rel in removed_rels + changed_rels:
                delete_file_index(con, rel)
    seen_qn = set(r["qn"] for r in con.execute("SELECT qn FROM decls"))
    nchanged_decls = 0
    processed = 0
    pending = 0
    for rel in changed_rels:
        if pending == 0:
            con.execute("BEGIN")
        nchanged_decls += insert_file_index(con, repo, current[rel]["path"], seen_qn)
        processed += 1; pending += 1
        if pending >= batch_size:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('last_index_progress', ?)", (json.dumps({"processed": processed, "total": len(changed_rels), "last_file": rel, "time": int(time.time())}, ensure_ascii=False),))
            con.commit(); pending = 0
    if pending:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_index_progress', ?)", (json.dumps({"processed": processed, "total": len(changed_rels), "last_file": changed_rels[-1] if changed_rels else None, "time": int(time.time())}, ensure_ascii=False),))
        con.commit()
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('indexed_at', ?)", (str(int(time.time())),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('index_phase', 'ready')")
    result = {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "mode": "full" if full and not resume else "resume" if resume else "incremental", "scope": scope_description(args), "indexed_files": table_count(con, "files"), "declarations": table_count(con, "decls"), "imports": total_imports(con), "changed_files": len(changed_rels), "changed_declarations": nchanged_decls, "removed_files": len(removed_rels), "skipped_files": max(0, len(current) - len(changed_rels)), "batch_size": batch_size, "elapsed_ms": round((time.time() - t0) * 1000)}
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


def module_name_from_file(rel):
    rel = norm_rel(rel)
    return rel[:-5].replace("/", ".") if rel.endswith(".lean") else ""


def import_for_file(rel):
    return module_name_from_file(rel)


def header_lines_before(con, r, limit=40):
    fr = con.execute("SELECT content FROM files WHERE path=?", (r["file"],)).fetchone()
    lines = fr["content"].splitlines() if fr else []
    header_lines = []
    for i, line in enumerate(lines[:max(0, r["start_line"] - 1)], 1):
        stripped = line.strip()
        if stripped.startswith(("open ", "variable ", "variables ", "section ", "namespace ", "local ", "attribute ")):
            header_lines.append({"line": i, "text": line})
    return header_lines[-limit:]


def key_typeclasses(stmt, header_lines):
    text = "\n".join([stmt or ""] + [h.get("text", "") for h in header_lines])
    seen, out = set(), []
    for item in bracket_groups(text):
        clean = " ".join(item.strip("[]").split())
        if clean and clean not in seen:
            seen.add(clean); out.append(clean)
        if len(out) >= 20:
            break
    return out


def theorem_card(repo, con, r, include_source=False):
    header = header_lines_before(con, r)
    import_module = import_for_file(r["file"])
    card = theorem_result(r)
    card.update({
        "import": import_module,
        "source_location": f"{Path(repo) / r['file']}:{r['start_line']}",
        "namespace": r["namespace"] or "",
        "docstring": r["doc"] or "",
        "binders": json.loads(r["binders_json"] or "[]"),
        "key_typeclasses": key_typeclasses(r["stmt"], header),
        "minimal_check": f"import {import_module}\n#check {r['qn']}",
        "local_header": header,
    })
    if include_source:
        card["source"] = r["src"]
    return card


SUGGEST_COLUMNS = "qn qualified_name,name,kind,file file_path,start_line"


def compact_decl_key(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def decl_suggestions(con, q, limit=10, kinds=None):
    q = str(q or "").strip()
    if not q:
        return []
    limit = max(1, int(limit or 10))
    kinds = tuple(kinds or [])
    kind_clause = ""
    kind_params = ()
    if kinds:
        kind_clause = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        kind_params = kinds
    seen = set()
    ranked = []

    def add(rows, score, reason):
        for row in rows:
            item = dict(row)
            key = item.get("qualified_name") or item.get("qn")
            if not key or key in seen:
                continue
            seen.add(key)
            item["suggestion_reason"] = reason
            item["suggestion_score"] = score
            ranked.append(item)

    def fetch(where, params, score, reason, order="length(qn), file, start_line", cap=None):
        sql = f"SELECT {SUGGEST_COLUMNS} FROM decls WHERE {where}{kind_clause} ORDER BY {order} LIMIT ?"
        rows = con.execute(sql, tuple(params) + kind_params + (cap or limit,)).fetchall()
        add(rows, score, reason)

    leaf = q.split(".")[-1]
    leaf_compact = compact_decl_key(leaf)

    fetch("qn=? OR name=?", (q, q), 1200, "exact_qn_or_name")
    if len(ranked) < limit and leaf and leaf != q:
        fetch("name=?", (leaf,), 1050, "same_leaf_name")
    if len(ranked) < limit and leaf and leaf != q:
        fetch("qn LIKE ?", (f"%.{leaf}",), 950, "same_qn_suffix")
    if len(ranked) < limit and leaf_compact:
        parts = [p for p in leaf.replace("'", "_").split("_") if len(p) > 2]
        if parts:
            placeholders = ",".join("?" for _ in parts)
            fetch(f"name IN ({placeholders})", tuple(parts), 650, "name_part_index", cap=max(limit, len(parts) * 3))

    if len(ranked) < limit:
        toks = lean_identifiers(q)
        if toks:
            try:
                sql = "SELECT d.qn qualified_name,d.name,d.kind,d.file file_path,d.start_line,bm25(decl_fts) rank FROM decl_fts JOIN decls d ON d.id=decl_fts.rowid WHERE decl_fts MATCH ?"
                if kinds:
                    sql += " AND d.kind IN (" + ",".join("?" for _ in kinds) + ")"
                sql += " ORDER BY rank LIMIT ?"
                rows = con.execute(sql, (fts(q),) + kind_params + (limit,)).fetchall()
                add(rows, 420, "fts_fallback")
            except sqlite3.Error:
                pass
    if len(ranked) < limit:
        like = f"%{leaf or q}%"
        fetch("qn LIKE ? OR name LIKE ?", (like, like), 300, "substring_fallback")
    ranked.sort(key=lambda item: (-item.get("suggestion_score", 0), len(item.get("qualified_name", "")), item.get("file_path", ""), item.get("start_line", 0)))
    return ranked[:limit]


def suggestion_response(con, q, limit=10, kinds=None):
    items = decl_suggestions(con, q, max(limit * 3, limit + 10), kinds)
    strong = [item for item in items if item.get("suggestion_score", 0) >= 900]
    fallback = [item for item in items if item.get("suggestion_score", 0) < 900]
    if strong:
        primary = strong[:limit]
    else:
        primary = fallback[:limit]
        fallback = fallback[limit:]
    out = {
        "found": False,
        "suggestions": primary,
        "suggestion_policy": "exact/name/suffix suggestions are primary; weaker part/FTS/substring matches are returned separately when present",
    }
    if fallback:
        out["fallback_suggestions"] = fallback[:limit]
    return out


def fts(q):
    toks = lean_identifiers(q)
    return " OR ".join(f'"{t}"' for t in toks) if toks else str(q).replace('"', '""')

def query_tokens(value):
    return [t.lower() for t in lean_identifiers(value)]


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
    query_shape_terms = shape_terms(args.get("query"))
    conclusion_shape_terms = shape_terms(args.get("conclusion"))
    symbol_shape_terms = shape_terms(args.get("symbol") or args.get("symbols"))
    name_pat, qn_pat, file_pat = args.get("name_pattern"), args.get("qn_pattern"), args.get("file_pattern")
    rows = list(con.execute("SELECT * FROM decls WHERE kind IN ('theorem','lemma','abbrev')"))
    scored = []
    for r in rows:
        if file_pat and not fnmatch.fnmatch(r["file"], str(file_pat)): continue
        if not file_in_scope(r["file"], args): continue
        if name_pat and not re.search(str(name_pat), r["name"]): continue
        if qn_pat and not re.search(str(qn_pat), r["qn"]): continue
        short_name = r["name"] or ""
        qn_text = r["qn"] or ""
        name_text = f"{short_name} {qn_text}"
        conclusion_text = r["conclusion"] or ""
        premise_text = r["premises"] or ""
        head_text = r["head_symbols"] or ""
        stmt_text = r["stmt"] or ""
        theorem_text = "\n".join([conclusion_text, premise_text, head_text, stmt_text])
        if toks and not contains_any(name_text + "\n" + theorem_text, toks): continue
        if conclusion_toks and not contains_any(conclusion_text + "\n" + stmt_text, conclusion_toks): continue
        if premise_toks and not contains_any(premise_text + "\n" + stmt_text, premise_toks): continue
        if symbol_toks and not contains_any("\n".join([head_text, conclusion_text, qn_text, stmt_text]), symbol_toks): continue
        score = 0
        shape_overlap = []
        if toks:
            compact = "".join(toks)
            short_lower = short_name.lower()
            if compact and short_lower.startswith(compact): score += 260
            elif compact and compact in short_lower: score += 220
            elif contains_all(short_name, toks): score += 180
            elif contains_any(short_name, toks): score += 120
            elif contains_all(qn_text, toks): score += 80
            elif contains_any(qn_text, toks): score += 45
            if contains_all(conclusion_text, toks): score += 70
            elif contains_any(conclusion_text, toks): score += 45
            if contains_any(head_text, toks): score += 35
            if contains_any(premise_text, toks): score += 20
            if contains_any(stmt_text, toks): score += 10
        if query_shape_terms:
            shape_bonus, _, shape_overlap = shape_score(query_shape_terms, r, False)
            qset = set(query_shape_terms)
            score += shape_bonus * 3
            score += 75 * len(text_term_hits(qset, head_text))
            score += 55 * len(text_term_hits(qset, conclusion_text))
            score += 20 * len(text_term_hits(qset, qn_text))
            if len(set(shape_overlap)) >= min(3, len(qset)):
                score += 90
        if conclusion_shape_terms:
            shape_bonus, _, overlap = shape_score(conclusion_shape_terms, r, False)
            cset = set(conclusion_shape_terms)
            score += 180 + shape_bonus * 4
            score += 90 * len(text_term_hits(cset, conclusion_text))
            score += 35 * len(text_term_hits(cset, head_text))
            if overlap:
                shape_overlap = list(dict.fromkeys(shape_overlap + overlap))
        if premise_toks: score += 25
        if symbol_toks or symbol_shape_terms:
            symbol_terms = symbol_shape_terms or symbol_toks
            sset = set(symbol_terms)
            symbol_text = "\n".join([head_text, qn_text, conclusion_text])
            hits = text_term_hits(sset, symbol_text)
            if symbol_toks and not hits:
                continue
            score += 120 + 140 * len(hits)
            if sset and all(t in hits for t in sset):
                score += 100
        if conclusion_toks: score += 60
        score -= generic_theorem_penalty(short_name, qn_text, query_shape_terms + conclusion_shape_terms + symbol_shape_terms, shape_overlap)
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["kind"] != "theorem", x[1]["file"], x[1]["start_line"]))
    filt = [r for _, r in scored]
    page = filt[offset:offset+limit]
    use_cards = bool(args.get("cards") or args.get("card"))
    results = [theorem_card(repo, con, r, bool(args.get("include_source"))) if use_cards else theorem_result(r) for r in page]
    con.close()
    return {"total": len(filt), "results": results, "has_more": offset + len(page) < len(filt)}
SHAPE_STOP = STOP_SYMBOLS | {"fun", "Function", "Set", "by", "exact", "rw", "simp", "the", "if", "then", "else"}
SHAPE_OPS = [("⁻¹", "inv"), ("•", "smul"), ("=", "eq"), ("≤", "le"), ("<=", "le"), ("≥", "ge"), (">=", "ge"), ("∈", "mem"), ("+", "add"), ("-", "sub"), ("*", "mul"), ("/", "div"), ("∫", "integral"), ("HasDerivAt", "HasDerivAt"), ("Matrix.trace", "Matrix.trace"), ("trace", "trace"), ("cfc", "cfc"), ("intervalIntegral", "intervalIntegral"), ("Real.log", "Real.log")]
IMPORTANT_SHAPE_TERMS = {"cfc", "trace", "hasderivat", "inv", "smul", "real.log", "intervalintegral", "integral"}


def shape_terms(text):
    text = str(text or "")
    terms = []
    for needle, token in SHAPE_OPS:
        if needle in text:
            terms.append(token.lower())
    for ident in lean_identifiers(text):
        short = ident.split(".")[-1]
        if ident in SHAPE_STOP or short in SHAPE_STOP:
            continue
        if len(short) == 1:
            continue
        ident_lower = ident.lower()
        short_lower = short.lower()
        terms.append(ident_lower)
        if "." in ident:
            terms.append(short_lower)
        for part in short_lower.replace("'", "_").split("_"):
            if len(part) > 1 and part not in SHAPE_STOP:
                terms.append(part)
    seen, out = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out


def ordered_hits(query_terms, candidate_terms):
    pos, hits = 0, 0
    for qt in query_terms:
        try:
            idx = candidate_terms.index(qt, pos)
        except ValueError:
            continue
        hits += 1; pos = idx + 1
    return hits


def shape_score(query_terms, row, require_important=True):
    text = "\n".join([row["stmt"] or "", row["conclusion"] or "", row["premises"] or "", row["head_symbols"] or "", row["qn"] or ""])
    cand = shape_terms(text)
    if not query_terms or not cand:
        return 0, cand, []
    required = [t for t in query_terms if t in IMPORTANT_SHAPE_TERMS]
    if require_important and any(t not in cand for t in required):
        return 0, cand, []
    overlap = [t for t in query_terms if t in cand]
    if not overlap:
        return 0, cand, []
    score = 20 * len(set(overlap)) + 6 * ordered_hits(query_terms, cand)
    if all(t in cand for t in query_terms):
        score += 80
    for a, b in zip(query_terms, query_terms[1:]):
        if a in cand and b in cand and cand.index(a) < cand.index(b):
            score += 8
    if row["kind"] == "theorem":
        score += 5
    return score, cand, overlap


GENERIC_RESULT_NAMES = {"add", "sub", "neg", "map", "smul", "mul", "zero", "one", "eq", "ne", "coe", "transpose", "conjtranspose", "star", "apply"}
GENERIC_SHAPE_TERMS = {"eq", "iff", "of", "to", "is", "has", "map", "add", "sub", "mul", "one", "zero", "theorem", "lemma"}


def text_term_hits(terms, text):
    lower = str(text or "").lower()
    return [t for t in terms if t and t.lower() in lower]


def strong_shape_terms(terms):
    return [t for t in terms if len(t) > 2 and t.lower() not in GENERIC_SHAPE_TERMS]


def generic_theorem_penalty(short_name, qn_text, query_terms, overlap):
    strong = set(strong_shape_terms(query_terms))
    if len(strong) < 3:
        return 0
    leaf = (short_name or "").split(".")[-1].lower()
    leaf_parts = {p for p in leaf.replace("'", "_").split("_") if p}
    matched = set(overlap or []) & strong
    qn_lower = (qn_text or "").lower()
    if leaf in GENERIC_RESULT_NAMES and len(matched) < 2:
        return 180
    if "ishermitian" in qn_lower and leaf in {"add", "sub", "map", "neg", "smul", "mul"} and len(matched) < 2:
        return 220
    if leaf_parts and leaf_parts <= GENERIC_RESULT_NAMES and len(matched) < 2:
        return 160
    return 0

def search_shape(args, repo):
    ensure_index(repo); con = connect(repo)
    shape = args.get("shape") or args.get("query") or args.get("type") or ""
    qterms = shape_terms(shape)
    limit = int(args.get("limit") or 20); offset = int(args.get("offset") or 0)
    min_score = int(args.get("min_score") or 1)
    file_pat = args.get("file_pattern")
    rows = list(con.execute("SELECT * FROM decls WHERE kind IN ('theorem','lemma','abbrev')"))
    scored = []
    for r in rows:
        if file_pat and not fnmatch.fnmatch(r["file"], str(file_pat)): continue
        if not file_in_scope(r["file"], args): continue
        score, cand_terms, overlap = shape_score(qterms, r)
        if score < min_score:
            continue
        scored.append((score, overlap, cand_terms, r))
    relaxed = False
    if not scored and not args.get("strict"):
        relaxed = True
        for r in rows:
            if file_pat and not fnmatch.fnmatch(r["file"], str(file_pat)): continue
            if not file_in_scope(r["file"], args): continue
            score, cand_terms, overlap = shape_score(qterms, r, False)
            if score < min_score:
                continue
            scored.append((score, overlap, cand_terms, r))
    scored.sort(key=lambda x: (-x[0], x[3]["kind"] != "theorem", x[3]["file"], x[3]["start_line"]))
    page = scored[offset:offset+limit]
    results = []
    for score, overlap, cand_terms, r in page:
        item = theorem_card(repo, con, r, bool(args.get("include_source"))) if args.get("cards", True) else theorem_result(r)
        item["shape_score"] = score
        item["matched_shape_terms"] = overlap
        item["candidate_shape_terms"] = cand_terms[:40]
        results.append(item)
    con.close()
    return {"shape": shape, "shape_terms": qterms, "relaxed": relaxed, "total": len(scored), "results": results, "has_more": offset + len(page) < len(scored)}

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
        return suggestion_response(con, q, 10)
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
        return suggestion_response(con, q, 10)
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
    imports = import_modules(fr["content"] if fr else "")
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
        imports.update(import_modules(r["content"]))
    return {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db_path(repo)), "files": files, "declarations": decls, "kinds": kinds, "imports": sorted(imports)}

def index_status(args, repo):
    con = connect(repo); meta = con.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    schema = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    phase = con.execute("SELECT value FROM meta WHERE key='index_phase'").fetchone()
    progress = con.execute("SELECT value FROM meta WHERE key='last_index_progress'").fetchone()
    scope = con.execute("SELECT value FROM meta WHERE key='index_scope'").fetchone()
    db = db_path(repo)
    if phase and phase["value"] == "running":
        status = "indexing_or_interrupted"
    else:
        status = "ready" if meta and schema and schema["value"] == SCHEMA_VERSION else "not_indexed" if meta is None else "needs_reindex"
    out = {"project": project_name(repo), "repo_path": str(repo), "db_path": str(db), "schema_version": schema["value"] if schema else None, "status": status, "indexed_at": int(meta["value"]) if meta else None, "files": table_count(con, "files"), "declarations": table_count(con, "decls"), "cache_bytes": db.stat().st_size if db.exists() else 0}
    if progress:
        try: out["last_index_progress"] = json.loads(progress["value"])
        except Exception: out["last_index_progress"] = progress["value"]
    if scope:
        try: out["last_index_scope"] = json.loads(scope["value"])
        except Exception: out["last_index_scope"] = scope["value"]
    out.update(stale_summary(repo, con, args, bool(args.get("details"))))
    con.close()
    return out


def git_file_sets(repo):
    repo = Path(repo)
    if not (repo / ".git").exists():
        return {"available": False, "tracked": set(), "untracked": set(), "error": None}
    try:
        tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--cached"], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20)
        untracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"], text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20)
        if tracked.returncode != 0 or untracked.returncode != 0:
            return {"available": False, "tracked": set(), "untracked": set(), "error": (tracked.stderr + untracked.stderr).strip()}
        return {"available": True, "tracked": {norm_rel(x) for x in tracked.stdout.splitlines() if x.strip()}, "untracked": {norm_rel(x) for x in untracked.stdout.splitlines() if x.strip()}, "error": None}
    except Exception as exc:
        return {"available": False, "tracked": set(), "untracked": set(), "error": str(exc)}


def root_module_candidates(repo, con):
    files = {r["path"] for r in con.execute("SELECT path FROM files WHERE path LIKE '%.lean'")}
    roots = []
    for name in [Path(repo).name, "Mathlib", "HighDimProb", "HighDimProbLiebProvider"]:
        rel = f"{name}.lean"
        if rel in files and rel not in roots:
            roots.append(rel)
    if not roots:
        roots.extend(sorted(rel for rel in files if "/" not in rel)[:5])
    return roots


def is_archived_validation_path(rel):
    parts = [p.lower() for p in norm_rel(rel).split("/")]
    markers = {"archive", "archived", "archives", "validation", "validations", "external", "blocked_clean"}
    if any(p in markers for p in parts):
        return True
    return any("validation" in p or "archive" in p or "blocked" in p for p in parts)


def split_unexposed_files(paths):
    archived, source_test = [], []
    for rel in paths:
        if is_archived_validation_path(rel):
            archived.append(rel)
        else:
            source_test.append(rel)
    return source_test, archived


def root_import_visibility(repo, con, args=None):
    args = args or {}
    rows = list(con.execute("SELECT path,content FROM files WHERE path LIKE '%.lean'"))
    content = {r["path"]: r["content"] for r in rows}
    module_to_file = {module_name_from_file(path): path for path in content}
    roots = [norm_rel(x) for x in as_list(args.get("root") or args.get("roots"))] or root_module_candidates(repo, con)
    graph = {}
    for path, body in content.items():
        deps = []
        for mod in import_modules(body or ""):
                rel = module_to_file.get(mod)
                if rel:
                    deps.append(rel)
        graph[path] = deps
    seen, stack = set(), list(roots)
    while stack:
        rel = stack.pop()
        if rel in seen or rel not in graph:
            continue
        seen.add(rel); stack.extend(graph.get(rel, []))
    unexposed = sorted(rel for rel in set(content) - seen if file_in_scope(rel, args))
    source_test, archived = split_unexposed_files(unexposed)
    limit = int(args.get("detail_limit") or 30)
    archived_limit = limit if args.get("details", True) or args.get("include_archived") else min(5, limit)
    return {
        "roots": roots,
        "reachable_files": len(seen),
        "lean_files": len(content),
        "unexposed_files": len(unexposed),
        "source_test_unexposed_files": len(source_test),
        "source_test_unexposed_sample": source_test[:limit],
        "archived_validation_unexposed_files": len(archived),
        "archived_validation_unexposed_sample": archived[:archived_limit],
        "unexposed_sample": source_test[:limit],
        "unexposed_sample_policy": "source/test files first; archived validation files are split separately",
    }

def index_visibility(args, repo):
    con = connect(repo)
    out = index_status(args, repo)
    git_sets = git_file_sets(repo)
    if git_sets["available"]:
        indexed = {norm_rel(r["path"]) for r in con.execute("SELECT path FROM files")}
        current = {p.relative_to(repo).as_posix() for p in indexed_files(repo, args)}
        out["git"] = {"available": True, "indexed_tracked_files": len(indexed & git_sets["tracked"]), "indexed_untracked_files": len(indexed & git_sets["untracked"]), "current_untracked_files": len(current & git_sets["untracked"]), "tracked_only": len(indexed & git_sets["untracked"]) == 0}
        if args.get("details", True):
            limit = int(args.get("detail_limit") or 30)
            out["git"]["indexed_untracked_sample"] = sorted(indexed & git_sets["untracked"])[:limit]
            out["git"]["current_untracked_sample"] = sorted(current & git_sets["untracked"])[:limit]
    else:
        out["git"] = {"available": False, "error": git_sets["error"]}
    out["root_import_visibility"] = root_import_visibility(repo, con, args)
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
    for repo in local_repo_aliases().values():
        if repo.exists():
            add(repo, db_path(repo))
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
        names = {x.split(".")[-1] for x in lean_identifiers(d["src"])}
        if names:
            qs = ",".join("?" for _ in names)
            outbound = [row_result(r) for r in con.execute(f"SELECT * FROM decls WHERE name IN ({qs}) LIMIT 100", tuple(names)) if r["id"] != d["id"]]
    return {"found": True, "mode": "approximate_identifier_text", "target": row_result(d), "inbound": inbound, "outbound": outbound}

def theorem_card_tool(args, repo):
    ensure_index(repo); con = connect(repo)
    q = str(args.get("qualified_name") or args.get("name") or "")
    r, rows = lookup_decl(con, q)
    if r is None and not rows:
        out = suggestion_response(con, q, 10)
        con.close(); return out
    if r is None:
        con.close(); return {"found": False, "ambiguous": True, "suggestions": [dict(x) for x in rows]}
    out = theorem_card(repo, con, r, bool(args.get("include_source")))
    con.close(); return {"found": True, "card": out}


def normalize_imports(value):
    imports = []
    for item in as_list(value):
        for part in item.split():
            part = part.strip()
            if part and part != "import":
                imports.append(part)
    seen, out = set(), []
    for imp in imports:
        if imp not in seen:
            seen.add(imp); out.append(imp)
    return out


def diagnose_lean_output(text):
    lower = str(text or "").lower()
    tags = []
    if "unknown constant" in lower or "unknown identifier" in lower:
        tags.append("missing_import_or_unknown_name")
    if "application type mismatch" in lower or "type mismatch" in lower:
        tags.append("type_mismatch")
    if "failed to synthesize" in lower:
        tags.append("missing_typeclass_or_instance")
    if "unsolved goals" in lower:
        tags.append("unsolved_goals")
    if "invalid field notation" in lower or "coe" in lower:
        tags.append("possible_coe_or_normal_form_issue")
    return tags or (["ok"] if "error:" not in lower else ["lean_error"])


def proof_probe(args, default_repo):
    search_repo = repo_arg(args, default_repo)
    run_args = {}
    if args.get("run_project"):
        run_args["project"] = args.get("run_project")
    if args.get("run_repo_path"):
        run_args["repo_path"] = args.get("run_repo_path")
    run_repo = repo_arg(run_args, default_repo) if run_args else default_repo
    imports = normalize_imports(args.get("imports"))
    checks = as_list(args.get("checks") or args.get("check") or args.get("qualified_name") or args.get("name"))
    ensure_index(search_repo); con = connect(search_repo)
    suggested = []
    for q in checks:
        r, _ = lookup_decl(con, q)
        if r is not None:
            suggested.append(import_for_file(r["file"]))
    con.close()
    imports = normalize_imports(imports + suggested)
    lines = [f"import {imp}" for imp in imports]
    verbose = bool(args.get("verbose") or args.get("full_type") or args.get("pp_all"))
    lines.append("set_option maxHeartbeats 400000")
    if verbose:
        lines.extend(["set_option pp.all true", "set_option pp.universes true", "set_option pp.explicit true"])
    else:
        lines.extend(["set_option pp.all false", "set_option pp.universes false", "set_option pp.explicit false"])
    for q in checks:
        lines.append(f"#check {q}")
    if args.get("code"):
        lines.append(str(args["code"]))
    if args.get("goal"):
        proof = str(args.get("proof") or "by\n  sorry")
        lines.append(f"example : {args['goal']} := {proof}")
    if args.get("example"):
        lines.append(str(args["example"]))
    probe_dir = index_root() / "probes" / project_name(run_repo)
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_file = probe_dir / f"Probe_{int(time.time() * 1000)}.lean"
    probe_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = ["lake", "env", "lean", str(probe_file)]
    timeout = int(args.get("timeout_sec") or 180)
    try:
        proc = subprocess.run(cmd, cwd=str(run_repo), text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "run_repo": str(run_repo), "probe_file": str(probe_file), "imports": imports, "checks": checks, "verbose": verbose, "timeout_sec": timeout, "command": " ".join(cmd), "diagnosis": diagnose_lean_output(output), "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "exit_code": None, "run_repo": str(run_repo), "probe_file": str(probe_file), "imports": imports, "checks": checks, "verbose": verbose, "timeout_sec": timeout, "command": " ".join(cmd), "diagnosis": ["timeout"], "message": f"lake env lean exceeded timeout_sec={timeout}; increase timeout_sec for heavy Mathlib imports or use narrower imports.", "stdout": exc.stdout, "stderr": exc.stderr}


def configured_repos(default_repo, names=None):
    repos, seen = [], set()
    for name in (names or DEFAULT_CROSS_PROJECTS):
        repo = alias_repo(name) or (default_repo if name == "default" else None)
        if repo and repo.exists() and str(repo).lower() not in seen:
            seen.add(str(repo).lower()); repos.append((name, repo))
    return repos


def cross_repo_lookup(args, default_repo):
    q = str(args.get("query") or args.get("name") or args.get("qualified_name") or "")
    short = q.split(".")[-1]
    projects = as_list(args.get("projects")) or DEFAULT_CROSS_PROJECTS
    limit = int(args.get("limit") or 20)
    out = {"query": q, "projects": []}
    for label, repo in configured_repos(default_repo, projects):
        item = {"project": label, "repo_path": str(repo), "indexed": db_path(repo).exists()}
        if not item["indexed"]:
            out["projects"].append(item); continue
        con = connect(repo)
        decls = [dict(x) for x in con.execute("SELECT qn qualified_name,name,kind,file file_path,start_line FROM decls WHERE qn=? OR name=? OR qn LIKE ? OR name LIKE ? ORDER BY length(qn) LIMIT ?", (q, q, f"%{q}%", f"%{q}%", limit))]
        users = [dict(x) for x in con.execute("SELECT qn qualified_name,name,kind,file file_path,start_line FROM decls WHERE src LIKE ? AND qn NOT LIKE ? ORDER BY file,start_line LIMIT ?", (f"%{short}%", f"%{q}%", limit))] if short else []
        item.update({"declarations": decls, "users": users})
        con.close(); out["projects"].append(item)
    provider_hits = [p for p in out["projects"] if p["project"] == "provider" and p.get("declarations")]
    main_hits = [p for p in out["projects"] if p["project"] in {"highdimprob", "main"} and p.get("declarations")]
    out["migration_hint"] = "same-name declaration appears in provider and HighDimProb" if provider_hits and main_hits else "no same-name provider/main pair found"
    return out


CONSUMER_FIT_CLASSES = ["exact_duplicate", "migrated_copy", "prerequisite_leaf", "downstream_consumer", "unrelated_shape_match"]


def compact_statement(text):
    return " ".join(str(text or "").split())


def classify_consumer_candidate(target_row, target_card, target_project, project_label, cr, overlap):
    target_qn = target_card.get("qualified_name") or ""
    target_name = target_card.get("name") or ""
    candidate_qn = cr["qn"] or ""
    candidate_name = cr["name"] or ""
    if project_label == target_project and candidate_qn == target_qn:
        return "exact_duplicate"
    same_short_name = candidate_name == target_name or candidate_qn.split(".")[-1] == target_name
    if same_short_name:
        same_stmt = compact_statement(cr["stmt"]) == compact_statement(target_row["stmt"])
        if project_label != target_project or same_stmt:
            return "migrated_copy"
    target_source = target_row["src"] or ""
    candidate_source = cr["src"] or ""
    if target_name and target_name in candidate_source and candidate_qn != target_qn:
        return "downstream_consumer"
    candidate_names = [candidate_qn, candidate_name]
    if any(name and name in target_source for name in candidate_names):
        return "prerequisite_leaf"
    if overlap:
        return "prerequisite_leaf"
    return "unrelated_shape_match"


def add_classified_candidate(buckets, cls, item, limit):
    bucket = buckets.setdefault(cls, [])
    if len(bucket) < limit:
        bucket.append(item)


def consumer_fit(args, default_repo):
    target_project = str(args.get("project") or "provider")
    repo = repo_arg({"project": target_project}, default_repo)
    ensure_index(repo); con = connect(repo)
    q = str(args.get("consumer") or args.get("qualified_name") or args.get("name") or "")
    r, rows = lookup_decl(con, q)
    if r is None:
        con.close(); return {"found": False, "ambiguous": bool(rows), "suggestions": [dict(x) for x in rows]}
    target = theorem_card(repo, con, r, False)
    obligations = [target.get("conclusion", "")] + target.get("premises", [])
    source_terms = shape_terms(r["src"] or r["stmt"] or "")
    con.close()
    projects = as_list(args.get("projects")) or DEFAULT_CROSS_PROJECTS
    per_obligation = []
    candidate_limit = int(args.get("candidates_per_obligation") or 6)
    class_limit = max(candidate_limit, int(args.get("class_limit") or candidate_limit))
    scan_limit = max(12, candidate_limit * 6)
    hidden_classes = {"exact_duplicate", "migrated_copy", "downstream_consumer"}
    for obl in obligations[:int(args.get("max_obligations") or 8)]:
        if not str(obl).strip():
            continue
        qterms = shape_terms(obl)
        candidates = []
        classified = {key: [] for key in CONSUMER_FIT_CLASSES}
        for label, cand_repo in configured_repos(default_repo, projects):
            if not db_path(cand_repo).exists():
                continue
            ccon = connect(cand_repo)
            scored = []
            for cr in ccon.execute("SELECT * FROM decls WHERE kind IN ('theorem','lemma','abbrev')"):
                score, overlap, _ = shape_score(qterms, cr, False)
                if score > 0:
                    scored.append((score, overlap, cr))
            scored.sort(key=lambda x: -x[0])
            for score, overlap, cr in scored[:scan_limit]:
                cls = classify_consumer_candidate(r, target, target_project, label, cr, overlap)
                item = theorem_result(cr); item["project"] = label; item["consumer_fit_class"] = cls; item["shape_score"] = score; item["matched_shape_terms"] = overlap; item["import"] = import_for_file(cr["file"])
                add_classified_candidate(classified, cls, item, class_limit)
                if cls not in hidden_classes:
                    candidates.append(item)
            ccon.close()
        candidates.sort(key=lambda x: -x["shape_score"])
        per_obligation.append({"obligation_shape": obl, "candidates": candidates[:candidate_limit], "classified_candidates": classified, "filtered_candidate_classes": sorted(hidden_classes)})
    return {"found": True, "consumer": target, "source_shape_terms": source_terms[:60], "obligation_candidates": per_obligation, "note": "Heuristic consumer-fit: exact duplicates, migrated copies, and downstream consumers are classified separately; run proof_probe for elaboration."}

def project_templates(args):
    return {"topics": TOPIC_PATHS, "search_templates": [
        {"name": "Matrix.IsHermitian.cfc", "project": "mathlib", "topic": "Matrix", "shape": "Matrix.trace (cfc f A) = _"},
        {"name": "trace cfc finite expansion", "project": "mathlib", "shape": "Matrix.trace (cfc f A) = ∑ i, _"},
        {"name": "CFC.log bridge", "project": "mathlib", "topic": "CFC", "shape": "cfc Real.log A = _"},
        {"name": "resolvent inverse affine line", "project": "mathlib", "shape": "HasDerivAt (fun t => (A + t • C)⁻¹) _ _"},
        {"name": "interval integral shift/cutoff", "project": "mathlib", "topic": "IntervalIntegral", "shape": "∫ x in a..b, f (x + c) = _"},
        {"name": "scalar integral inv positive", "project": "mathlib", "topic": "SpecialFunctions", "shape": "∫ x, (x + a)⁻¹ = Real.log _"}
    ], "probe_template": {"run_project": "provider", "project": "mathlib", "checks": ["Matrix.IsHermitian.eigenvalues_mem_spectrum_real"]}}

TOOLS = {
 "index_repository": ("Incrementally/resumably index a Lean repository. Supports background=true, mode=resume, topic/path_prefix/path_filter/file_pattern, and batch_size.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"mode":{"type":"string"},"background":{"type":"boolean"},"resume":{"type":"boolean"},"topic":{"type":"string"},"topics":{"type":"array","items":{"type":"string"}},"path_prefix":{"type":"string"},"paths":{"type":"array","items":{"type":"string"}},"path_filter":{"type":"string"},"file_pattern":{"type":"string"},"batch_size":{"type":"integer"}}}),
 "index_status": ("Return local Lean index status, progress, staleness, cache size, and optional scoped file details.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"details":{"type":"boolean"},"topic":{"type":"string"},"path_prefix":{"type":"string"},"path_filter":{"type":"string"},"file_pattern":{"type":"string"},"detail_limit":{"type":"integer"}}}),
 "cache_status": ("List cache databases or inspect one project's cache status.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "remove_project": ("Delete one indexed project cache database. Requires project or repo_path unless allow_default is true.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"allow_default":{"type":"boolean"}}}),
 "list_projects": ("List configured local Lean projects.", {"type":"object","properties":{}}),
 "index_visibility": ("Report tracked/untracked indexed files, stale files, and root-import exposure.", {"type":"object","properties":{"repo_path":{"type":"string"},"project":{"type":"string"},"details":{"type":"boolean"},"root":{"type":"string"},"roots":{"type":"array","items":{"type":"string"}},"topic":{"type":"string"},"path_prefix":{"type":"string"},"detail_limit":{"type":"integer"}}}),
 "search_shape": ("Search theorem-like declarations by syntactic type-shape overlap, e.g. Matrix.trace (cfc f A) = _ or HasDerivAt inverse affine-line shapes.", {"type":"object","properties":{"shape":{"type":"string"},"query":{"type":"string"},"type":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},"min_score":{"type":"integer"},"cards":{"type":"boolean"},"include_source":{"type":"boolean"},"repo_path":{"type":"string"},"project":{"type":"string"},"topic":{"type":"string"},"path_prefix":{"type":"string"},"path_filter":{"type":"string"},"file_pattern":{"type":"string"}}}),
 "theorem_card": ("Return a rich theorem card: statement, import, location, namespace, typeclass hints, and minimal #check.", {"type":"object","properties":{"qualified_name":{"type":"string"},"name":{"type":"string"},"include_source":{"type":"boolean"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "proof_probe": ("Generate a temporary Lean file and run lake env lean for #check/example probes; reports import/name/typeclass/type mismatch diagnostics.", {"type":"object","properties":{"project":{"type":"string"},"repo_path":{"type":"string"},"run_project":{"type":"string"},"run_repo_path":{"type":"string"},"imports":{"type":"array","items":{"type":"string"}},"checks":{"type":"array","items":{"type":"string"}},"check":{"type":"string"},"qualified_name":{"type":"string"},"name":{"type":"string"},"goal":{"type":"string"},"proof":{"type":"string"},"example":{"type":"string"},"code":{"type":"string"},"timeout_sec":{"type":"integer"},"verbose":{"type":"boolean"},"full_type":{"type":"boolean"},"pp_all":{"type":"boolean"}}}),
 "consumer_fit": ("Heuristically match a consumer theorem premise/conclusion shape against provider/HighDimProb/Mathlib candidate leaf theorems.", {"type":"object","properties":{"consumer":{"type":"string"},"qualified_name":{"type":"string"},"name":{"type":"string"},"project":{"type":"string"},"projects":{"type":"array","items":{"type":"string"}},"max_obligations":{"type":"integer"},"candidates_per_obligation":{"type":"integer"}}}),
 "cross_repo_lookup": ("Search provider/HighDimProb/Mathlib for same-name declarations and textual users.", {"type":"object","properties":{"query":{"type":"string"},"name":{"type":"string"},"qualified_name":{"type":"string"},"projects":{"type":"array","items":{"type":"string"}},"limit":{"type":"integer"}}}),
 "project_templates": ("Return HighDimProb/LiebProvider-specific search and proof-probe templates.", {"type":"object","properties":{}}),
 "search_graph": ("Search Lean declarations by FTS query, name_pattern, qn_pattern, file_pattern, or label.", {"type":"object","properties":{"query":{"type":"string"},"name_pattern":{"type":"string"},"qn_pattern":{"type":"string"},"file_pattern":{"type":"string"},"label":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"}}}),
 "search_theorems": ("Lean-aware theorem-like search over theorem/lemma/abbrev names, conclusions, premises, and head symbols. Pass cards=true for rich theorem cards; topic/path filters are supported.", {"type":"object","properties":{"query":{"type":"string"},"conclusion":{"type":"string"},"premise":{"type":"string"},"symbol":{"type":"string"},"name_pattern":{"type":"string"},"qn_pattern":{"type":"string"},"file_pattern":{"type":"string"},"limit":{"type":"integer"},"offset":{"type":"integer"},"repo_path":{"type":"string"},"project":{"type":"string"},"topic":{"type":"string"},"path_prefix":{"type":"string"},"path_filter":{"type":"string"},"cards":{"type":"boolean"},"include_source":{"type":"boolean"}}}),
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
    if name == "proof_probe": return proof_probe(args, default_repo)
    if name == "project_templates": return project_templates(args)
    if name == "consumer_fit": return consumer_fit(args, default_repo)
    if name == "cross_repo_lookup": return cross_repo_lookup(args, default_repo)
    repo = repo_arg(args, default_repo)
    if name == "index_repository": return index_repository(args, repo)
    if name == "index_status": return index_status(args, repo)
    if name == "index_visibility": return index_visibility(args, repo)
    if name == "search_graph": return search_graph(args, repo)
    if name == "search_theorems": return search_theorems(args, repo)
    if name == "search_shape": return search_shape(args, repo)
    if name == "theorem_card": return theorem_card_tool(args, repo)
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
    ap.add_argument("--index-args")
    ns = ap.parse_args(); repo = Path(ns.repo).resolve()
    if ns.index_args: print(json.dumps(index_repository(json.loads(ns.index_args), repo), ensure_ascii=False, indent=2)); return 0
    if ns.index: print(json.dumps(index_repository({"mode":"full" if ns.full else "incremental"}, repo), ensure_ascii=False, indent=2)); return 0
    if ns.status: print(json.dumps(index_status({}, repo), ensure_ascii=False, indent=2)); return 0
    if ns.search: print(json.dumps(search_graph({"query":ns.search}, repo), ensure_ascii=False, indent=2)); return 0
    serve(repo); return 0

if __name__ == "__main__":
    raise SystemExit(main())
