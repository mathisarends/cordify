from dataclasses import dataclass


@dataclass(eq=False, frozen=True)
class Key[T]:
    name: str

    @property
    def __name__(self) -> str:
        return self.name


type ServiceKey[T] = type[T] | Key[T]
