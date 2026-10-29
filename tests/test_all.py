import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from sponsorplan import cli, lockfiles, report
from sponsorplan.allocate import split
from sponsorplan.funding import (Cache, Link, Resolver, best_link, github_repo, links_from_funding_yml, make_link,
                                 parse_funding_yml)
from sponsorplan.model import Dep
from sponsorplan.planner import build_plan
from sponsorplan.ranking import Recipient, group_by_recipient, reliance

FIX = Path(__file__).parent / "fixtures"


def load(sub, name):
    return lockfiles.parse(FIX / sub / name)


class LockfileTests(unittest.TestCase):
    def test_npm_v3_direct_dev_depth_and_fanin(self):
        g = load("npm", "package-lock.json")
        p = g.packages
        self.assertEqual(set(p), {"a", "b", "c", "d", "e", "f", "@s/pkg"})  # workspace link skipped
        self.assertTrue(p["a"].direct and not p["a"].dev)
        self.assertTrue(p["d"].direct and p["d"].dev)
        self.assertEqual(p["c"].depth, 2)
        self.assertFalse(p["c"].dev)            # reachable from production through a
        self.assertEqual(p["c"].dependents, 3)  # a, b, d
        self.assertTrue(p["f"].dev)
        self.assertEqual(p["e"].depth, 3)

    def test_yarn_v1_scoped_and_multi_spec(self):
        g = load("yarn", "yarn.lock")
        self.assertEqual(set(g.packages), {"@scope/x", "y"})
        self.assertEqual(g.packages["@scope/x"].version, "1.2.3")
        self.assertTrue(g.packages["@scope/x"].direct)
        self.assertFalse(g.packages["y"].direct)

    def test_yarn_berry(self):
        g = load("yarn-berry", "yarn.lock")
        self.assertEqual(set(g.packages), {"lodash", "@types/node", "x"})
        self.assertEqual(g.packages["lodash"].version, "4.17.21")
        self.assertTrue(g.packages["x"].direct)
        self.assertEqual(g.packages["lodash"].depth, 2)

    def test_pnpm_v9(self):
        g = load("pnpm", "pnpm-lock.yaml")
        self.assertEqual(set(g.packages), {"foo", "baz", "@types/bar"})
        self.assertTrue(g.packages["foo"].direct)
        self.assertTrue(g.packages["@types/bar"].dev)
        self.assertEqual(g.packages["baz"].depth, 2)

    def test_poetry_normalises_names_and_uses_pyproject(self):
        g = load("poetry", "poetry.lock")
        self.assertIn("requests", g.packages)
        self.assertTrue(g.packages["requests"].direct)
        self.assertTrue(g.packages["pytest"].dev)
        self.assertEqual(g.packages["idna"].depth, 2)

    def test_uv_virtual_root(self):
        g = load("uv", "uv.lock")
        self.assertNotIn("myapp", g.packages)
        self.assertTrue(g.packages["httpx"].direct)
        self.assertEqual(g.packages["anyio"].depth, 2)
        self.assertTrue(g.packages["ruff"].dev)

    def test_cargo_local_crate_is_root(self):
        g = load("cargo", "Cargo.lock")
        self.assertNotIn("myapp", g.packages)
        self.assertTrue(g.packages["serde"].direct)
        self.assertFalse(g.packages["serde_derive"].direct)

    def test_go_indirect(self):
        g = load("go", "go.mod")
        self.assertTrue(g.packages["github.com/spf13/cobra"].direct)
        self.assertFalse(g.packages["github.com/spf13/pflag"].direct)
        self.assertEqual(len(g.packages), 3)

    def test_requirements(self):
        g = load("req", "requirements.txt")
        self.assertEqual(set(g.packages), {"flask", "requests"})
        self.assertEqual(g.packages["flask"].version, "3.0.0")

    def test_find_lockfiles_skips_node_modules(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "node_modules" / "x").mkdir(parents=True)
            (Path(d) / "node_modules" / "x" / "package-lock.json").write_text("{}")
            (Path(d) / "go.mod").write_text("module m\n")
            self.assertEqual([p.name for p in lockfiles.find_lockfiles(d)], ["go.mod"])

    def test_no_roots_falls_back_to_in_degree_zero(self):
        from sponsorplan.model import Graph
        g = Graph("npm")
        g.add("top", deps=["mid"])
        g.add("mid", deps=["leaf"])
        g.add("leaf")
        g.finalize()
        self.assertTrue(g.packages["top"].direct)
        self.assertEqual(g.packages["leaf"].depth, 3)


class FundingTests(unittest.TestCase):
    YML = """# comment
github: [ljharb, "sindresorhus"]
patreon: someone
open_collective: express  # trailing
custom:
  - https://example.org/donate
  - "www.example.com/pay"
tidelift: npm/lodash
ko_fi:
"""

    def test_parse(self):
        d = parse_funding_yml(self.YML)
        self.assertEqual(d["github"], ["ljharb", "sindresorhus"])
        self.assertEqual(d["open_collective"], ["express"])
        self.assertEqual(d["custom"], ["https://example.org/donate", "www.example.com/pay"])
        self.assertEqual(d["ko_fi"], [])

    def test_links(self):
        urls = {l.url for l in links_from_funding_yml(self.YML)}
        self.assertIn("https://github.com/sponsors/ljharb", urls)
        self.assertIn("https://opencollective.com/express", urls)
        self.assertIn("https://example.com/pay", urls)
        self.assertIn("https://tidelift.com/funding/github/npm/lodash", urls)

    def test_make_link_canonical(self):
        self.assertEqual(make_link("https://www.GitHub.com/sponsors/Foo/?x=1"), Link("github-sponsors", "https://github.com/sponsors/Foo"))
        self.assertEqual(make_link("https://opencollective.com/babel/donate").url, "https://opencollective.com/babel")
        self.assertIsNone(make_link("https://github.com/foo/bar"))  # a repo is not a funding page
        self.assertIsNone(make_link(""))
        self.assertIsNone(make_link("https://example.com"))

    def test_best_link_prefers_github_sponsors(self):
        ls = [make_link("https://patreon.com/x"), make_link("https://github.com/sponsors/x"), make_link("https://opencollective.com/x")]
        self.assertEqual(best_link(ls).kind, "github-sponsors")

    def test_github_repo_spellings(self):
        for u in ("git+https://github.com/o/r.git", "git://github.com/o/r.git", "github:o/r",
                  "git@github.com:o/r.git", "https://github.com/o/r/tree/main/packages/x"):
            self.assertEqual(github_repo(u), ("o", "r"), u)
        self.assertIsNone(github_repo("https://gitlab.com/o/r"))


class RegistryTests(unittest.TestCase):
    def lookup(self, eco, name, version, routes):
        http = FakeHttp(routes)
        http.retries = 0
        return Resolver(http, Cache()).lookup(Dep(eco, name, version)), http

    def test_pypi_project_urls_and_release_document(self):
        doc = json.dumps({"info": {"project_urls": {"Funding": "https://github.com/sponsors/pallets", "Source": "https://github.com/pallets/flask"}}})
        info, http = self.lookup("pypi", "flask", "3.0.0", {"https://pypi.org/pypi/flask/3.0.0/json": doc})
        self.assertEqual(info.links[0].url, "https://github.com/sponsors/pallets")
        self.assertEqual(len(http.calls), 1)

    def test_pypi_falls_back_to_repo_funding_yml(self):
        doc = json.dumps({"info": {"project_urls": {"Homepage": "https://github.com/o/r"}}})
        info, _ = self.lookup("pypi", "p", "", {"https://pypi.org/pypi/p/json": doc,
                                               "https://raw.githubusercontent.com/o/r/HEAD/.github/FUNDING.yml": "open_collective: r\n"})
        self.assertEqual(info.links[0].url, "https://opencollective.com/r")

    def test_org_level_funding_yml(self):
        doc = json.dumps({"crate": {"repository": "https://github.com/o/r"}})
        info, _ = self.lookup("cargo", "r", "1.0", {"https://crates.io/api/v1/crates/r": doc,
                                                   "https://raw.githubusercontent.com/o/.github/HEAD/FUNDING.yml": "github: o\n"})
        self.assertEqual(info.links[0].url, "https://github.com/sponsors/o")

    def test_go_module_path_maps_to_github(self):
        info, _ = self.lookup("go", "github.com/o/r/v2", "v2.0.0", {"https://raw.githubusercontent.com/o/r/HEAD/.github/FUNDING.yml": "github: o\n"})
        self.assertEqual(info.links[0].url, "https://github.com/sponsors/o")
        info, _ = self.lookup("go", "golang.org/x/sys", "v0.1.0", {})
        self.assertEqual(info.links, [])

    def test_network_failure_is_not_cached_as_unfunded(self):
        cache = Cache()
        http = FakeHttp({"https://registry.npmjs.org/z/latest": (0, "")})
        http.retries = 0
        info = Resolver(http, cache).lookup(Dep("npm", "z"))
        self.assertEqual(info.status, "error")
        self.assertIsNone(cache.get("pkg:npm:z"))


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url):
        self.calls.append(url)
        for k, v in self.routes.items():
            if url.startswith(k):
                return (200, v) if isinstance(v, str) else v
        return 404, ""


NPM_ROUTES = {
    "https://registry.npmjs.org/a/latest": json.dumps({"funding": {"url": "https://opencollective.com/a", "type": "opencollective"}}),
    "https://registry.npmjs.org/b/latest": json.dumps({"repository": {"url": "git+https://github.com/o/b.git"}}),
    "https://registry.npmjs.org/c/latest": json.dumps({"funding": ["https://github.com/sponsors/zed", "https://patreon.com/z"]}),
    "https://registry.npmjs.org/d/latest": json.dumps({"funding": "https://opencollective.com/a"}),
    "https://registry.npmjs.org/e/latest": json.dumps({"repository": "github:o/e"}),
    "https://registry.npmjs.org/f/latest": (500, ""),
    "https://registry.npmjs.org/@s%2fpkg/latest": json.dumps({}),
    "https://raw.githubusercontent.com/o/b/HEAD/.github/FUNDING.yml": "github: bob\n",
}


class PlannerTests(unittest.TestCase):
    def plan(self, **kw):
        http = FakeHttp(NPM_ROUTES)
        http.retries = 0
        res = Resolver(http, Cache(), repo_lookup=kw.pop("repo_lookup", True))
        return build_plan([FIX / "npm"], kw.pop("budget", 100), resolver=res, min_amount=kw.pop("min_amount", 0), **kw), http

    def test_resolution_paths(self):
        plan, http = self.plan()
        by = {s.recipient.link.url: s for s in plan.shares}
        self.assertIn("https://opencollective.com/a", by)
        self.assertEqual({p.name for p, _ in by["https://opencollective.com/a"].recipient.packages}, {"a", "d"})  # pooled
        self.assertIn("https://github.com/sponsors/bob", by)       # FUNDING.yml fallback
        self.assertIn("https://github.com/sponsors/zed", by)       # best of several
        self.assertEqual(plan.unknown, 1)                          # f -> HTTP 500
        self.assertEqual(sum(s.cents for s in plan.shares), 10000)

    def test_no_repo_lookup_skips_raw_requests(self):
        _, http = self.plan(repo_lookup=False)
        self.assertFalse([u for u in http.calls if "raw.githubusercontent" in u])

    def test_exclude_and_no_dev(self):
        plan, _ = self.plan(exclude=["a"], include_dev=False)
        names = {p.name for s in plan.shares for p, _ in s.recipient.packages}
        self.assertNotIn("a", names)
        self.assertNotIn("d", names)
        self.assertEqual(plan.excluded, 1)

    def test_cache_makes_second_run_free(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cp = Path(d) / "c.json"
            http = FakeHttp(NPM_ROUTES)
            http.retries = 0
            build_plan([FIX / "npm"], 100, resolver=Resolver(http, Cache(cp)), min_amount=0)
            self.assertTrue(cp.exists())
            n = len(http.calls)
            http2 = FakeHttp({})
            plan = build_plan([FIX / "npm"], 100, cache_path=cp, offline=True, min_amount=0)
            self.assertEqual(http2.calls, [])
            self.assertTrue(plan.shares)
            self.assertGreater(n, 0)

    def test_offline_without_cache_reports_unchecked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            plan = build_plan([FIX / "npm"], 100, cache_path=Path(d) / "none.json", offline=True)
            self.assertEqual(plan.unknown, plan.total_packages)
            self.assertEqual(plan.shares, [])

    def test_missing_lockfile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                build_plan([d], 10)


def recip(name, score):
    r = Recipient(Link("github-sponsors", f"https://github.com/sponsors/{name}"))
    r.score = score
    r.packages = [(Dep("npm", name), score)]
    return r


class AllocateTests(unittest.TestCase):
    def test_sums_exactly_and_orders(self):
        rs = [recip("a", 5), recip("b", 3), recip("c", 1)]
        shares, left = split(rs, 100.01, exponent=1, min_amount=0, max_share=1)
        self.assertEqual(sum(s.cents for s in shares) + left, 10001)
        self.assertEqual(left, 0)
        self.assertGreater(shares[0].cents, shares[1].cents)

    def test_cap_redistributes(self):
        rs = [recip("a", 100)] + [recip(c, 1) for c in "bcdef"]
        shares, _ = split(rs, 100, exponent=1, min_amount=0, max_share=0.5)
        self.assertEqual(shares[0].cents, 5000)
        self.assertEqual(sum(s.cents for s in shares[1:]), 5000)

    def test_min_amount_drops_and_resplits(self):
        rs = [recip("a", 100), recip("b", 20), recip("c", 0.1)]
        shares, _ = split(rs, 50, exponent=1, min_amount=5, max_share=1)
        self.assertEqual([s.recipient.label for s in shares], ["github.com/sponsors/a", "github.com/sponsors/b"])
        self.assertEqual(sum(s.cents for s in shares), 5000)
        self.assertTrue(all(s.amount >= 5 for s in shares))

    def test_budget_below_min_goes_to_top(self):
        shares, _ = split([recip("a", 2), recip("b", 1)], 3, min_amount=5)
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].cents, 300)

    def test_exponent_flattens(self):
        rs = [recip("a", 100), recip("b", 1)]
        steep, _ = split(rs, 100, exponent=1, min_amount=0, max_share=1)
        flat, _ = split(rs, 100, exponent=0.3, min_amount=0, max_share=1)
        self.assertGreater(steep[0].cents, flat[0].cents)

    def test_top_and_tight_cap(self):
        rs = [recip(c, 1) for c in "abcd"]
        shares, _ = split(rs, 10, top=2, max_share=0.1, min_amount=0)
        self.assertEqual(sum(s.cents for s in shares), 1000)  # cap impossible -> still spends everything
        self.assertEqual(len(shares), 2)

    def test_cap_relaxes_for_short_lists(self):
        shares, _ = split([recip("a", 10), recip("b", 2), recip("c", 1)], 100, exponent=1, min_amount=0)
        self.assertGreater(shares[0].cents, shares[1].cents)
        self.assertGreater(shares[1].cents, shares[2].cents)

    def test_empty(self):
        self.assertEqual(split([], 10), ([], 1000))


class RankingTests(unittest.TestCase):
    def test_order_of_tiers(self):
        d = lambda **k: Dep("npm", "x", **k)
        direct = reliance(d(direct=True, depth=1))
        direct_dev = reliance(d(direct=True, dev=True, depth=1))
        trans = reliance(d(depth=2))
        deep = reliance(d(depth=5))
        trans_dev = reliance(d(dev=True, depth=2))
        self.assertGreater(direct, trans)
        self.assertGreater(trans, deep)
        self.assertGreater(trans, trans_dev)
        self.assertGreater(direct, direct_dev)

    def test_fan_in_boosts(self):
        self.assertGreater(reliance(Dep("npm", "x", depth=2, dependents=8)), reliance(Dep("npm", "x", depth=2, dependents=1)))

    def test_group_pools_by_funding_url(self):
        l = make_link("https://opencollective.com/babel")
        deps = [Dep("npm", f"@babel/p{i}", depth=2, dev=False) for i in range(3)]
        groups = group_by_recipient([(d, [l]) for d in deps])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].packages), 3)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_requires_budget(self):
        code, _, err = self.run_cli(str(FIX / "npm"))
        self.assertEqual(code, 2)
        self.assertIn("--budget", err)

    def test_no_lockfile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            code, _, err = self.run_cli(d, "-b", "10")
            self.assertEqual(code, 2)
            self.assertIn("no lockfile", err)

    def test_offline_formats_and_ro(self):
        import tempfile
        from sponsorplan.funding import Cache as C
        with tempfile.TemporaryDirectory() as d:
            cp = Path(d) / "c.json"
            http = FakeHttp(NPM_ROUTES)
            http.retries = 0
            build_plan([FIX / "npm"], 100, resolver=Resolver(http, C(cp)))
            base = [str(FIX / "npm"), "-b", "100", "--offline", "--cache", str(cp), "--min", "0"]
            code, out, _ = self.run_cli(*base)
            self.assertEqual(code, 0)
            self.assertIn("opencollective.com/a", out)
            _, js, _ = self.run_cli(*base, "-f", "json")
            data = json.loads(js)
            self.assertAlmostEqual(sum(r["year"] for r in data["recipients"]), 100)
            _, cs, _ = self.run_cli(*base, "-f", "csv")
            self.assertTrue(cs.startswith("recipient,platform,amount_year"))
            _, md, _ = self.run_cli(*base, "-f", "md")
            self.assertIn("| #", md)
            _, ro, _ = self.run_cli(*base, "-l", "ro")
            self.assertIn("Plan de sponsorizare", ro)
            self.assertIn("Pe an", ro)

    def test_devs_budget(self):
        code, _, err = self.run_cli(str(FIX / "npm"), "--devs", "3", "--offline", "--cache", "/nonexistent/x.json")
        self.assertEqual(code, 1)  # nothing cached -> no recipients, but budget accepted


if __name__ == "__main__":
    unittest.main()
