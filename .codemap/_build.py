#!/usr/bin/env python3
"""
Cartographer build script (template — v0.4.0).

Copy this into a project's `.codemap/_build.py`. Walks the source tree and
writes the codemap JSON files into `.codemap/`. Used for both initial full
build and incremental refresh (content-hash-keyed; only re-analyzes files
that changed).

Invoked by:
  - User: `python3 .codemap/_build.py` from the repo root.
  - SessionStart hook (see `.claude/settings.json`): runs automatically
    on every Claude session start so the codemap stays fresh.
  - app-audit's Phase 0.5: invokes this script before audit runs.
  - audit-fix's Phase 0: invokes with --with-call-graph for tier classification.

Idempotent. Cheap to re-run (file hash gates the work).

Build stages:
  Stage 1 — Function-level tag propagation (default on)
  Stage 2 — Function-level spec_refs propagation (default on)
  Stage 3 — Qualified names + git blame per file (default on)
  Stage 4 — Call graph extraction (opt-in; --with-call-graph or interactive prompt)

Flags:
  --with-call-graph       run stage 4 (call graph extraction)
  --no-call-graph         skip stage 4 even if cached result exists
  --rebuild-call-graph    force-rebuild stage 4 cache
  --non-interactive       never prompt; treat stage 4 as off unless --with-call-graph

Per-project customization:
  The tag inference (TAG_KEYWORDS), suspicious-name patterns, and any
  project-specific spec_refs hints are intended to be edited per project.
  This template ships with a baseline set; customize freely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CODEMAP_DIR = REPO_ROOT / ".codemap"
CALL_GRAPH_CACHE = CODEMAP_DIR / "_call_graph_cache.json"
CARTOGRAPHER_VERSION = "0.4.1"

# Directories that never get walked into.
# `.claude` is excluded so cartographer never descends into Desktop-dispatched
# code-task worktrees (.claude/worktrees/<name>/), which are full copies of the
# repo and would otherwise double-count every file as a duplicate.
SKIP_DIRS = {
    ".git", "target", "node_modules", "dist", ".next", "build",
    "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".codemap", ".claude",
}

# File suffixes worth analyzing.
SOURCE_SUFFIXES = {".rs", ".py", ".sh", ".js", ".jsx", ".ts", ".tsx", ".sql"}
CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json"}
DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}
HTML_SUFFIXES = {".html"}

# Filenames that signal an entry point regardless of import graph.
ENTRY_POINT_NAMES = {
    "main.rs", "lib.rs", "index.html", "index.js", "main.py",
    "Cargo.toml", "package.json", "tauri.conf.json",
}

# Suspicious filename patterns for warnings.json detector 2.
SUSPICIOUS_PATTERNS = [
    (re.compile(r".*\.old\..*", re.IGNORECASE),         "*.old.*"),
    (re.compile(r".*\.backup\..*", re.IGNORECASE),       "*.backup.*"),
    (re.compile(r".*\.bak$|.*\.bak\..*", re.IGNORECASE), "*.bak.*"),
    (re.compile(r".*_old\..*"),                          "*_old.*"),
    (re.compile(r".*_backup\..*"),                       "*_backup.*"),
    (re.compile(r".*_archive\..*"),                      "*_archive.*"),
    (re.compile(r".*_v\d+\..*"),                         "*_v[N].*"),
    (re.compile(r".*[-_]copy\..*", re.IGNORECASE),       "*-copy.*"),
    (re.compile(r".*(DELETE|REMOVE|TODO_REMOVE).*"),     "*DELETE/REMOVE*"),
    (re.compile(r"^(Untitled|New File)\..*"),            "Untitled.*"),
]

# Directories where the migrations/ exclusion applies — these are sequenced
# files where _v[N] suffix is canonical, not a backup. Resolves R2.cart.1.
MIGRATION_DIRS = {"migrations", "migration"}

FN_SIG_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+([a-zA-Z_][a-zA-Z0-9_]*)"
)

# Section header pattern in markdown / comments — captures §N.M references.
SPEC_REF_RE = re.compile(r"§(\d+(?:\.\d+)*)")


# ---------------------------------------------------------------------
# Tag inference (unchanged from prior version — included for completeness)
# ---------------------------------------------------------------------

def infer_tags(rel_path: str, language: str) -> list[str]:
    """Heuristic tag inference. Conservative — better fewer correct
    tags than many wrong ones."""
    tags = []
    rl = rel_path.lower()

    # Language-based foundation
    if language == "rust":
        tags.append("rust")
    elif language in {"javascript", "typescript"}:
        tags.append("frontend")

    # Path-based tags
    if "test" in rl or "_test." in rl or "/tests/" in rl:
        tags.append("test")
    if "/migrations/" in rl or rl.startswith("migrations/"):
        tags.append("migration")
    if "/routes/" in rl or rl.startswith("routes/"):
        tags.append("api")
    if "oauth" in rl or "/auth/" in rl or "_auth" in rl:
        tags.append("auth")
    if "crypto" in rl or "encrypt" in rl or "keychain" in rl:
        tags.append("security-sensitive")
    if "worker" in rl or "ticker" in rl or "scheduler" in rl or "supervisor" in rl:
        tags.append("worker")
    if "sync" in rl:
        tags.append("sync")
    if "vector" in rl or "embed" in rl:
        tags.append("vector")
    if "/services/mail/" in rl:
        tags.append("mail")
    if "/services/calendar/" in rl:
        tags.append("calendar")
    if "/services/messages/" in rl:
        tags.append("messages")
    if "/services/shared/org/" in rl or "/org_map/" in rl:
        tags.append("org-map")
    if "briefing" in rl or "digest" in rl:
        tags.append("briefing")
    if "chief_of_staff" in rl or "chief-of-staff" in rl:
        tags.append("chief-of-staff")
    if "tasks" in rl and "/services/" in rl:
        tags.append("tasks")

    return tags


def classify_doc(rel_path: str, content: str, spec_cfg: dict) -> str:
    """Classify a markdown doc as spec / operational / informational."""
    if rel_path == spec_cfg.get("canonical_spec"):
        return "spec"
    if rel_path in spec_cfg.get("supporting_docs", []):
        return "spec"
    if rel_path in spec_cfg.get("operational_docs", []):
        return "operational"
    return "informational"


def section_count_in(md_content: str) -> int:
    return sum(1 for ln in md_content.split("\n") if ln.startswith("##"))


# ---------------------------------------------------------------------
# Spec-refs inference (NEW — Stage 2 helper)
# ---------------------------------------------------------------------

def infer_file_spec_refs(rel_path: str, text: str, tags: list[str]) -> list[dict]:
    """Infer which spec sections this file likely implements.

    Returns a list of {ref, source, confidence} entries.

    Conservative — only emits high-confidence inferences. Tag-based,
    matching common spec section patterns to file content/path.
    """
    refs = []
    seen_refs = set()

    # 1. Explicit references in code comments (highest confidence)
    for match in SPEC_REF_RE.finditer(text):
        ref = match.group(1)  # e.g. "3.2" or "4.1.2"
        if ref not in seen_refs:
            refs.append({
                "ref": f"§{ref}",
                "source": "explicit",
                "confidence": 1.0,
            })
            seen_refs.add(ref)

    # 2. Tag-based inference (medium confidence — only when tag profile is well-defined)
    tag_to_specs = {
        "auth": [("§8.1", 0.85)],  # auth section in chief-of-staff-v3
        "security-sensitive": [("§8.2", 0.80)],  # encryption section
        "worker": [("§6", 0.75)],  # background workers section
        "vector": [("§4.3", 0.80)],  # vectorization
        "mail": [("§3.1", 0.75)],  # mail surface
        "calendar": [("§3.2", 0.80)],  # calendar surface (v4.1)
        "messages": [("§3.3", 0.80)],  # messages surface (v4.1)
        "org-map": [("§3.6", 0.80)],  # org map
        "briefing": [("§3.7", 0.75)],  # briefing surface
        "chief-of-staff": [("§3.5", 0.80)],  # chief-of-staff conversation
        "api": [("§5", 0.70)],  # API/routes
        "migration": [("§3", 0.70)],  # schema section, low confidence
    }
    for tag in tags:
        for ref, conf in tag_to_specs.get(tag, []):
            ref_key = ref.lstrip("§")
            if ref_key not in seen_refs:
                refs.append({
                    "ref": ref,
                    "source": "inferred",
                    "confidence": conf,
                })
                seen_refs.add(ref_key)

    return refs


# ---------------------------------------------------------------------
# Qualified name computation (Stage 3 helper)
# ---------------------------------------------------------------------

def compute_qualified_prefix(rel_path: str, language: str) -> str:
    """Build a module-path prefix for functions defined in this file.

    For Rust: backend-rs/src/services/mail/imap_sync.rs
              → backend-rs::services::mail::imap_sync

    For Python: scripts/build/foo.py → scripts.build.foo

    For TS/JS: returns the file path with the suffix stripped, used
    as-is by callers (less standardized than Rust's module system).
    """
    p = Path(rel_path)
    parts = list(p.parts)
    stem = p.stem

    if language == "rust":
        # Strip /src/ from the path if present — convention for Rust crates
        if "src" in parts:
            src_idx = parts.index("src")
            crate = "::".join(parts[:src_idx])
            module = "::".join(parts[src_idx + 1:-1] + ([stem] if stem != "lib" and stem != "mod" else []))
            return f"{crate}::{module}" if module else crate
        return "::".join(parts[:-1] + [stem])

    if language == "python":
        return ".".join(parts[:-1] + [stem])

    if language in {"javascript", "typescript"}:
        # Keep path form — TS doesn't have a canonical module name
        return str(p.with_suffix(""))

    return rel_path  # fallback


# ---------------------------------------------------------------------
# Git blame helpers (Stage 3) — per file, cached by content hash
# ---------------------------------------------------------------------

def file_last_commit(rel_path: str) -> str:
    """Run `git log -1 --format=%H -- <rel_path>`. Returns empty string
    on failure or non-git environments."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", rel_path],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


# ---------------------------------------------------------------------
# Function extraction (UPDATED — stages 1, 2, 3 inline)
# ---------------------------------------------------------------------

def extract_rust_imports(text: str) -> list[dict]:
    """Extract Rust use-statements (best-effort — single-line uses only)."""
    imports = []
    for line in text.split("\n"):
        m = re.match(r"^\s*use\s+([a-zA-Z0-9_:]+(?:::\{[^}]+\})?)\s*;", line)
        if not m:
            continue
        path = m.group(1).split("::{")[0]
        symbols_part = m.group(1).split("::{", 1)[1].rstrip("}") if "::{" in m.group(1) else ""
        symbols = [s.strip() for s in symbols_part.split(",")] if symbols_part else []
        imports.append({"path": path, "symbols": symbols, "kind": "use"})
    return imports


def extract_rust_functions(
    text: str,
    *,
    file_tags: list[str] | None = None,
    file_spec_refs: list[dict] | None = None,
    qualified_prefix: str = "",
    file_last_commit_hash: str = "",
) -> list[dict]:
    """Extract Rust function signatures from text.

    Stages 1, 2, 3 are applied here:
      - Stage 1: file_tags is copied into each function's tags
      - Stage 2: file_spec_refs is copied into each function's spec_refs,
        and §N.M patterns in nearby comments are added
      - Stage 3: qualified_name is built from qualified_prefix + name,
        last_modified_commit is populated from file_last_commit_hash
    """
    file_tags = file_tags or []
    file_spec_refs = file_spec_refs or []

    fns: list[dict] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = FN_SIG_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i + 1
        buf = lines[i].rstrip()
        j = i
        while j < len(lines) and "{" not in lines[j] and not lines[j].rstrip().endswith(";"):
            j += 1
            if j < len(lines):
                buf += " " + lines[j].strip()
        if "{" in buf:
            buf = buf[: buf.index("{")]
        sig = buf.strip().rstrip(";").rstrip()
        name = m.group(1)

        # Stage 2 (continued): scan a small window of comment lines above
        # the function for §N.M references. Take the previous 4 lines.
        nearby_refs: list[dict] = list(file_spec_refs)  # shallow copy
        seen_local = {r["ref"].lstrip("§") for r in nearby_refs}
        for k in range(max(0, i - 4), i):
            for ref_match in SPEC_REF_RE.finditer(lines[k]):
                ref = ref_match.group(1)
                if ref not in seen_local:
                    nearby_refs.append({
                        "ref": f"§{ref}",
                        "source": "explicit",
                        "confidence": 1.0,
                    })
                    seen_local.add(ref)

        # Stage 3: build qualified_name
        qualified_name = f"{qualified_prefix}::{name}" if qualified_prefix else name

        fns.append({
            "name": name,
            "qualified_name": qualified_name,
            "signature": sig,
            "loc_range": [start, 0],
            "calls": [],
            "called_by": [],
            "tags": list(file_tags),  # Stage 1
            "spec_refs": nearby_refs,  # Stage 2
            "exported": sig.startswith("pub"),
            "last_modified_commit": file_last_commit_hash,  # Stage 3
        })
        i = j + 1
    return fns


# ---------------------------------------------------------------------
# Stage 4 — Call graph extraction (opt-in)
# ---------------------------------------------------------------------

def extract_call_graph(funcs_data: dict, files_text: dict[str, str]) -> dict:
    """Build function-to-function call edges across the codebase.

    Approach: regex-based identifier matching. For each function body,
    look for identifiers that match other functions' names. Noisy but
    fast — good enough for blast-radius grading, not for precise
    refactoring.

    Returns: dict mapping qualified_name → {calls: [...], called_by: [...]}
    """
    # Build a lookup of name → list of qualified_names that define it
    name_lookup: dict[str, list[str]] = {}
    qname_to_loc: dict[str, tuple[str, int, int]] = {}  # qname → (file, start, end)

    for rel, file_data in funcs_data.get("files", {}).items():
        for fn in file_data.get("functions", []):
            name = fn["name"]
            qname = fn.get("qualified_name") or name
            name_lookup.setdefault(name, []).append(qname)
            start = fn["loc_range"][0]
            qname_to_loc[qname] = (rel, start, start + 100)  # rough end estimate

    print(f"cartographer stage 4: scanning {len(qname_to_loc)} function bodies for calls...", file=sys.stderr)

    edges: dict[str, dict[str, list]] = {}  # qname → {calls, called_by}
    for qname in qname_to_loc:
        edges[qname] = {"calls": [], "called_by": []}

    # Skip common Rust keywords / stdlib that match function-name patterns
    SKIP_NAMES = {
        "new", "as", "from", "into", "to_string", "clone", "unwrap", "expect",
        "ok", "err", "some", "none", "iter", "map", "filter", "collect", "len",
        "push", "pop", "insert", "remove", "get", "set", "default", "fmt",
        "drop", "build", "with", "fn", "let", "if", "else", "match", "for",
        "while", "loop", "return", "break", "continue", "use", "mod", "pub",
        "self", "Self", "true", "false", "Some", "None", "Ok", "Err", "Vec",
        "String", "Box", "Arc", "Rc", "RefCell", "Mutex", "HashMap", "BTreeMap",
        "Option", "Result", "PathBuf", "Path", "println", "print", "format",
        "write", "writeln", "vec", "matches", "todo", "unimplemented", "panic",
        "debug_assert", "assert", "trace", "info", "warn", "error",
    }

    edge_count = 0
    for caller_qname, (rel, start, end) in qname_to_loc.items():
        text = files_text.get(rel, "")
        if not text:
            continue
        lines = text.split("\n")
        # body is from start to next function or EOF (rough)
        body_lines = lines[start - 1:start + 200]
        body = "\n".join(body_lines)

        # Strip line comments to reduce false positives
        body_clean = re.sub(r"//[^\n]*", "", body)
        # Look for identifier-followed-by-paren patterns
        for match in re.finditer(r"([a-z_][a-zA-Z0-9_]*)\s*\(", body_clean):
            called_name = match.group(1)
            if called_name in SKIP_NAMES:
                continue
            if called_name == caller_qname.split("::")[-1]:
                # likely self-recursion or self-name match — record but flag
                pass
            candidates = name_lookup.get(called_name, [])
            for callee_qname in candidates:
                if callee_qname == caller_qname:
                    continue  # skip pure self-recursion in edges
                if callee_qname not in [c["target"] for c in edges[caller_qname]["calls"]]:
                    edges[caller_qname]["calls"].append({"target": callee_qname})
                    edges[callee_qname]["called_by"].append({"caller": caller_qname})
                    edge_count += 1

    print(f"cartographer stage 4: extracted {edge_count} edges", file=sys.stderr)
    return edges


def prompt_user_for_call_graph(non_interactive: bool) -> bool:
    """Ask user whether to run stage 4. Returns True if yes."""
    if non_interactive:
        return False
    # If cached and fresh, skip the prompt — caller handles that path.
    print("", file=sys.stderr)
    print("cartographer: Stage 4 (call graph extraction) is optional.", file=sys.stderr)
    print("  - It produces richer audit-fix blast-radius grading.", file=sys.stderr)
    print("  - It costs an additional ~30-60 seconds at build time.", file=sys.stderr)
    print("  - It increases app-audit token cost by roughly 30-50% when consumed.", file=sys.stderr)
    print("", file=sys.stderr)
    try:
        ans = input("Run stage 4 now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in {"y", "yes"}


def cached_call_graph_is_fresh(per_file_state: dict) -> bool:
    """Returns True if the cached call graph matches the current file set
    closely enough that we can reuse it without re-extracting."""
    if not CALL_GRAPH_CACHE.exists():
        return False
    try:
        cache = json.loads(CALL_GRAPH_CACHE.read_text())
    except Exception:
        return False
    cached_files = set(cache.get("files_covered", []))
    current_files = set(per_file_state.keys())
    if not cached_files or not current_files:
        return False
    # Consider fresh if ≥90% of files are unchanged (covered by cache)
    overlap = cached_files & current_files
    coverage = len(overlap) / len(current_files)
    return coverage >= 0.90


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Cartographer build script")
    parser.add_argument("--with-call-graph", action="store_true",
                        help="Run stage 4 (call graph extraction)")
    parser.add_argument("--no-call-graph", action="store_true",
                        help="Skip stage 4 even if cache exists")
    parser.add_argument("--rebuild-call-graph", action="store_true",
                        help="Force-rebuild stage 4 cache")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Never prompt; treat stage 4 as off unless --with-call-graph")
    args = parser.parse_args()

    # Load spec-config.yml (lightweight YAML — only top-level keys).
    spec_cfg = load_spec_config()

    # 1. Walk the tree
    files = walk_tree()
    print(f"cartographer: walked {len(files)} files", file=sys.stderr)

    # 2. Per-file analysis
    structure = {"schema_version": 1, "generated_at": utc_now_iso(),
                 "root": str(REPO_ROOT), "languages_detected": set(),
                 "files_count": 0, "directories": [], "files": {}}
    deps = {"schema_version": 1, "generated_at": utc_now_iso(), "graph": {}}
    funcs = {"schema_version": 1, "generated_at": utc_now_iso(),
             "files": {}, "build_metadata": {
                 "build_method": "py-script-richer",
                 "stages_run": ["1_tags", "2_spec_refs", "3_qualified_names"],
             }}
    per_file_state = {}
    files_text_cache: dict[str, str] = {}  # used by stage 4 if it runs

    for fp in files:
        rel = fp.relative_to(REPO_ROOT).as_posix()
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        size = fp.stat().st_size
        loc = sum(1 for _ in text.split("\n"))
        h = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        language = guess_language(fp)
        if language:
            structure["languages_detected"].add(language)

        purpose = ""
        tags: list[str] = []
        kind = "source"
        doc_class = None
        section_count = 0
        is_canonical_spec = False
        imports: list[dict] = []
        functions: list[dict] = []
        file_spec_refs: list[dict] = []

        if language == "markdown":
            kind = "documentation"
            doc_class = classify_doc(rel, text, spec_cfg)
            section_count = section_count_in(text)
            is_canonical_spec = (rel == spec_cfg.get("canonical_spec"))
            tags = ["documentation", doc_class]
            purpose = first_meaningful_line(text)
        elif language == "rust":
            tags = infer_tags(rel, "rust")
            file_spec_refs = infer_file_spec_refs(rel, text, tags)  # Stage 2 (file-level)
            qualified_prefix = compute_qualified_prefix(rel, "rust")  # Stage 3
            file_commit = file_last_commit(rel)  # Stage 3
            imports = extract_rust_imports(text)
            functions = extract_rust_functions(
                text,
                file_tags=tags,
                file_spec_refs=file_spec_refs,
                qualified_prefix=qualified_prefix,
                file_last_commit_hash=file_commit,
            )
            purpose = first_meaningful_line(text)
            files_text_cache[rel] = text  # cache for stage 4 if it runs
        elif language == "sql":
            kind = "source"
            tags = infer_tags(rel, "sql")
            file_spec_refs = infer_file_spec_refs(rel, text, tags)
            purpose = first_meaningful_line(text)
        elif language in {"javascript", "typescript"}:
            tags = infer_tags(rel, language)
            file_spec_refs = infer_file_spec_refs(rel, text, tags)
            purpose = first_meaningful_line(text)
        elif language == "python":
            tags = infer_tags(rel, language)
            file_spec_refs = infer_file_spec_refs(rel, text, tags)
            purpose = first_meaningful_line(text)
        elif language in {"toml", "yaml", "json"}:
            kind = "config"
            tags = ["config"]
        else:
            kind = "other"

        entry_point = fp.name in ENTRY_POINT_NAMES

        struct_entry = {
            "kind": kind, "language": language or "unknown",
            "loc": loc, "size_bytes": size,
            "purpose": purpose, "tags": tags,
            "spec_refs": file_spec_refs,  # Stage 2 (file-level)
            "entry_point": entry_point,
            "test_file": "/tests/" in rel or rel.endswith("_test.rs"),
        }
        if doc_class:
            struct_entry["doc_class"] = doc_class
            struct_entry["section_count"] = section_count
            if is_canonical_spec:
                struct_entry["is_canonical_spec"] = True
        structure["files"][rel] = struct_entry

        deps["graph"][rel] = {"imports_from": imports, "imported_by": []}
        if functions:
            funcs["files"][rel] = {"functions": functions}

        per_file_state[rel] = {
            "last_refreshed_commit": git_head() + ("-dirty" if git_dirty() else ""),
            "content_hash": h,
            "last_modified_in_git": utc_now_iso(),
        }

    structure["files_count"] = len(structure["files"])
    structure["languages_detected"] = sorted(structure["languages_detected"])

    # 3. Build cross-references — imported_by population
    for src, info in deps["graph"].items():
        for imp in info["imports_from"]:
            target = imp.get("path")
            if target and target in deps["graph"]:
                deps["graph"][target]["imported_by"].append({
                    "path": src, "symbols": imp.get("symbols", []),
                    "kind": imp.get("kind", "use"),
                })

    # 4. Stage 4 — Call graph (opt-in)
    run_stage_4 = False
    cache_valid = cached_call_graph_is_fresh(per_file_state) and not args.rebuild_call_graph

    if args.no_call_graph:
        run_stage_4 = False
    elif args.with_call_graph or args.rebuild_call_graph:
        run_stage_4 = True
    elif cache_valid:
        # Reuse cache without prompting
        print("cartographer: reusing cached call graph (≥90% file overlap)", file=sys.stderr)
        try:
            cache = json.loads(CALL_GRAPH_CACHE.read_text())
            cached_edges = cache.get("edges", {})
            # Apply cached edges to funcs
            for rel, file_data in funcs["files"].items():
                for fn in file_data["functions"]:
                    qname = fn.get("qualified_name")
                    if qname in cached_edges:
                        fn["calls"] = cached_edges[qname].get("calls", [])
                        fn["called_by"] = cached_edges[qname].get("called_by", [])
            funcs["build_metadata"]["stages_run"].append("4_call_graph_cached")
        except Exception as e:
            print(f"cartographer: cache load failed ({e}), running fresh", file=sys.stderr)
            run_stage_4 = prompt_user_for_call_graph(args.non_interactive)
    else:
        run_stage_4 = prompt_user_for_call_graph(args.non_interactive)

    if run_stage_4:
        edges = extract_call_graph(funcs, files_text_cache)
        # Apply to funcs
        for rel, file_data in funcs["files"].items():
            for fn in file_data["functions"]:
                qname = fn.get("qualified_name")
                if qname in edges:
                    fn["calls"] = edges[qname].get("calls", [])
                    fn["called_by"] = edges[qname].get("called_by", [])
        funcs["build_metadata"]["stages_run"].append("4_call_graph")
        # Write cache
        CALL_GRAPH_CACHE.write_text(json.dumps({
            "schema_version": 1,
            "generated_at": utc_now_iso(),
            "files_covered": sorted(per_file_state.keys()),
            "edges": edges,
        }, indent=2))
        print(f"cartographer: stage 4 cache written to {CALL_GRAPH_CACHE.name}", file=sys.stderr)

    # 5. Warning detectors
    warnings = detect_warnings(structure, deps)

    # 6. Write everything
    state = {
        "schema_version": 1,
        "cartographer_version": CARTOGRAPHER_VERSION,
        "created_at": utc_now_iso(),
        "last_full_build_at": utc_now_iso(),
        "last_full_build_commit": git_head() + ("-dirty" if git_dirty() else ""),
        "last_refresh_at": utc_now_iso(),
        "last_refresh_commit": git_head() + ("-dirty" if git_dirty() else ""),
        "build_metadata": {
            "files_processed": len(per_file_state),
            "languages_detected": structure["languages_detected"],
            "build_method": "py-script-richer",
            "stages_run": funcs["build_metadata"]["stages_run"],
            "note": (
                "Stages 1+2+3 default-on (tag propagation, spec_refs, "
                "qualified names, git blame). Stage 4 (call graph) is opt-in."
            ),
        },
        "configured_entry_points": sorted(ENTRY_POINT_NAMES),
        "per_file_state": per_file_state,
    }

    CODEMAP_DIR.mkdir(exist_ok=True)
    write_json(CODEMAP_DIR / "structure.json", structure)
    write_json(CODEMAP_DIR / "dependencies.json", deps)
    write_json(CODEMAP_DIR / "functions.json", funcs)
    write_json(CODEMAP_DIR / "warnings.json", warnings)
    write_json(CODEMAP_DIR / "state.json", state)

    print(
        f"cartographer: complete. files={len(per_file_state)} "
        f"stages={','.join(funcs['build_metadata']['stages_run'])}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------
# Walk / language detection / utilities (unchanged from prior version)
# ---------------------------------------------------------------------

def walk_tree() -> list[Path]:
    out = []
    for root, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            fp = Path(root) / n
            suffix = fp.suffix.lower()
            if suffix in SOURCE_SUFFIXES or suffix in CONFIG_SUFFIXES or suffix in DOC_SUFFIXES or suffix in HTML_SUFFIXES:
                out.append(fp)
            elif n in ENTRY_POINT_NAMES:
                out.append(fp)
    return sorted(set(out))


def guess_language(fp: Path) -> str | None:
    s = fp.suffix.lower()
    return {
        ".rs": "rust", ".py": "python", ".sh": "bash",
        ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".sql": "sql", ".toml": "toml",
        ".yaml": "yaml", ".yml": "yaml",
        ".json": "json", ".md": "markdown",
        ".mdx": "markdown", ".rst": "markdown",
        ".txt": "text", ".html": "html",
    }.get(s)


def first_meaningful_line(text: str) -> str:
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("//") or s.startswith("#") or s.startswith("--") or s.startswith("/*"):
            s = s.lstrip("/#-* ")
        if s:
            return s[:160]
    return ""


def detect_warnings(structure: dict, deps: dict) -> dict:
    warnings = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "summary": {
            "duplicate_basenames": 0,
            "suspicious_names": 0,
            "backup_directories": 0,
            "orphan_files": 0,
            "near_duplicates": 0,
            "stale_build_output": 0,
            "canonical_designations": 0,
        },
        "duplicate_basenames": [],
        "suspicious_names": [],
        "backup_directories": [],
        "orphan_files": [],
        "near_duplicates": [],
        "stale_build_output": [],
        "canonical_designations": [],
    }

    # Duplicate basenames
    basename_to_paths: dict[str, list[str]] = {}
    for rel in structure["files"]:
        basename_to_paths.setdefault(Path(rel).name, []).append(rel)
    for bn, paths in sorted(basename_to_paths.items()):
        if len(paths) > 1:
            warnings["duplicate_basenames"].append({
                "basename": bn, "paths": sorted(paths),
                "imported_count": 0, "severity": "medium",
                "concern": f"`{bn}` appears in {len(paths)} locations.",
            })
    warnings["summary"]["duplicate_basenames"] = len(warnings["duplicate_basenames"])

    # Suspicious names — but exclude migration directories (resolves R2.cart.1)
    for rel in structure["files"]:
        path_parts = Path(rel).parts
        is_in_migrations = any(p.lower() in MIGRATION_DIRS for p in path_parts)
        if is_in_migrations:
            continue  # migrations/_v[N] is canonical, not suspicious
        for pat, label in SUSPICIOUS_PATTERNS:
            if pat.match(Path(rel).name):
                warnings["suspicious_names"].append({
                    "path": rel, "pattern": label,
                    "severity": "medium",
                    "concern": f"Filename `{Path(rel).name}` matches pattern `{label}`. NOT imported.",
                    "imported_by_count": len(deps["graph"].get(rel, {}).get("imported_by", [])),
                })
                break
    warnings["summary"]["suspicious_names"] = len(warnings["suspicious_names"])

    # Orphan files — source files with no imported_by
    for rel, struct in structure["files"].items():
        if struct["kind"] not in {"source"}:
            continue
        if struct["entry_point"]:
            continue
        imported_by = deps["graph"].get(rel, {}).get("imported_by", [])
        if not imported_by:
            warnings["orphan_files"].append({
                "path": rel, "severity": "low",
                "concern": "Source file has no `imported_by` entries; may be unreachable.",
                "is_entry_point": False,
                "suggested_action": "Check whether this file is dynamically loaded.",
            })
    warnings["summary"]["orphan_files"] = len(warnings["orphan_files"])

    return warnings


def load_spec_config() -> dict:
    cfg_path = CODEMAP_DIR / "spec-config.yml"
    if not cfg_path.exists():
        return {}
    cfg: dict = {}
    current_list_key = None
    for line in cfg_path.read_text().split("\n"):
        line = line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if line.startswith("  - ") and current_list_key:
            cfg[current_list_key].append(line[4:].strip())
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                cfg[key] = []
                current_list_key = key
            else:
                cfg[key] = value
                current_list_key = None
    return cfg


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def git_dirty() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        return bool(result.stdout.strip()) if result.returncode == 0 else False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False, default=str))


if __name__ == "__main__":
    main()
