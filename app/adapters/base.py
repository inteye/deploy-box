from abc import ABC, abstractmethod

from ..models import Environment, Release


class DeployAdapter(ABC):
    def __init__(self, environment: Environment) -> None:
        self.environment = environment

    @abstractmethod
    def trigger_deploy(self, release: Release, triggered_by: str | None) -> dict:
        raise NotImplementedError

    @abstractmethod
    def fetch_status(self) -> dict:
        raise NotImplementedError
