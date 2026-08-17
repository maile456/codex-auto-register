from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderAdapter:
    name: str
    result_field: str
    preferred_hosts: tuple[str, ...]

    def result(self, url: str) -> dict[str, str]:
        return {
            "provider_url": url,
            self.result_field: url,
        }
