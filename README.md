# Dependency Sponsor Planner

Reads your lockfiles, finds which dependencies accept money (registry `funding` fields, PyPI project links,
`.github/FUNDING.yml` in the package repository or the owner's `.github` repo; npm's `https://github.com/org/repo?sponsor=1` links are resolved through that repo's `FUNDING.yml`), ranks them by how much your project
relies on them and splits a yearly budget between the places that can receive it.
Zero dependencies, Python 3.11+. Romanian output: `--lang ro` (*arată ce biblioteci poți sprijini și împarte un buget între ele*).

```bash
python -m sponsorplan . --budget 1200                  # scan the current folder
python -m sponsorplan app/ api/ -b 5000 -c EUR -f md -o plan.md
python -m sponsorplan . --devs 8                       # 8 x 2000 = Open Source Pledge level budget
python -m sponsorplan . -b 1000 -x '@mycompany/*' --no-dev --top 15
python -m sponsorplan . -b 1000 --skip-recipient 'github.com/sponsors/myorg'   # never pay your own projects
python -m sponsorplan . -b 1000 --verify               # skip funding pages that 404 / have no Sponsors profile
python -m sponsorplan . -b 1000 --detail               # show which packages sit behind each recipient
python -m sponsorplan . -b 1000 --offline              # answers from the local cache only
pip install .  &&  sponsor-plan . -b 1000              # optional: installs the `sponsor-plan` command
```

Supported lockfiles: `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock` (v1 and Berry), `pnpm-lock.yaml`,
`poetry.lock`, `uv.lock`, `requirements.txt` (follows `-r` includes), `Cargo.lock`, `go.mod`, `composer.lock`, `Gemfile.lock`. Folders are searched recursively
(skipping `node_modules`, `vendor`, virtualenvs). Several lockfiles are merged into one plan.
A package declared only as a devDependency but also pulled in by production code counts at the higher of the two weights.

## How the money is split

1. **Reliance score per package.** Direct production dependency = 1.0, direct dev = 0.3, transitive production = 0.4
   (×0.75 for each level deeper than 2), transitive dev = 0.1. Multiplied by `1 + 0.5·log2(1 + dependents)`, where
   dependents = how many other packages in your lockfile pull it in. Direct/dev/depth come from the lockfile
   (and `package.json` / `pyproject.toml` beside it when the lockfile does not say).
2. **Pooling.** Packages that share a funding page (all `@babel/*`, a maintainer with 20 packages) are paid once;
   their scores add up. Where several routes exist the order is GitHub Sponsors, Open Collective, Liberapay, Ko-fi, Polar,
   thanks.dev, Patreon, Buy Me a Coffee, Tidelift, other; the rest are kept as `other_links` in JSON.
3. **Split.** Weight = pooled score ^ `--exponent` (default 0.7; 1 = strictly proportional, lower = flatter so small
   but important projects are not crowded out). No recipient gets more than `--max-share` (default 30%, relaxed to 2/N
   for short lists); payments under `--min` (default 5) are dropped and the budget is re-split; cents are allocated
   by largest remainder so the total is exact.

The report also lists heavily used packages with **no** funding route found; money can't help there, code and bug reports can.

## Data sources and limits

npm registry, PyPI JSON API, crates.io API, Packagist (`funding` field), RubyGems (`funding_uri`) and `raw.githubusercontent.com` (no tokens needed, no GitHub API quota).
Answers are cached 7 days in `~/.cache/sponsor-planner/cache.json` (`--cache`, or `SPONSOR_PLAN_CACHE`);
failed lookups are never cached as "no funding". The registry's *latest* release is checked, not the locked version.
Tidelift pages are per package subscriptions for companies and rank last. Maven and NuGet lockfiles are not supported yet; `Gemfile.lock` has no dev/prod information, so all gems count as production. Non-GitHub repositories only count if the registry
lists a funding link. Amounts are labels (`--currency`), nothing is converted and **nothing is paid**: the output is a plan
you carry out yourself (or with a service like GitHub Sponsors, Open Collective or thanks.dev).

## Stale links and `--verify`

`FUNDING.yml` files and registry metadata go stale. With `--verify` every recipient page is requested once
(cached 7 days): a 404, or a GitHub `sponsors/<user>` URL that redirects to the profile (Sponsors not enabled),
is reported and skipped, and the budget is re-split among the live pages. On the sample npm project
this removed two dead GitHub Sponsors pages. If a page cannot be reached the recipient is kept and counted as unverified.

## Formats

`-f text` (default), `md`, `csv`, `json` (includes per-package reliance and alternate links).
Exit code 0 = plan produced, 1 = lockfile found but no funding routes, 2 = usage error.

## Tests

```bash
python -m unittest discover -s tests     # 55 tests, no network
```
