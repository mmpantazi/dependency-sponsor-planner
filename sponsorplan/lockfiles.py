"""Lockfile parsers. Each returns a Graph; stdlib only."""
import json
import re
import tomllib
from pathlib import Path

from .model import Graph

LOCKFILES = {
    "package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "yarn.lock": "npm",
    "pnpm-lock.yaml": "npm", "poetry.lock": "pypi", "uv.lock": "pypi",
    "requirements.txt": "pypi", "Cargo.lock": "cargo", "go.mod": "go",
}
SKIP_DIRS = {"node_modules", ".git", "vendor", ".venv", "venv", "target", "dist", "build"}


def find_lockfiles(root):
    root = Path(root)
    if root.is_file():
        return [root]
    found = []
    for path in sorted(root.rglob("*")):
        if path.name in LOCKFILES and path.is_file() and not (SKIP_DIRS & set(path.relative_to(root).parts[:-1])):
            found.append(path)
    return found


def parse(path):
    path = Path(path)
    fn = {"package-lock.json": npm_lock, "npm-shrinkwrap.json": npm_lock, "yarn.lock": yarn_lock,
          "pnpm-lock.yaml": pnpm_lock, "poetry.lock": poetry_lock, "uv.lock": uv_lock,
          "requirements.txt": requirements, "Cargo.lock": cargo_lock, "go.mod": go_mod}[path.name]
    g = fn(path)
    g.source = str(path)
    return g.finalize()


def _text(path):
    return Path(path).read_text(encoding="utf-8-sig", errors="replace")


def _package_json_roots(folder):
    pj = Path(folder) / "package.json"
    if not pj.exists():
        return []
    try:
        data = json.loads(_text(pj))
    except ValueError:
        return []
    roots = [(n, False) for n in {**data.get("dependencies", {}), **data.get("optionalDependencies", {})}]
    roots += [(n, True) for n in data.get("devDependencies", {})]
    return roots


def _npm_name(key):
    return key.rsplit("node_modules/", 1)[-1]


def npm_lock(path):
    data = json.loads(_text(path))
    g = Graph("npm")
    pk = data.get("packages")
    if pk:
        for key, meta in pk.items():
            if key == "":
                g.roots = [(n, False) for n in {**meta.get("dependencies", {}), **meta.get("optionalDependencies", {})}]
                g.roots += [(n, True) for n in meta.get("devDependencies", {})]
                continue
            if meta.get("link") or "node_modules/" not in key:
                continue  # workspace links / local packages
            name = meta.get("name") or _npm_name(key)
            g.add(name, meta.get("version", ""),
                  list(meta.get("dependencies", {})) + list(meta.get("optionalDependencies", {})))
    else:  # lockfileVersion 1
        def walk(deps):
            for name, meta in deps.items():
                g.add(name, meta.get("version", ""), list(meta.get("requires", {})))
                walk(meta.get("dependencies", {}))
        walk(data.get("dependencies", {}))
        g.roots = _package_json_roots(Path(path).parent)
    return g


_YARN_HEAD = re.compile(r'^("?)(?P<specs>[^\s].*?)\1:\s*$')


def yarn_lock(path):
    g = Graph("npm")
    lines = _text(path).splitlines()
    if any("__metadata:" in ln for ln in lines[:12]):
        return _yarn_berry(lines, path)
    cur_names, version, deps, section = None, "", [], None

    def flush():
        if cur_names:
            for n in cur_names:
                g.add(n, version, deps)

    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln.startswith(" "):
            flush()
            specs = [s.strip().strip('"') for s in ln.rstrip().rstrip(":").split(",")]
            cur_names = {s.rsplit("@", 1)[0] for s in specs if "@" in s[1:]}
            version, deps, section = "", [], None
        elif ln.startswith("    ") and section == "deps":
            deps.append(ln.split()[0].strip('"'))
        elif ln.startswith("  "):
            section = None
            s = ln.strip()
            if s.startswith("version"):
                version = s.split(None, 1)[1].strip('"')
            elif s in ("dependencies:", "optionalDependencies:"):
                section = "deps"
    flush()
    g.roots = _package_json_roots(Path(path).parent)
    return g


def _yarn_berry(lines, path):
    g = Graph("npm")
    names, version, deps, section = None, "", [], None

    def flush():
        if names:
            for n in names:
                if n:
                    g.add(n, version, deps)

    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if not ln.startswith(" "):
            flush()
            names, version, deps, section = None, "", [], None
            if ln.startswith("__metadata"):
                continue
            specs = [s.strip().strip('"') for s in ln.rstrip().rstrip(":").split(",")]
            names = set()
            for s in specs:
                n = s.rsplit("@", 1)[0] if "@npm:" not in s else s.split("@npm:")[0]
                if "@workspace:" in s or "@link:" in s:
                    names = set()
                    break
                names.add(n)
        elif ln.startswith("    ") and section == "deps":
            deps.append(ln.split(":")[0].strip().strip('"'))
        elif ln.startswith("  "):
            section = None
            s = ln.strip()
            if s.startswith("version:"):
                version = s.split(":", 1)[1].strip().strip('"')
            elif s == "dependencies:":
                section = "deps"
    flush()
    g.roots = _package_json_roots(Path(path).parent)
    return g


def _simple_yaml(text):
    """Nested-mapping subset of YAML (enough for pnpm-lock.yaml); lists and flow values stay strings."""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        if body.startswith("- "):
            continue
        m = re.match(r"""^(?:'([^']*)'|"([^"]*)"|([^:'"]+?))\s*:(?:\s+(.*))?$""", body)
        if not m:
            continue
        key = next(x for x in m.groups()[:3] if x is not None)
        val = (m.group(4) or "").strip()
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val in ("", "|", ">") or val.startswith("#"):
            node = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = val.strip("'\"")
    return root


def _pnpm_split(key):
    """'/@scope/name@1.0.0(peer)' / '/name/1.0.0' / 'name@1.0.0' -> (name, version)."""
    key = key.lstrip("/")
    key = re.sub(r"\(.*$", "", key)
    if "@" in key[1:]:
        i = key.rfind("@")
        return key[:i], key[i + 1:]
    m = re.match(r"^(.*)/([^/]+)$", key)
    return (m.group(1), m.group(2)) if m else (key, "")


def pnpm_lock(path):
    y = _simple_yaml(_text(path))
    g = Graph("npm")
    for section in ("snapshots", "packages"):
        for key, meta in (y.get(section) or {}).items():
            name, version = _pnpm_split(key)
            deps = []
            if isinstance(meta, dict):
                for k in ("dependencies", "optionalDependencies"):
                    deps += list((meta.get(k) or {}))
            g.add(name, version, deps)
    importers = y.get("importers") or {".": y}
    for imp in importers.values():
        if not isinstance(imp, dict):
            continue
        for k, dev in (("dependencies", False), ("optionalDependencies", False), ("devDependencies", True)):
            for n in (imp.get(k) or {}):
                g.roots.append((n, dev))
    return g


def _norm_py(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(spec):
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return _norm_py(m.group(1)) if m else None


def poetry_lock(path):
    data = tomllib.loads(_text(path))
    g = Graph("pypi")
    for p in data.get("package", []):
        g.add(_norm_py(p["name"]), p.get("version", ""), [_norm_py(d) for d in p.get("dependencies", {})])
    pyproject = Path(path).parent / "pyproject.toml"
    if pyproject.exists():
        try:
            pp = tomllib.loads(_text(pyproject))
        except tomllib.TOMLDecodeError:
            pp = {}
        poetry = pp.get("tool", {}).get("poetry", {})
        for n in poetry.get("dependencies", {}):
            if n.lower() != "python":
                g.roots.append((_norm_py(n), False))
        for n in poetry.get("dev-dependencies", {}):
            g.roots.append((_norm_py(n), True))
        for grp in poetry.get("group", {}).values():
            for n in grp.get("dependencies", {}):
                g.roots.append((_norm_py(n), True))
        for spec in pp.get("project", {}).get("dependencies", []):
            if _req_name(spec):
                g.roots.append((_req_name(spec), False))
    return g


def uv_lock(path):
    data = tomllib.loads(_text(path))
    g = Graph("pypi")
    root_pkgs = []
    for p in data.get("package", []):
        name = _norm_py(p["name"])
        src = p.get("source", {})
        is_root = "virtual" in src or "editable" in src
        edges = [_norm_py(d["name"]) for d in p.get("dependencies", [])]
        if is_root:
            root_pkgs.append(p)
            for n in edges:
                g.roots.append((n, False))
            for grp in list(p.get("dev-dependencies", {}).values()) + list(p.get("optional-dependencies", {}).values()):
                for d in grp:
                    g.roots.append((_norm_py(d["name"]), True))
        else:
            g.add(name, p.get("version", ""), edges)
    return g


def requirements(path):
    g = Graph("pypi")
    for ln in _text(path).splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln or ln.startswith(("-", "git+", "http")):
            continue
        n = _req_name(ln)
        if n:
            m = re.search(r"==\s*([^\s;,]+)", ln)
            g.add(n, m.group(1) if m else "")
            g.roots.append((n, False))
    return g


def cargo_lock(path):
    data = tomllib.loads(_text(path))
    g = Graph("cargo")
    local = []
    for p in data.get("package", []):
        edges = [d.split()[0] for d in p.get("dependencies", [])]
        if "source" not in p:
            local.append(p["name"])
            for e in edges:
                g.roots.append((e, False))
            continue
        g.add(p["name"], p.get("version", ""), edges)
    return g


def go_mod(path):
    g = Graph("go")
    in_block = False
    for ln in _text(path).splitlines():
        s = ln.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if s.startswith("require "):
            s = s[len("require "):]
        elif not in_block:
            continue
        m = re.match(r"(\S+)\s+(v\S+)(\s*//\s*indirect)?", s)
        if m:
            g.add(m.group(1), m.group(2))
            if not m.group(3):
                g.roots.append((m.group(1), False))
    return g
