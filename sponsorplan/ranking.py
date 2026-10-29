"""How much does this project rely on a package? A transparent score, not a download count."""
import fnmatch
import math
from dataclasses import dataclass, field

DIRECT_PROD, DIRECT_DEV, TRANSITIVE_PROD, TRANSITIVE_DEV = 1.0, 0.3, 0.4, 0.1
DEPTH_DECAY = 0.75


def reliance(dep):
    """Tier weight, decayed with depth, boosted by how many other packages pull it in."""
    transitive = (TRANSITIVE_DEV if dep.dev else TRANSITIVE_PROD) * DEPTH_DECAY ** max(dep.depth - 2, 0)
    if dep.direct:
        # a devDependency that production code also pulls in transitively counts at least as that
        tier = DIRECT_DEV if dep.root_dev else DIRECT_PROD
        if dep.root_dev and not dep.dev:
            tier = max(tier, transitive)
    else:
        tier = transitive
    return tier * (1 + 0.5 * math.log2(1 + dep.dependents))


@dataclass
class Recipient:
    link: object
    packages: list = field(default_factory=list)  # (dep, score)
    other_links: list = field(default_factory=list)
    score: float = 0.0

    @property
    def label(self):
        return self.link.url.removeprefix("https://")


def is_excluded(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def group_by_recipient(funded, include_dev=True):
    """funded: iterable of (dep, links). Packages sharing a funding page (a monorepo, an org) pool their score."""
    from .funding import best_link
    groups = {}
    for dep, links in funded:
        if dep.dev and not include_dev:
            continue
        link = best_link(links)
        r = groups.setdefault(link.url, Recipient(link))
        for l in links:
            if l != r.link and l not in r.other_links:
                r.other_links.append(l)
        s = reliance(dep)
        r.packages.append((dep, s))
        r.score += s
    for r in groups.values():
        r.packages.sort(key=lambda t: -t[1])
    return sorted(groups.values(), key=lambda r: -r.score)
