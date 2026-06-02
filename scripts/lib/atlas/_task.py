"""Typed task I/O — kills the stringly-typed boundary at both ends.

A Task's contract used to be: a dict of UPPER_SNAKE→str env vars in, and a
`KEY=value` line scraped off stdout out. Both ends did ad-hoc string handling
(`os.environ.get` + a `require()` guard on the remote; `_parse_size_bytes`
grepping the trace on the controller). This module replaces that with:

- `TaskInputs`: a frozen dataclass per task. Each field becomes a `--kebab-case`
  CLI flag, typed from the annotation, required unless it declares a default.
  `from_args()` parses argv ONCE into the typed object and gives `--help` for
  free — so every task is already a CLI subcommand, the shape a future `atlas`
  CLI composes directly. After construction the task touches typed fields only.
- `TaskResult`: a frozen dataclass per task. `emit()` prints exactly one
  `ATLAS_RESULT=<json>` line; `parse(stdout)` on the controller recovers the
  typed object. No more grepping `bash -x` trace for a bespoke KEY=value.

The marker line is distinct from any command the script runs, so trace noise
never collides with it. Everything else on stdout/stderr stays human-readable.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import typing
from dataclasses import dataclass

RESULT_MARKER = "ATLAS_RESULT="

T = typing.TypeVar("T", bound="TaskInputs")
R = typing.TypeVar("R", bound="TaskResult")


@dataclass(frozen=True)
class TaskInputs:
    """Base for a task's typed inputs. Subclass with annotated fields; each field
    maps to a `--kebab-case` flag (snapshot_rootfs_path → --snapshot-rootfs-path).
    Fields with a default are optional; everything else is a required argument.

    Subclasses may set `command` (the CLI subcommand name) and a `__doc__`; both
    feed argparse so `--help` is meaningful and a future `atlas` CLI can mount
    each task as a subparser with no extra wiring."""

    #: The CLI subcommand name for this task. Defaults to the script stem when a
    #: task wires its own parser; harmless to leave unset for the flat form.
    command: typing.ClassVar[str] = ""

    @classmethod
    def build_parser(
        cls, parser: argparse.ArgumentParser | None = None
    ) -> argparse.ArgumentParser:
        """Construct (or populate) an ArgumentParser from the dataclass fields.
        Reused both standalone (`from_args`) and as a subparser when an `atlas`
        CLI mounts many tasks under one program."""
        parser = parser or argparse.ArgumentParser(
            prog=cls.command or None, description=cls.__doc__
        )
        for field in dataclasses.fields(cls):
            flag = "--" + field.name.replace("_", "-")
            required = not _has_default(field)
            if _is_list(field):
                # A list field becomes a REPEATABLE flag: --cgroup-arg X
                # --cgroup-arg Y collects ["X", "Y"]. This is what lets provision
                # pass the jailer's cgroup/resource args as a clean argv instead
                # of the newline-joined string the shell needed to dodge systemd
                # word-splitting — the hack disappears with a real arg vector.
                parser.add_argument(
                    flag, dest=field.name, action="append", default=None,
                    required=required, help=_field_help(field),
                )
            else:
                parser.add_argument(
                    flag, dest=field.name, type=_arg_type(field),
                    required=required,
                    default=None if required else _default(field),
                    help=_field_help(field),
                )
        return parser

    @classmethod
    def from_args(cls: type[T], argv: typing.Sequence[str] | None = None) -> T:
        """Parse argv into the typed object. On a missing/!int argument argparse
        prints usage to stderr and exits 2 — the CLI form of `${VAR:?required}`,
        naming the flag and (for ints) the expected type."""
        namespace = cls.build_parser().parse_args(argv)
        values = {}
        for field in dataclasses.fields(cls):
            value = getattr(namespace, field.name)
            # An optional list flag that never appeared comes back None; normalize
            # to the declared default (usually []) so the body always sees a list.
            if _is_list(field) and value is None:
                value = _default(field) if _has_default(field) else []
            values[field.name] = value
        return cls(**values)


@dataclass(frozen=True)
class TaskResult:
    """Base for a task's typed result. Subclass with annotated fields. `emit()`
    writes the one machine-readable line; `parse()` recovers it controller-side."""

    def emit(self) -> None:
        print(RESULT_MARKER + json.dumps(dataclasses.asdict(self)))

    @classmethod
    def parse(cls: type[R], stdout: str) -> R:
        """Recover the typed result from a Task's stdout. Raises if the marker is
        absent — unlike the old `_parse_size_bytes`, which silently returned 0 on
        a truncated run. A task that declares a result must produce one."""
        for line in reversed((stdout or "").splitlines()):
            if line.startswith(RESULT_MARKER):
                payload = json.loads(line[len(RESULT_MARKER):])
                return cls(**payload)
        raise ValueError(f"no {RESULT_MARKER} line in task output")


def _has_default(field: dataclasses.Field) -> bool:
    return (
        field.default is not dataclasses.MISSING
        or field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
    )


def _default(field: dataclasses.Field) -> typing.Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    return field.default_factory()  # type: ignore[misc]


def _is_list(field: dataclasses.Field) -> bool:
    """True for a field annotated as a list (list[str] / 'list[str]' / list).
    String annotations are compared textually since `from __future__ import
    annotations` makes every annotation a string at runtime."""
    annotation = field.type
    if isinstance(annotation, str):
        return annotation.startswith("list")
    return annotation is list or typing.get_origin(annotation) is list


def _arg_type(field: dataclasses.Field) -> typing.Callable[[str], typing.Any]:
    """argparse `type=` callable from the field annotation. `int` fields parse as
    int (argparse reports a clean 'invalid int value' on bad input); everything
    else stays a string."""
    if field.type in (int, "int"):
        return int
    return str


def _field_help(field: dataclasses.Field) -> str:
    """The trailing `#:` comment convention isn't introspectable, so help text
    is empty unless a subclass supplies metadata — kept as a hook for when the
    CLI wants rich `--help`."""
    return field.metadata.get("help", "")
