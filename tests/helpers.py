"""Deterministic local tasks shared by accounting and recovery tests."""

import random


def tasks_for(seed):
    rng = random.Random(seed)
    tasks = [
        {
            "id": f"request-{user}-{n}",
            "owner": f"traveler-{user}",
            "route": f"R{rng.randrange(4)}",
            "bags": rng.randrange(3),
            "max_fare_cents": rng.choice((23000, 30000, 50000)),
        }
        for user in range(8)
        for n in range(2)
    ]
    rng.shuffle(tasks)
    return tasks
