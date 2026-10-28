from dataclasses import dataclass
from typing import Any


@dataclass(eq=False, frozen=True)
class Key[T]:
    name: str

    @property
    def __name__(self) -> str:
        return self.name


type ServiceKey[T] = type[T] | Key[T]


class MissingService(LookupError):
    def __init__(self, key: ServiceKey[Any]) -> None:
        self.key = key
        super().__init__(f"Service {key.__name__} is not available")
