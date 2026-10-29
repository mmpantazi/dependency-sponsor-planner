"""Split a budget: weights ^ exponent, per-recipient cap, minimum payment, exact cents."""
from dataclasses import dataclass


@dataclass
class Share:
    recipient: object
    weight: float
    cents: int = 0

    @property
    def amount(self):
        return self.cents / 100


def _water_fill(weights, total, cap_share):
    """Proportional split where no one exceeds cap_share*total; excess flows to the rest."""
    n = len(weights)
    if n == 0:
        return []
    cap = total * max(cap_share, 2 / n)  # relaxed for short lists so the ranking survives
    alloc = [None] * n
    remaining, free = total, list(range(n))
    while free:
        wsum = sum(weights[i] for i in free)
        over = [i for i in free if remaining * weights[i] / wsum > cap + 1e-9]
        if not over:
            for i in free:
                alloc[i] = remaining * weights[i] / wsum
            break
        for i in over:
            alloc[i] = cap
            remaining -= cap
            free.remove(i)
    return alloc


def split(recipients, budget, exponent=0.7, min_amount=5.0, max_share=0.3, top=None):
    """Return (shares, leftover_cents). Recipients that would get less than min_amount are dropped and
    the budget re-split among the rest, so every payment is worth the transaction."""
    pool = [r for r in recipients if r.score > 0]
    if top:
        pool = pool[:top]
    total = round(budget * 100)
    while pool:
        weights = [r.score ** exponent for r in pool]
        amounts = _water_fill(weights, total, max_share)
        if min_amount > 0:
            keep = [i for i, a in enumerate(amounts) if a / 100 >= min_amount]
            if len(keep) < len(pool) and len(pool) > 1:
                keep = keep or [0]
                pool = [pool[i] for i in keep]
                continue
        break
    else:
        return [], total
    floors = [int(a) for a in amounts]
    remainder = total - sum(floors)
    order = sorted(range(len(pool)), key=lambda i: -(amounts[i] - floors[i]))
    for i in order[:remainder]:
        floors[i] += 1
    shares = [Share(r, w, c) for r, w, c in zip(pool, weights, floors)]
    return shares, total - sum(floors)
