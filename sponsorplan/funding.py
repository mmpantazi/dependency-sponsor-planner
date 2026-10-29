"""Find where a package accepts money: registry metadata, then FUNDING.yml in its repository."""
import http.client
import json
import re
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "dependency-sponsor-planner/0.1"
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
    """GET with one shared TLS context and a keep-alive connection per thread and host
    (a fresh handshake per request cost ~0.2 s of CPU each)."""

    def __init__(self, timeout=15, retries=2, backoff=1.5):
        self.timeout, self.retries, self.backoff = timeout, retries, backoff
        self._ctx = ssl.create_default_context()
        self._local = threading.local()

    def _conn(self, scheme, host):
        conns = self._local.__dict__.setdefault("conns", {})
        if (scheme, host) not in conns:
            conns[(scheme, host)] = (http.client.HTTPConnection(host, timeout=self.timeout) if scheme == "http" else
                                     http.client.HTTPSConnection(host, timeout=self.timeout, context=self._ctx))
        return conns[(scheme, host)]

    def _drop(self, scheme, host):
        conn = self._local.__dict__.setdefault("conns", {}).pop((scheme, host), None)
        if conn:
            conn.close()

    def get(self, url, _hops=0, follow=True):
        """Return (status, body); body only for 200. 0 = network failure. follow=False returns 3xx as is."""
        parts = urllib.parse.urlsplit(url)
        target = parts.path + ("?" + parts.query if parts.query else "")
        for attempt in range(self.retries + 1):
            try:
                conn = self._conn(parts.scheme, parts.netloc)
                conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
                r = conn.getresponse()
                body = r.read(30_000_000)
                if follow and r.status in (301, 302, 307, 308) and r.getheader("Location") and _hops < 3:
                    return self.get(urllib.parse.urljoin(url, r.getheader("Location")), _hops + 1)
                if r.status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(self.backoff * (attempt + 1))
                    continue
                return r.status, body.decode("utf-8", "replace") if r.status == 200 else ""
            except (OSError, http.client.HTTPException):
                self._drop(parts.scheme, parts.netloc)
                if attempt < self.retries:
                    time.sleep(self.backoff * (attempt + 1))
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

    def page_exists(self, link):
        """True/False if the funding page exists, None if we could not tell (network, rate limit)."""
        def fetch():
            # GitHub redirects /sponsors/<user> to the profile when the user has not enabled Sponsors
            code, _ = self.http.get(link.url, follow=link.kind != "github-sponsors")
            if code in (404, 410) or (link.kind == "github-sponsors" and code in (301, 302, 307, 308)):
                return {"exists": False}
            return {"exists": True} if code and code < 400 else {"exists": None, "status": "error"}
        if self.offline and self.cache.get("page:" + link.url, allow_stale=True) is None:
            return None
        return self._cached("page:" + link.url, fetch).get("exists")

    def lookup(self, dep):
        raw = self._cached(f"pkg:{dep.ecosystem}:{dep.name}", lambda: self._registry(dep))
        links = [Link(k, u) for k, u in raw.get("links", [])]
        repo = raw.get("repo", "")
        status = raw.get("status", "ok")
        if not links and repo and self.repo_lookup and status != "offline":
            owner_repo = github_repo(repo)
            if owner_repo:
                links = self.repo_funding(*owner_repo)
        if not links and raw.get("fallback") and status != "offline":
            links = [Link(*raw["fallback"])]
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
        fn = {"npm": self._npm, "pypi": self._pypi, "cargo": self._cargo, "go": self._go,
              "composer": self._composer, "rubygems": self._rubygems}[dep.ecosystem]
        return fn(dep.name, dep.version)

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

    def _npm(self, name, version=""):
        status, d = self._json("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@") + "/latest")
        if d is None:
            return self._pack([], "", status)
        funding = d.get("funding")
        items = funding if isinstance(funding, list) else [funding] if funding else []
        urls = [i if isinstance(i, str) else (i or {}).get("url") for i in items]
        links = [make_link(u) for u in urls]
        repo = d.get("repository")
        repo = repo if isinstance(repo, str) else (repo or {}).get("url", "")
        # "https://github.com/chalk/chalk?sponsor=1" points at a repo whose FUNDING.yml names the real recipients
        hint = next((u for u in urls if u and not make_link(u) and github_repo(u) and "sponsor" in u), "")
        if hint:
            repo = hint
        out = self._pack(links, repo)
        if hint:
            out["fallback"] = ["github-sponsors", "https://github.com/sponsors/" + github_repo(hint)[0]]
        return out

    def _pypi(self, name, version=""):
        base = f"https://pypi.org/pypi/{urllib.parse.quote(name)}"
        # the per-release document is a few KB; the project document lists every release (SQLAlchemy: 5 MB)
        status, d = self._json(f"{base}/{urllib.parse.quote(version)}/json") if version else ("not-found", None)
        if d is None and status == "not-found":
            status, d = self._json(f"{base}/json")
        if d is None:
            return self._pack([], "", status)
        info = d.get("info") or {}
        urls = info.get("project_urls") or {}
        links = [make_link(u) for k, u in urls.items()
                 if re.search(r"fund|sponsor|donat|patreon|collective|tidelift", k, re.I)]
        links += [make_link(u) for u in urls.values() if u and re.search(r"opencollective|sponsors/|liberapay|ko-fi|patreon", u)]
        repo = next((u for u in list(urls.values()) + [info.get("home_page") or ""] if github_repo(u)), "")
        return self._pack(links, repo)

    def _cargo(self, name, version=""):
        status, d = self._json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
        if d is None:
            return self._pack([], "", status)
        c = d.get("crate", {})
        repo = next((u for u in (c.get("repository"), c.get("homepage")) if github_repo(u)), "")
        return self._pack([], repo)

    def _go(self, name, version=""):
        gh = github_from_go_module(name)
        return self._pack([], f"https://github.com/{gh[0]}/{gh[1]}" if gh else "", "ok" if gh else "not-found")

    def _composer(self, name, version=""):
        status, d = self._json(f"https://repo.packagist.org/p2/{urllib.parse.quote(name)}.json")
        if d is None:
            return self._pack([], "", status)
        releases = (d.get("packages") or {}).get(name) or [{}]
        latest = releases[0]
        links = []
        for item in latest.get("funding") or []:
            url = item.get("url", "")
            m = re.match(r"^https?://github\.com/([\w.-]+)/?$", url)
            links.append(make_link(f"https://github.com/sponsors/{m.group(1)}" if m else url))
        return self._pack(links, (latest.get("source") or {}).get("url", ""))

    def _rubygems(self, name, version=""):
        status, d = self._json(f"https://rubygems.org/api/v1/gems/{urllib.parse.quote(name)}.json")
        if d is None:
            return self._pack([], "", status)
        meta = d.get("metadata") or {}
        links = [make_link(meta.get("funding_uri"))]
        repo = next((u for u in (meta.get("source_code_uri"), d.get("source_code_uri"), d.get("homepage_uri")) if github_repo(u)), "")
        return self._pack(links, repo)
