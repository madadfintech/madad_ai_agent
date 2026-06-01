"""Workflow registry — name+version lookup of workflow definitions."""

from __future__ import annotations

from .definition import WorkflowDefinition
from .errors import WorkflowAlreadyRegisteredError, WorkflowNotFoundError


class WorkflowRegistry:
    """Holds registered workflow definitions, keyed by name and version.

    Multiple versions of a workflow may coexist (in-flight runs pin their
    version); ``get`` with no version returns the latest.
    """

    def __init__(self) -> None:
        self._defs: dict[str, dict[int, WorkflowDefinition]] = {}

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        name = definition.name
        version = definition.version
        versions = self._defs.setdefault(name, {})
        if version in versions:
            raise WorkflowAlreadyRegisteredError(
                f"Workflow {name!r} v{version} is already registered"
            )
        versions[version] = definition
        return definition

    def get(self, name: str, version: int | None = None) -> WorkflowDefinition:
        versions = self._defs.get(name)
        if not versions:
            raise WorkflowNotFoundError(f"Workflow {name!r} is not registered")
        if version is None:
            version = max(versions)
        try:
            return versions[version]
        except KeyError as exc:
            raise WorkflowNotFoundError(
                f"Workflow {name!r} v{version} is not registered"
            ) from exc

    def latest_version(self, name: str) -> int:
        versions = self._defs.get(name)
        if not versions:
            raise WorkflowNotFoundError(f"Workflow {name!r} is not registered")
        return max(versions)

    def exists(self, name: str, version: int | None = None) -> bool:
        versions = self._defs.get(name)
        if not versions:
            return False
        return True if version is None else version in versions

    def list_workflows(self) -> list[tuple[str, int]]:
        return [(n, v) for n, vs in self._defs.items() for v in sorted(vs)]
