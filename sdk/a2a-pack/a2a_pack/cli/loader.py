"""Load an :class:`A2AAgent` subclass from a string entrypoint."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from ..agent import A2AAgent


def load_agent_class(entrypoint: str, *, project_dir: Path | None = None) -> type[A2AAgent]:
    """Resolve ``module:ClassName`` to an :class:`A2AAgent` subclass.

    If ``project_dir`` is given, it is prepended to ``sys.path`` so a local
    ``agent.py`` can be imported without packaging.
    """
    if ":" not in entrypoint:
        raise ValueError(
            f"entrypoint must be 'module:ClassName' (got {entrypoint!r})"
        )
    module_name, class_name = entrypoint.split(":", 1)

    if project_dir is not None:
        resolved_project = project_dir.resolve()
        path = str(resolved_project)
        if path not in sys.path:
            sys.path.insert(0, path)
        _evict_stale_project_module(module_name, resolved_project)

    module = importlib.import_module(module_name)
    obj = getattr(module, class_name, None)
    if obj is None:
        raise AttributeError(f"{module_name} has no attribute {class_name!r}")
    if not isinstance(obj, type) or not issubclass(obj, A2AAgent):
        raise TypeError(f"{entrypoint} is not an A2AAgent subclass")
    return obj


def _evict_stale_project_module(module_name: str, project_dir: Path) -> None:
    """Avoid reusing ``agent.py`` from another local project.

    Local scaffolds commonly use the same entrypoint, ``agent:ClassName``.
    Python's import cache is process-wide, so test runs or long-lived CLI
    sessions can otherwise resolve a new project to an old temp directory.
    """
    module = sys.modules.get(module_name)
    if module is None:
        return
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return
    try:
        path = Path(module_file).resolve()
    except OSError:
        return
    if not path.is_relative_to(project_dir):
        del sys.modules[module_name]
