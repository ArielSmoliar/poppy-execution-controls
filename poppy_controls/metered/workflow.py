"""Replaceable business workflow; every billable call passes through the engine."""

from poppy_controls.metered.backend import units


class FlightWorkflow:
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["route", "bags", "max_fare_cents"],
        "properties": {
            "route": {"type": "string", "enum": ["R0", "R1", "R2", "R3"]},
            "bags": {"type": "integer", "minimum": 0, "maximum": 2},
            "max_fare_cents": {"type": "integer", "minimum": 0, "maximum": 1000000000},
        },
    }

    def validate(self, task):
        required = {"id", "owner", "route", "bags", "max_fare_cents"}
        if set(task) != required or not all(
            isinstance(task[k], str) and task[k] for k in ("id", "owner", "route")
        ):
            raise ValueError("Invalid task")
        if type(task["bags"]) is not int or task["bags"] not in (0, 1, 2):
            raise ValueError("Invalid bag count")
        units(task["max_fare_cents"])

    async def execute(self, task, call):
        flights = await call("search", "search", {"route": task["route"]})
        if not flights:
            return {"options": []}
        ids = [flight["id"] for flight in flights]
        # Separate tool calls: each gets its own bound, reservation and measured receipt.
        fares = await call("fares", "fares", {"ids": ids})
        statuses = await call("status", "status", {"ids": ids})
        operating = {r["id"] for r in statuses if r["status"] == "operating"}
        options = [
            {"id": r["id"], "total_cents": r["fare_cents"] + task["bags"] * r["bag_cents"]}
            for r in fares
            if r["id"] in operating
        ]
        options = [r for r in options if r["total_cents"] <= task["max_fare_cents"]]
        return {
            "options": sorted(options, key=lambda r: (r["total_cents"], r["id"]))[:2],
        }


def build(options):
    if options:
        raise ValueError("Flight workflow accepts no options")
    return FlightWorkflow()
