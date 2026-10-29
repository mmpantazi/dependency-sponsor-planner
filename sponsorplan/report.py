import csv
import io
import json

STRINGS = {
    "en": {
        "title": "Dependency sponsorship plan", "budget": "Budget per year", "lockfiles": "Lockfiles",
        "scanned": "{n} packages scanned, {f} accept funding ({r} recipients after grouping by funding page)",
        "unknown": "Packages that could not be checked (registry unreachable or offline cache miss): {n}",
        "recipient": "Where to send it", "amount": "Per year", "month": "Per month", "share": "Share",
        "covers": "Covers (top packages)", "kind": "Platform", "leftover": "Unallocated rounding: {x}",
        "unfunded": "Heavily used, but no funding link found",
        "unfunded_hint": "No money route found. Consider code, bug reports or thanking them instead.",
        "nothing": "No funding links found for any package.", "excluded": "Packages excluded by --exclude: {n}",
        "package": "Package", "score": "Reliance", "more": "+{n} more",
        "dead": "Funding pages that no longer exist (skipped): {n}",
        "unverified": "Funding pages that could not be verified: {n}", "detail": "Packages behind each recipient",
        "direct": "direct", "dev": "dev", "indirect": "indirect",
    },
    "ro": {
        "title": "Plan de sponsorizare a dependențelor", "budget": "Buget pe an", "lockfiles": "Fișiere lock",
        "scanned": "{n} pachete analizate, {f} acceptă finanțare ({r} destinatari după grupare pe pagina de finanțare)",
        "unknown": "Pachete care nu au putut fi verificate (registru inaccesibil sau lipsă în cache offline): {n}",
        "recipient": "Unde trimiți banii", "amount": "Pe an", "month": "Pe lună", "share": "Pondere",
        "covers": "Acoperă (pachete principale)", "kind": "Platformă", "leftover": "Rest nealocat din rotunjire: {x}",
        "unfunded": "Folosite intens, dar fără link de finanțare",
        "unfunded_hint": "Nu există o cale de plată. Poți contribui cu cod, rapoarte de bug sau mulțumiri.",
        "nothing": "Niciun pachet nu are link de finanțare.", "excluded": "Pachete excluse prin --exclude: {n}",
        "package": "Pachet", "score": "Dependență", "more": "+{n} altele",
        "dead": "Pagini de finanțare care nu mai există (omise): {n}",
        "unverified": "Pagini de finanțare care nu au putut fi verificate: {n}", "detail": "Pachetele din spatele fiecărui destinatar",
        "direct": "direct", "dev": "dev", "indirect": "indirect",
    },
}


def _row(plan, s, t):
    names = [p.name for p, _ in s.recipient.packages]
    covers = ", ".join(names[:4]) + (f" ({t['more'].format(n=len(names) - 4)})" if len(names) > 4 else "")
    pct = 100 * s.cents / max(round(plan.budget * 100), 1)
    return {"recipient": s.recipient.link.url, "kind": s.recipient.link.kind, "year": s.amount,
            "month": round(s.amount / 12, 2), "share": round(pct, 1), "covers": covers,
            "packages": len(names), "other_links": [l.url for l in s.recipient.other_links]}


def rows(plan, lang="en"):
    t = STRINGS[lang]
    return [_row(plan, s, t) for s in plan.shares]


def money(x, cur):
    return f"{x:,.2f} {cur}"


def render_text(plan, lang="en", unfunded_limit=10, markdown=False, detail=False):
    t = STRINGS[lang]
    out = []
    h = (lambda s: f"## {s}") if markdown else (lambda s: s)
    out.append(("# " if markdown else "") + t["title"])
    out.append(f"{t['budget']}: {money(plan.budget, plan.currency)}  |  {t['lockfiles']}: {', '.join(plan.lockfiles)}")
    out.append(t["scanned"].format(n=plan.total_packages, f=plan.funded_packages, r=len(plan.shares)))
    if plan.excluded:
        out.append(t["excluded"].format(n=plan.excluded))
    if plan.unknown:
        out.append(t["unknown"].format(n=plan.unknown))
    if plan.dead:
        out.append(t["dead"].format(n=len(plan.dead)) + ": " + ", ".join(u.removeprefix("https://") for u in plan.dead))
    if plan.unverified:
        out.append(t["unverified"].format(n=plan.unverified))
    out.append("")
    data = rows(plan, lang)
    if not data:
        out.append(t["nothing"])
    else:
        out.append(h(t["recipient"]))
        head = ["#", t["recipient"], t["kind"], t["amount"], t["month"], t["share"], t["covers"]]
        body = [[str(i), r["recipient"], r["kind"], f"{r['year']:,.2f}", f"{r['month']:,.2f}", f"{r['share']}%", r["covers"]]
                for i, r in enumerate(data, 1)]
        out.append(_table(head, body, markdown))
        if plan.leftover:
            out.append("")
            out.append(t["leftover"].format(x=money(plan.leftover, plan.currency)))
    if detail and plan.shares:
        out.append("")
        out.append(h(t["detail"]))
        for i, sh in enumerate(plan.shares, 1):
            out.append(f"{i}. {sh.recipient.link.url}")
            for dep, score in sh.recipient.packages:
                kind = t["direct"] if dep.direct else t["indirect"]
                out.append(f"   {dep.name} {dep.version}".rstrip() + f"  [{kind}{', ' + t['dev'] if dep.dev else ''}]  {score:.2f}")
    if plan.unfunded and unfunded_limit:
        out.append("")
        out.append(h(t["unfunded"]))
        head = [t["package"], t["score"]]
        body = [[f"{d.name} ({d.ecosystem})", f"{s:.2f}"] for d, s in plan.unfunded[:unfunded_limit]]
        out.append(_table(head, body, markdown))
        out.append(t["unfunded_hint"])
    return "\n".join(out) + "\n"


def _table(head, body, markdown):
    if markdown:
        lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
        lines += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in body]
        return "\n".join(lines)
    widths = [max(len(x) for x in col) for col in zip(head, *body)]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([fmt(head), fmt(["-" * w for w in widths])] + [fmt(r) for r in body])


def render_csv(plan):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["recipient", "platform", "amount_year", "amount_month", "currency", "share_percent", "packages"])
    for r in rows(plan):
        w.writerow([r["recipient"], r["kind"], f"{r['year']:.2f}", f"{r['month']:.2f}", plan.currency, r["share"], r["covers"]])
    return buf.getvalue()


def render_json(plan):
    return json.dumps({
        "budget": plan.budget, "currency": plan.currency, "lockfiles": plan.lockfiles,
        "packages_scanned": plan.total_packages, "packages_funded": plan.funded_packages,
        "unchecked": plan.unknown, "dead_pages": plan.dead, "leftover": plan.leftover,
        "recipients": [{**r, "packages_detail": [{"name": p.name, "ecosystem": p.ecosystem, "reliance": round(sc, 3)}
                                                 for p, sc in s.recipient.packages]}
                       for r, s in zip(rows(plan), plan.shares)],
        "unfunded": [{"name": d.name, "ecosystem": d.ecosystem, "reliance": round(s, 3)} for d, s in plan.unfunded[:50]],
    }, indent=2, ensure_ascii=False) + "\n"
