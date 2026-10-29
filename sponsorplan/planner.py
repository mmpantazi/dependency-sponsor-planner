from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import lockfiles
from .allocate import split
from .funding import Cache, Resolver
from .model import Dep
from .ranking import group_by_recipient, is_excluded, reliance


@dataclass
class Plan:
    budget: float
    currency: str
    lockfiles: list
    total_packages: int
    shares: list
    leftover: float
    unfunded: list = field(default_factory=list)   # (dep, reliance), most relied-on first
    unknown: int = 0                               # packages whose registry could not be queried
    funded_packages: int = 0
    excluded: int = 0


def merge_graphs(graphs):
    merged = {}
    for g in graphs:
        for p in g.packages.values():
            m = merged.get(p.key)
            if m is None:
                merged[p.key] = Dep(p.ecosystem, p.name, p.version, set(p.deps), p.direct, p.dev, p.depth, p.dependents, p.root_dev)
            else:
                if p.direct:
                    m.root_dev = m.root_dev and p.root_dev if m.direct else p.root_dev
                m.direct |= p.direct
                m.dev &= p.dev
                m.depth = min(m.depth, p.depth)
                m.dependents = max(m.dependents, p.dependents)
    return list(merged.values())


def build_plan(paths, budget, currency="USD", exclude=(), include_dev=True, exponent=0.7, min_amount=5.0,
               max_share=0.3, top=None, skip_recipients=(), resolver=None, cache_path=None, offline=False, workers=12,
               repo_lookup=True, progress=None):
    files = []
    for path in paths:
        files += lockfiles.find_lockfiles(path)
    if not files:
        raise FileNotFoundError("no lockfile found (package-lock.json, yarn.lock, pnpm-lock.yaml, poetry.lock, "
                                "uv.lock, requirements.txt, Cargo.lock, go.mod)")
    deps = merge_graphs([lockfiles.parse(f) for f in files])
    excluded = [d for d in deps if is_excluded(d.name, exclude)]
    deps = [d for d in deps if not is_excluded(d.name, exclude)]
    if not include_dev:
        deps = [d for d in deps if not d.dev]
    cache = Cache(cache_path) if resolver is None else resolver.cache
    resolver = resolver or Resolver(cache=cache, offline=offline, repo_lookup=repo_lookup)

    done = [0]

    def look(dep):
        info = resolver.lookup(dep)
        done[0] += 1
        if progress:
            progress(done[0], len(deps))
        return info

    with ThreadPoolExecutor(workers) as pool:
        infos = list(pool.map(look, deps))
    cache.save()

    funded = [(d, i.links) for d, i in zip(deps, infos) if i.links]
    unfunded = sorted(((d, reliance(d)) for d, i in zip(deps, infos) if not i.links and i.status in ("ok", "not-found")),
                      key=lambda t: -t[1])
    recipients = [r for r in group_by_recipient(funded) if not is_excluded(r.link.url.removeprefix("https://"), skip_recipients)]
    shares, leftover = split(recipients, budget, exponent, min_amount, max_share, top)
    return Plan(budget, currency, [str(f) for f in files], len(deps) + len(excluded), shares, leftover / 100, unfunded,
                sum(1 for i in infos if i.status in ("error", "offline")), len(funded), len(excluded))
