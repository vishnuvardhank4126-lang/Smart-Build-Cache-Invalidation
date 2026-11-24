"""Load a user build file that declares the target graph.

A build file is an ordinary Python module that exposes either a module-level
``TARGETS`` list or a ``build_graph()`` function returning one.  Using Python
instead of a bespoke format keeps the tool dependency-free and lets authors
compute targets with loops/conditionals.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List, Sequence, Union

from .model import BuildError, Target

__all__ = ["DEFAULT_BUILD_FILES", "find_build_file", "load_targets"]

DEFAULT_BUILD_FILES: Sequence[str] = ("build.py", "buildcachefile.py", "targets.py")


def find_build_file(project_dir: Union[str, Path]) -> Path:
    """Return the first known build file found in ``project_dir``."""
    project_dir = Path(project_dir)
    for name in DEFAULT_BUILD_FILES:
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    raise BuildError(
        f"no build file found in {project_dir} "
        f"(looked for: {', '.join(DEFAULT_BUILD_FILES)})"
    )


def load_targets(
    build_file: Union[str, Path],
    project_dir: Union[str, Path, None] = None,
) -> List[Target]:
    """Execute ``build_file`` and return the validated list of targets."""
    build_file = Path(build_file)
    if not build_file.is_file():
        raise BuildError(f"build file not found: {build_file}")

    project_dir = Path(project_dir) if project_dir is not None else build_file.parent
    module_name = "_buildcache_user_build"

    # Allow the build file to import sibling helper modules.
    added_path = False
    project_str = str(project_dir.resolve())
    if project_str not in sys.path:
        sys.path.insert(0, project_str)
        added_path = True

    spec = importlib.util.spec_from_file_location(module_name, build_file)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise BuildError(f"cannot load build file: {build_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BuildError:
        raise
    except Exception as exc:  # surface authoring mistakes with context
        raise BuildError(f"error while loading {build_file}: {exc}") from exc
    finally:
        if added_path:
            try:
                sys.path.remove(project_str)
            except ValueError:  # pragma: no cover - defensive
                pass

    raw = getattr(module, "TARGETS", None)
    if raw is None and hasattr(module, "build_graph"):
        raw = module.build_graph()
    if raw is None:
        raise BuildError(
            f"{build_file} must define a TARGETS list or a build_graph() function"
        )

    targets: List[Target] = []
    for item in raw:
        if not isinstance(item, Target):
            raise BuildError(
                f"{build_file}: expected Target objects, got {type(item).__name__} "
                f"({item!r})"
            )
        targets.append(item)
    if not targets:
        raise BuildError(f"{build_file} declared no targets")
    return targets
