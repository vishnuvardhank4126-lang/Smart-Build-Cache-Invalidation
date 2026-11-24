"""Core data types shared by every module of the build engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Union


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BuildError(Exception):
    """Base class for every error raised by the build engine."""


class GraphError(BuildError):
    """The declared target graph is invalid (unknown dependency, duplicate...)."""


class CycleError(GraphError):
    """The declared target graph contains a dependency cycle."""


class CommandError(BuildError):
    """A target command exited with a non-zero status."""


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    """Outcome of one target during a build."""

    HIT = "HIT"          # cache entry reused, command not executed
    MISS = "MISS"        # something changed, command executed
    FORCED = "FORCED"    # executed because the user passed --force
    SKIPPED = "SKIPPED"  # phony target: declared but nothing to execute

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


# --------------------------------------------------------------------------- #
# Target
# --------------------------------------------------------------------------- #
Command = Union[str, Sequence[str]]


@dataclass
class Target:
    """One node of the build graph.

    Parameters
    ----------
    name:
        Unique target name.
    command:
        Either a string (executed through the shell) or a list of arguments
        (executed directly, no shell).  ``None`` makes the target a "phony"
        grouping node that only aggregates dependencies.
    inputs:
        Files (or simple glob patterns) whose *content* should invalidate the
        cache entry.  Paths are resolved relative to the target ``cwd``.
    outputs:
        Files the command is expected to create.  They are hashed after the
        run and re-verified on every later build, so tampering with an output
        invalidates the cache entry as well.
    deps:
        Names of upstream targets.  The digest of their outputs is folded into
        this target's action hash, giving transitive invalidation for free.
    env:
        Extra environment variables.  Their values are part of the fingerprint.
    cwd:
        Sub-directory (relative to the project root) the command runs in.
    phony:
        Marks a target as a pure grouping node (no command/outputs required).
    description:
        Free-form text shown by ``buildcache graph``/``status``.
    """

    name: str
    command: Optional[Command] = None
    inputs: Sequence[str] = ()
    outputs: Sequence[str] = ()
    deps: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    phony: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise BuildError("every target needs a non-empty name")
        if self.command is not None and not isinstance(self.command, (str, list, tuple)):
            raise BuildError(
                f"target {self.name!r}: command must be a string or a list of arguments"
            )
        if isinstance(self.command, (list, tuple)) and not self.command:
            raise BuildError(f"target {self.name!r}: command list must not be empty")
        self.inputs = tuple(self.inputs)
        self.outputs = tuple(self.outputs)
        self.deps = tuple(dict.fromkeys(self.deps))
        self.env = {str(k): str(v) for k, v in dict(self.env).items()}

        if self.phony and self.outputs:
            raise BuildError(f"target {self.name!r}: a phony target cannot declare outputs")
        if self.command is None and self.outputs:
            raise BuildError(
                f"target {self.name!r}: declares outputs but has no command to produce them"
            )

    # -- derived helpers ---------------------------------------------------- #
    @property
    def shell(self) -> bool:
        """True when the command has to be run through the shell."""
        return isinstance(self.command, str)

    @property
    def command_key(self) -> str:
        """Stable, human-readable representation of the command for hashing."""
        if self.command is None:
            return "<phony>"
        if self.shell:
            return "shell:" + str(self.command)
        return "argv:" + "\x1f".join(str(part) for part in self.command)


# --------------------------------------------------------------------------- #
# Cache record
# --------------------------------------------------------------------------- #
@dataclass
class CacheRecord:
    """Everything we remember about the last successful run of one target."""

    name: str
    action_hash: str
    status: str
    command: str
    input_hashes: Dict[str, str]
    dep_hashes: Dict[str, str]
    output_hashes: Dict[str, str]
    output_digest: str
    env_fingerprint: str
    timestamp: float
    duration: float
    exit_code: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CacheRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in dict(data).items() if k in known})
