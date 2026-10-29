"""Find where a package accepts money: registry metadata, then FUNDING.yml in its repository."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "dependency-sponsor-planner/0.1 (+https://github.com/)"
CACHE_TTL = 7 * 86400

# Higher = better place to send money to for a company/individual.
KIND_PREFERENCE = ["github-sponsors", "open-collective", "liberapay", "ko-fi", "polar", "thanks-dev",
                   "patreon", "buy-me-a-coffee", "issuehunt", "tidelift", "other"]

_HOSTS = {
    "github.com": "github-sponsors", "opencollective.com": "open-collective", "liberapay.com": "liberapay",
    "ko-fi.com": "ko-fi", "polar.sh": "polar", "thanks.dev": "thanks-dev", "patreon.com": "patreon",
    "buymeacoffee.com": "buy-me-a-coffee", "issuehunt.io": "issuehunt", "tidelift.com": "tidelift",
}


@dataclass(frozen=True)
class Link:
    kind: str
    url: str

    @property
    def rank(self):
        return KIND_PREFERENCE.index(self.kind)


def make_link(url):
    """Canonicalise a funding URL; None if it does not look like one."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^[a-z]+://", url, re.I):
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower().removeprefix("www.")
    if not host or "." not in host:
        return None
    path = p.path.rstrip("/")
    kind = _HOSTS.get(host, "other")
    if host == "github.com":
        if not path.startswith("/sponsors/"):
            return None
        path = "/sponsors/" + path.split("/")[2] if len(path.split("/")) > 2 else path
    if host == "opencollective.com":
        path = "/" + path.split("/")[1] if path.count("/") >= 1 and path != "" else path
    if kind == "other" and not path and not p.query:
        return None
    return Link(kind, f"https://{host}{path}")


_FUNDING_KEYS = {
    "github": "https://github.com/sponsors/{}", "open_collective": "https://opencollective.com/{}",
    "patreon": "https://www.patreon.com/{}", "ko_fi": "https://ko-fi.com/{}",
    "liberapay": "https://liberapay.com/{}", "polar": "https://polar.sh/{}",
    "buy_me_a_coffee": "https://buymeacoffee.com/{}", "thanks_dev": "https://thanks.dev/{}",
    "issuehunt": "https://issuehunt.io/r/{}", "tidelift": "https://tidelift.com/funding/github/{}",
    "custom": "{}",
}


def parse_funding_yml(text):
    """Parse the flat FUNDING.yml format (scalars, [a, b] lists and block lists)."""
    out, current = {}, None
    for raw in text.splitlines():
        ln = raw.split(" #", 1)[0].rstrip()
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", ln)
        if m and not ln.startswith((" ", "\t", "-")):
            current = m.group(1).lower()
            val = m.group(2).strip()
            out.setdefault(current, [])
            if val.startswith("["):
                out[current] += [v.strip().strip("'\"") for v in val.strip("[]").split(",") if v.strip()]
            elif val and val not in ("~", "null"):
                out[current].append(val.strip("'\""))
        elif ln.lstrip().startswith("-") and current:
            out[current].append(ln.lstrip()[1:].strip().strip("'\""))
    return out


def links_from_funding_yml(text):
    links = []
    for key, values in parse_funding_yml(text).items():
        template = _FUNDING_KEYS.get(key)
        if not template:
            continue
        for v in values:
            url = v if key == "custom" else template.format(v.lstrip("@"))
            link = make_link(url)
            if link and link not in links:
                links.append(link)
    return links


def best_link(links):
    return min(links, key=lambda l: l.rank) if links else None


def github_repo(url):
    """Extract (owner, repo) from the many ways registries spell a GitHub URL."""
    if not url:
        return None
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+)", url) or re.match(r"^github:([\w.-]+)/([\w.-]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    return (owner, repo) if repo else None


def github_from_go_module(path):
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "github.com":
        return parts[1], parts[2]
    return None


class Http:
    def __init__(self, timeout=15, retries=2):
        self.timeout, self.retries = timeout, retries

    def get(self, url):
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, r.read(2_000_000).decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return e.code, ""
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                return 0, ""
        return 0, ""


class Cache:
    def __init__(self, path=None, ttl=CACHE_TTL):
        self.path, self.ttl, self.data = (Path(path) if path else None), ttl, {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                self.data = {}

    def get(self, key, allow_stale=False):
        e = self.data.get(key)
        if e and (allow_stale or time.time() - e["t"] < self.ttl):
            return e["v"]
        return None

    def put(self, key, value):
        self.data[key] = {"t": time.time(), "v": value}

    def save(self):
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data))
            except OSError:
                pass  # the cache is an optimisation only


@dataclass
class Info:
    links: list
    repo: str = ""
    status: str = "ok"  # ok | not-found | error | offline


class Resolver:
    def __init__(self, http=None, cache=None, offline=False, repo_lookup=True):
        self.http, self.cache = http or Http(), cache or Cache()
        self.offline, self.repo_lookup = offline, repo_lookup

    def _cached(self, key, fetch):
        hit = self.cache.get(key, allow_stale=self.offline)
        if hit is not None:
            return hit
        if self.offline:
            return {"status": "offline"}
        val = fetch()
        if val.get("status") != "error":
            self.cache.put(key, val)
        return val

    def lookup(self, dep):
        raw = self._cached(f"pkg:{dep.ecosystem}:{dep.name}", lambda: self._registry(dep))
        links = [Link(k, u) for k, u in raw.get("links", [])]
        repo = raw.get("repo", "")
        status = raw.get("status", "ok")
        if not links and repo and self.repo_lookup and status != "offline":
            owner_repo = github_repo(repo)
            if owner_repo:
                links = self.repo_funding(*owner_repo)
        return Info(links, repo, status)

    def repo_funding(self, owner, repo):
        def fetch():
            for path in (f"{owner}/{repo}/HEAD", f"{owner}/.github/HEAD"):
                for name in (".github/FUNDING.yml", "FUNDING.yml", ".github/funding.yml"):
                    code, text = self.http.get(f"https://raw.githubusercontent.com/{path}/{name}")
                    if code == 200:
                        return {"links": [[l.kind, l.url] for l in links_from_funding_yml(text)]}
                    if code == 0:
                        return {"links": [], "status": "error"}
            return {"links": []}
        raw = self._cached(f"gh:{owner}/{repo}".lower(), fetch)
        return [Link(k, u) for k, u in raw.get("links", [])]

    def _registry(self, dep):
        fn = {"npm": self._npm, "pypi": self._pypi, "cargo": self._cargo, "go": self._go}[dep.ecosystem]
        return fn(dep.name)

    def _json(self, url):
        code, text = self.http.get(url)
        if code == 200:
            try:
                return "ok", json.loads(text)
            except ValueError:
                return "error", None
        return ("not-found" if code in (404, 410) else "error"), None

    @staticmethod
    def _pack(links, repo, status="ok"):
        uniq = []
        for l in links:
            if l and l not in uniq:
                uniq.append(l)
        return {"links": [[l.kind, l.url] for l in uniq], "repo": repo, "status": status}

    def _npm(self, name):
        status, d = self._json("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@") + "/latest")
        if d is None:
            return self._pack([], "", status)
        funding = d.get("funding")
        items = funding if isinstance(funding, list) else [funding] if funding else []
        links = [make_link(i if isinstance(i, str) else (i or {}).get("url")) for i in items]
        repo = d.get("repository")
        repo = repo if isinstance(repo, str) else (repo or {}).get("url", "")
        return self._pack(links, repo)

    def _pypi(self, name):
        status, d = self._json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
        if d is None:
            return self._pack([], "", status)
        info = d.get("info") or {}
        urls = info.get("project_urls") or {}
        links = [make_link(u) for k, u in urls.items()
                 if re.search(r"fund|sponsor|donat|patreon|collective|tidelift", k, re.I)]
        links += [make_link(u) for u in urls.values() if u and re.search(r"opencollective|sponsors/|liberapay|ko-fi|patreon", u)]
        repo = next((u for u in list(urls.values()) + [info.get("home_page") or ""] if github_repo(u)), "")
        return self._pack(links, repo)

    def _cargo(self, name):
        status, d = self._json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
        if d is None:
            return self._pack([], "", status)
        c = d.get("crate", {})
        repo = next((u for u in (c.get("repository"), c.get("homepage")) if github_repo(u)), "")
        return self._pack([], repo)

    def _go(self, name):
        gh = github_from_go_module(name)
        return self._pack([], f"https://github.com/{gh[0]}/{gh[1]}" if gh else "", "ok" if gh else "not-found")
