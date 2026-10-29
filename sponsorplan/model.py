from dataclasses import dataclass, field


@dataclass
class Dep:
    ecosystem: str
    name: str
    version: str = ""
    deps: set = field(default_factory=set)
    direct: bool = False
    dev: bool = False
    depth: int = 0
    dependents: int = 0
    root_dev: bool = False  # declared only as a development dependency by the project itself

    @property
    def key(self):
        return (self.ecosystem, self.name)


@dataclass
class Graph:
    """Packages of one lockfile. `roots` are the project's own direct deps,
    given as (name, is_dev). Edges in Dep.deps are names within the same ecosystem."""
    ecosystem: str
    packages: dict = field(default_factory=dict)
    roots: list = field(default_factory=list)
    source: str = ""

    def add(self, name, version="", deps=()):
        p = self.packages.get(name)
        if p is None:
            p = self.packages[name] = Dep(self.ecosystem, name, version)
        elif version and not p.version:
            p.version = version
        p.deps.update(d for d in deps if d != name)
        return p

    def finalize(self, default_roots=None):
        """Compute direct/dev/depth/dependents from roots (or from in-degree-0 packages)."""
        roots = [(n, d) for n, d in self.roots if n in self.packages]
        if not roots:
            indeg = {n: 0 for n in self.packages}
            for p in self.packages.values():
                for d in p.deps:
                    if d in indeg:
                        indeg[d] += 1
            roots = [(n, False) for n, c in indeg.items() if c == 0] or \
                    [(n, False) for n in self.packages]
        for p in self.packages.values():
            p.direct = False
            p.root_dev = False
            p.dev = True
            p.depth = 0
            p.dependents = 0
        declared = {}
        for n, is_dev in roots:
            declared[n] = declared.get(n, True) and is_dev
        for n, only_dev in declared.items():
            self.packages[n].direct = True
            self.packages[n].root_dev = only_dev
        for p in self.packages.values():
            for d in p.deps:
                if d in self.packages:
                    self.packages[d].dependents += 1
        for want_dev in (False, True):
            frontier = [n for n, is_dev in roots if is_dev == want_dev]
            seen = set()
            level = 1
            while frontier:
                nxt = []
                for n in frontier:
                    if n in seen:
                        continue
                    seen.add(n)
                    p = self.packages[n]
                    if want_dev and not p.dev:
                        continue  # already reachable from production
                    if p.depth == 0 or level < p.depth:
                        p.depth = level
                    if not want_dev:
                        p.dev = False
                    nxt.extend(d for d in p.deps if d in self.packages)
                frontier = nxt
                level += 1
        for p in self.packages.values():
            if p.depth == 0:
                p.depth = 1
        return self
