"""Job specification (plan §6.5): parsing, validation and array expansion.

Shared by the control plane (authoritative validation at submit time) and the
CLI (early feedback before upload).

Two ways to write a job, both accepted everywhere:

- the full form (apiVersion, kind, metadata, spec), and
- the short form, one flat mapping: name, image, command, parts or sweep, cpu, memory, ...
  (`SHORT_KEYS`). It is turned into the full form by `expand_short_form`.
"""

from __future__ import annotations

import itertools
import re
import shlex
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .messages import TaskCheckpoint, TaskInput, TaskNetwork, TaskResources, TaskSpec, Tier

MAX_TASKS_PER_JOB = 10_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
# registry/path/name, an optional :tag, an optional @sha256 digest. The server parses it again (registry.py).
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*(@sha256:[0-9a-f]{64})?$")
BUILTIN_PLACEHOLDERS = {"index", "count"}  # {{index}}: 0..count-1, the task's number; {{count}}: tasks in the job
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_QUANTITY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T)?\s*$")
_QUANTITY_FACTOR_MB = {
    None: 1 / (1024 * 1024),  # plain bytes
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K": 1000 / (1024 * 1024),
    "M": 1000**2 / (1024 * 1024),
    "G": 1000**3 / (1024 * 1024),
    "T": 1000**4 / (1024 * 1024),
}
_DURATION_RE = re.compile(r"(\d+)\s*(d|h|m|s)")
_DURATION_FACTOR = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class JobSpecError(ValueError):
    pass


def is_pinned(image: str) -> bool:
    """Whether the image names one exact version (a digest) rather than a tag that can move."""
    return bool(DIGEST_RE.match(image))


def parse_values(value: Any) -> Any:
    """Sweep values: a list stays a list; '0..9' -> [0, ..., 9]; 'a,b' -> ['a', 'b']; one value -> [value].

    Numbers in strings become ints or floats, as on the command line (`pool sweep --set seed=0..9`).
    Anything else is returned unchanged for the model to reject.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, bool | int | float):
        return [value]
    if not isinstance(value, str):
        return value
    text = value.strip()
    m = re.fullmatch(r"(-?\d+)\s*\.\.\s*(-?\d+)", text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < lo:
            raise JobSpecError(f"range {text!r} is empty: write it low..high")
        if hi - lo >= MAX_TASKS_PER_JOB:
            raise JobSpecError(f"range {text!r} has more than {MAX_TASKS_PER_JOB} values")
        return list(range(lo, hi + 1))
    out: list[Any] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        for kind in (int, float):
            try:
                out.append(kind(part))
                break
            except ValueError:
                pass
        else:
            out.append(part)
    return out


def parse_quantity_mb(value: str | int | float) -> int:
    """'16Gi' -> 16384 (MiB). Integers are taken as MiB already."""
    if isinstance(value, int | float):
        return int(value)
    m = _QUANTITY_RE.match(value)
    if not m:
        raise JobSpecError(f"invalid quantity {value!r} (use e.g. 512Mi, 16Gi)")
    number, unit = float(m.group(1)), m.group(2)
    return max(1, int(round(number * _QUANTITY_FACTOR_MB[unit])))


def parse_duration_s(value: str | int) -> int:
    """'2h', '10m', '1h30m', or an integer number of seconds."""
    if isinstance(value, int):
        return value
    text = value.strip()
    if text.isdigit():
        return int(text)
    parts = _DURATION_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        raise JobSpecError(f"invalid duration {value!r} (use e.g. 90s, 10m, 2h)")
    return sum(int(n) * _DURATION_FACTOR[u] for n, u in parts)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GPUReq(_Strict):
    count: int = Field(ge=0, default=0)
    vendor: Literal["nvidia"] | None = None
    min_memory: str | int | None = None


# Sizes used when a size is left out and nobody decides it (e.g. a job file checked on its own). On the
# server, a size left out is chosen by pool from the person's earlier runs (docs/design/pool-decides.md §4.3).
DEFAULT_CPU = 1.0
DEFAULT_MEMORY = "1Gi"
DEFAULT_DISK = "1Gi"
DEFAULT_MAX_RUNTIME = "1h"
# The largest sizes a job may ask for: far above any computer, inside the database's 32-bit columns.
MAX_CPU_CORES = 4096.0
MAX_SIZE_MB = 1 << 30  # 1024 TiB, in MiB
MAX_RUNTIME_S = 365 * 24 * 3600


class ResourcesReq(_Strict):
    # None: not given, so pool decides. A given size is kept as it is.
    cpu: float | None = Field(gt=0, default=None)
    memory: str | int | None = None
    disk: str | int | None = None
    gpu: GPUReq | None = None


class IsolationReq(_Strict):
    min_tier: Tier = "T1"


class NetworkReq(_Strict):
    egress: Literal["none", "allowlist"] = "none"
    allow: list[str] = Field(default_factory=list)


class InputReq(_Strict):
    ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mount: str


class OutputsReq(_Strict):
    path: str = "/out"


class DataReq(_Strict):
    inputs: list[InputReq] = Field(default_factory=list)
    outputs: OutputsReq = Field(default_factory=OutputsReq)


class CheckpointReq(_Strict):
    path: str = "/ckpt"
    every: str | int = "10m"


class Metadata(_Strict):
    name: str
    group: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("use lowercase letters, digits and dashes, at most 63 characters, e.g. pi-sweep")
        return v


class Spec(_Strict):
    # Inferred when left out: "array" with a matrix or parts, otherwise "single".
    shape: Literal["single", "array", "service", "gang"] | None = None
    # A tag (ghcr.io/lab/app:1) or a digest (...@sha256:...). The server pins tags to a digest at submit time,
    # so every task runs the same image and computers only ever see digests.
    image: str
    # A list, or one string split like a shell would (no pipes or &&: use ["sh", "-c", "..."]). Empty: the
    # image's own default command.
    command: list[str] | str = Field(default_factory=list)
    # Arguments: after `command`, or for the image's own ENTRYPOINT when there is no command (`docker run IMAGE
    # ARGS`). A string is split like `command`.
    args: list[str] | str = Field(default_factory=list)
    workdir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    matrix: dict[str, list[str | int | float | bool] | str | int | float | bool] | None = None
    # Split the work into this many copies of the same task; each copy gets {{index}} and POOL_TASK_INDEX.
    # "auto": the server picks as many as the group's computers can run at once.
    parts: int | Literal["auto"] | None = None
    resources: ResourcesReq = Field(default_factory=ResourcesReq)
    isolation: IsolationReq = Field(default_factory=IsolationReq)
    network: NetworkReq = Field(default_factory=NetworkReq)
    data: DataReq = Field(default_factory=DataReq)
    checkpoint: CheckpointReq | None = None
    retries: int = Field(ge=0, le=20, default=3)
    max_runtime: str | int | None = None  # None: not given, so pool decides
    data_class: Literal["public", "internal", "confidential"] = "internal"
    secrets: list[str] = Field(default_factory=list)
    os: Literal["linux", "windows"] | None = None

    @field_validator("image")
    @classmethod
    def _image_reference(cls, v: str) -> str:
        v = v.strip()
        if not IMAGE_RE.match(v):
            raise ValueError("must be an image name like ghcr.io/lab/app:1 or busybox@sha256:<64 hex>")
        return v

    @field_validator("command", "args", mode="before")
    @classmethod
    def _command_words(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return shlex.split(v)
            except ValueError as exc:
                raise ValueError(f"{exc} (check the quotes)") from None
        return v

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, v: str | None) -> str | None:
        if v is not None and (not v.startswith("/") or "\x00" in v or ".." in v.split("/")):
            raise ValueError("must be an absolute path inside the container, e.g. /app")
        return v

    @field_validator("matrix", mode="before")
    @classmethod
    def _matrix_values(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: parse_values(x) for k, x in v.items()}
        return v

    @field_validator("parts")
    @classmethod
    def _parts_range(cls, v: int | str | None) -> int | str | None:
        if isinstance(v, int) and not 1 <= v <= MAX_TASKS_PER_JOB:
            raise ValueError(f"parts must be between 1 and {MAX_TASKS_PER_JOB}, or auto")
        return v

    @field_validator("env")
    @classmethod
    def _env_names(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not ENV_NAME_RE.match(k) or k.startswith("POOL_"):
                raise ValueError(f"invalid or reserved env name {k!r}")
        return v

    @model_validator(mode="after")
    def _shape_rules(self) -> Spec:
        if self.shape is None:
            self.shape = "array" if (self.matrix or self.parts) else "single"
        if self.shape in ("service", "gang"):
            raise ValueError(f"shape {self.shape!r} is not available yet (plan phases 3 and 4)")
        if self.matrix and self.parts:
            raise ValueError("use either parts (copies of one task) or a sweep/matrix (one task per value), not both")
        if self.shape == "array" and not (self.matrix or self.parts):
            raise ValueError("shape 'array' needs a non-empty matrix, or parts")
        if self.shape == "single" and (self.matrix or self.parts):
            raise ValueError("shape 'single' runs one task: remove the matrix or parts, or use shape: array")
        if self.secrets:
            raise ValueError("secrets are not available yet (plan phase 3)")
        if self.data.inputs:
            # Rejected rather than ignored: a job must never run without the data it asked for.
            raise ValueError("data.inputs are not available yet (input delivery from the object store is follow-up "
                             "work); bake small inputs into the image for now")
        if self.matrix:
            for key, values in self.matrix.items():
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    raise ValueError(f"invalid matrix key {key!r}")
                if key in BUILTIN_PLACEHOLDERS:
                    raise ValueError(f"{key!r} is a built-in placeholder ({{{{{key}}}}}); give the values another name")
                if not values:
                    raise ValueError(f"matrix key {key!r} has no values")
            count = 1
            for values in self.matrix.values():
                count *= len(values)
            if count > MAX_TASKS_PER_JOB:
                raise ValueError(f"matrix expands to {count} tasks; the limit is {MAX_TASKS_PER_JOB}")
        used = set()
        for text in [*self.command, *self.args, *self.env.values()]:
            used.update(PLACEHOLDER_RE.findall(text))
        unknown = used - set(self.matrix or {}) - BUILTIN_PLACEHOLDERS
        if unknown:
            names = ", ".join("{{" + u + "}}" for u in sorted(unknown))
            raise ValueError(f"unknown placeholders: {names} (built in: {{{{index}}}}, {{{{count}}}}; others need "
                             "values in the sweep/matrix)")
        for path in [self.data.outputs.path] + [i.mount for i in self.data.inputs]:
            if not path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"mount path must be absolute without '..': {path!r}")
        return self


class JobSpec(_Strict):
    apiVersion: Literal["pool/v1alpha1"] = "pool/v1alpha1"  # noqa: N815 (wire name)
    kind: Literal["Job"] = "Job"
    metadata: Metadata
    spec: Spec

    # ---- derived values ---------------------------------------------------

    def task_params(self) -> list[dict[str, Any]]:
        """One dict of values per task, in a stable order: the matrix's Cartesian product, `parts` empty
        dicts, or [{}] for single. `parts: auto` must be replaced by a number first (the server does)."""
        if self.spec.parts == "auto":
            raise JobSpecError("parts: auto is decided by the server when the job is submitted")
        if self.spec.parts:
            return [{} for _ in range(self.spec.parts)]
        if not self.spec.matrix:
            return [{}]
        keys = list(self.spec.matrix)
        return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*self.spec.matrix.values())]

    def given_sizes(self) -> dict[str, bool]:
        """Which sizes the job file sets. The others are pool's to decide."""
        r = self.spec.resources
        return {"cpu": r.cpu is not None, "memory": r.memory is not None, "disk": r.disk is not None,
                "time_limit": self.spec.max_runtime is not None}

    def resources(self) -> TaskResources:
        r = self.spec.resources
        gpu = r.gpu or GPUReq()
        return TaskResources(
            cpu=r.cpu if r.cpu is not None else DEFAULT_CPU,
            memory_mb=parse_quantity_mb(r.memory if r.memory is not None else DEFAULT_MEMORY),
            disk_mb=parse_quantity_mb(r.disk if r.disk is not None else DEFAULT_DISK),
            gpus=gpu.count,
            gpu_vendor=gpu.vendor if gpu.count else None,
            gpu_memory_mb=parse_quantity_mb(gpu.min_memory) if (gpu.count and gpu.min_memory) else 0,
        )

    def render_task(self, params: dict[str, Any], index: int = 0, count: int = 1) -> TaskSpec:
        values = {**params, "index": index, "count": count}

        def sub(text: str) -> str:
            return PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), text)

        s = self.spec
        return TaskSpec(
            image=s.image,
            command=[sub(c) for c in s.command],
            args=[sub(a) for a in s.args],
            workdir=s.workdir,
            env={k: sub(v) for k, v in s.env.items()},
            task_index=index,
            task_count=count,
            resources=self.resources(),
            min_tier=s.isolation.min_tier,
            network=TaskNetwork(egress=s.network.egress, allow=s.network.allow),
            data_class=s.data_class,
            max_runtime_s=parse_duration_s(s.max_runtime if s.max_runtime is not None else DEFAULT_MAX_RUNTIME),
            outputs_path=s.data.outputs.path,
            checkpoint=(
                TaskCheckpoint(path=s.checkpoint.path, every_s=parse_duration_s(s.checkpoint.every))
                if s.checkpoint
                else None
            ),
            inputs=[TaskInput(ref=i.ref, mount=i.mount) for i in s.data.inputs],
        )


def result_folder(index: int, count: int, params: dict[str, Any]) -> str:
    """Folder for one task's results, the same in the web download and `pool download --all`:
    task-003, or task-003_seed=3_size=large for a sweep. Sorted by name, folders keep task order."""
    width = max(3, len(str(max(count - 1, 0))))
    name = f"task-{index:0{width}d}"
    tail = "_".join(f"{k}={v}" for k, v in params.items())
    tail = re.sub(r"[^A-Za-z0-9._=+-]+", "-", tail)[:80].strip("-.")
    return f"{name}_{tail}" if tail else name


# ---- the short form ------------------------------------------------------------

SHORT_KEYS = {
    "name": "a short name (default: from the image)",
    "group": "the group to submit to",
    "image": "container image, e.g. ghcr.io/lab/app:1",
    "command": "what to run (default: the image's own command)",
    "args": "arguments for the image's own command, like `docker run IMAGE ARGS`",
    "workdir": "working directory inside the container",
    "env": "environment variables",
    "parts": "split into this many copies, or auto",
    "sweep": "one task per combination of values",
    "cpu": "cores per task",
    "memory": "memory per task, e.g. 512Mi",
    "disk": "disk per task, e.g. 1Gi",
    "gpu": "GPUs per task",
    "time_limit": "time limit per task, e.g. 30m",
    "retries": "retries after a failure",
    "isolation": "minimum isolation tier, e.g. T2",
    "data_class": "public, internal or confidential",
    "os": "linux or windows",
}
_SHORT_ALIASES = {"max_runtime": "time_limit", "matrix": "sweep"}


def is_short_form(data: dict[str, Any]) -> bool:
    return "spec" not in data and "metadata" not in data


def default_name(image: str) -> str:
    """'ghcr.io/lab/pi-sweep:1@sha256:...' -> 'pi-sweep'."""
    last = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0].lower()
    name = re.sub(r"[^a-z0-9-]+", "-", last).strip("-")[:63].strip("-")
    return name or "job"


def expand_short_form(data: dict[str, Any]) -> dict[str, Any]:
    """The short form as the full form. Unknown keys are an error, so typos do not pass silently."""
    data = {_SHORT_ALIASES.get(k, k): v for k, v in data.items()}
    unknown = sorted(set(data) - set(SHORT_KEYS) - {"apiVersion", "kind"})
    if unknown:
        raise JobSpecError(f"unknown field(s) {', '.join(unknown)}; a short job file may use: {', '.join(SHORT_KEYS)}")
    image = data.get("image")
    if not isinstance(image, str):
        raise JobSpecError("image is required, e.g. image: ghcr.io/lab/app:1")
    spec: dict[str, Any] = {"image": image}
    for key in ("command", "args", "workdir", "env", "parts", "retries", "data_class", "os"):
        if key in data:
            spec[key] = data[key]
    if "sweep" in data:
        spec["matrix"] = data["sweep"]
    if "time_limit" in data:
        spec["max_runtime"] = data["time_limit"]
    if "isolation" in data:
        spec["isolation"] = {"min_tier": data["isolation"]} if isinstance(data["isolation"], str) else data["isolation"]
    resources = {k: data[k] for k in ("cpu", "memory", "disk") if k in data}
    if "gpu" in data:
        resources["gpu"] = {"count": data["gpu"]} if isinstance(data["gpu"], int) else data["gpu"]
    if resources:
        spec["resources"] = resources
    metadata = {"name": data.get("name") or default_name(image)}
    if data.get("group"):
        metadata["group"] = data["group"]
    return {"apiVersion": data.get("apiVersion", "pool/v1alpha1"), "kind": data.get("kind", "Job"),
            "metadata": metadata, "spec": spec}


_SHORT_FIELD_NAMES = {"spec.matrix": "sweep", "spec.max_runtime": "time_limit", "spec.resources.cpu": "cpu",
                      "spec.resources.memory": "memory", "spec.resources.disk": "disk", "spec.resources.gpu": "gpu",
                      "spec.isolation.min_tier": "isolation", "metadata.name": "name"}


def _readable(exc: ValidationError, short: bool) -> str:
    """Pydantic's report as one line per problem: 'field: what is wrong'."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"] if not str(x).startswith(("list[", "int", "str", "float", "bool",
                                                                              "literal[")))
        if short:
            loc = next((v + loc[len(k):] for k, v in _SHORT_FIELD_NAMES.items() if loc.startswith(k)),
                       "" if loc == "spec" else loc.removeprefix("spec."))
        msg = err["msg"].removeprefix("Value error, ")
        line = f"{loc}: {msg}" if loc else msg
        if line not in lines:
            lines.append(line)
    return "; ".join(lines)


def load_job_spec(data: dict[str, Any]) -> JobSpec:
    """Validate a job in either form. Tags are allowed here; the server pins them before creating tasks."""
    if not isinstance(data, dict):
        raise JobSpecError("a job file is a mapping, e.g. image: ..., command: ...")
    short = is_short_form(data)
    try:
        if short:
            data = expand_short_form(data)
        spec = JobSpec.model_validate(data)
        # Force evaluation of derived values so bad quantities fail at submit time.
        res = spec.resources()
        if res.cpu > MAX_CPU_CORES:
            raise JobSpecError(f"cpu: at most {MAX_CPU_CORES:g} cores")
        for what, mb in (("memory", res.memory_mb), ("disk", res.disk_mb)):
            if mb > MAX_SIZE_MB:
                raise JobSpecError(f"{what}: at most {MAX_SIZE_MB >> 20} TiB")
        if spec.spec.max_runtime is not None:
            seconds = parse_duration_s(spec.spec.max_runtime)
            if not 1 <= seconds <= MAX_RUNTIME_S:
                raise JobSpecError("time_limit: between 1 second and 365 days, e.g. 30m or 2h")
        if spec.spec.checkpoint:
            parse_duration_s(spec.spec.checkpoint.every)
    except JobSpecError:
        raise
    except ValidationError as exc:
        raise JobSpecError(_readable(exc, short)) from None
    except ValueError as exc:
        raise JobSpecError(str(exc)) from exc
    return spec
