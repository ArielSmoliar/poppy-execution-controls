"""Bundled local provider. Tariffs are fictional execution units, not money."""

from poppy_controls.metered.backend import AirlineBackend, LocalAirlineAPI


class DemoBackend(AirlineBackend):
    def close(self):
        self.api.close()


def build(state_dir, options):
    if set(options) - {"fault", "query_units", "row_units"}:
        raise ValueError("Unknown demo backend option")
    return DemoBackend(LocalAirlineAPI(state_dir / "provider.sqlite", **options))
