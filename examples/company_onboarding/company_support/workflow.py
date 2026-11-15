"""A company's order-support workflow. No scheduler or ledger code here."""


class OrderWorkflow:
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["order_id"],
        "properties": {"order_id": {"type": "string", "enum": ["ORDER-100"]}},
    }

    def validate(self, task):
        if set(task) != {"id", "owner", "order_id"} or task["order_id"] != "ORDER-100":
            raise ValueError("Use order_id ORDER-100 for this local example")

    async def execute(self, task, call):
        orders = await call("order-v1", "order", {"order_id": task["order_id"]})
        deliveries = await call(
            "delivery-v1", "delivery", {"shipment_id": orders[0]["shipment_id"]}
        )
        return {"order_id": task["order_id"], "delivery": deliveries[0]}


def build(options):
    if options:
        raise ValueError("Order workflow accepts no options")
    return OrderWorkflow()
