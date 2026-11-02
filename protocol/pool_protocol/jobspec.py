"""Job specification (plan §6.5): parsing, validation and array expansion.

Shared by the control plane (authoritative validation at submit time) and the
CLI (early feedback before upload).
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .messages import TaskCheckpoint, TaskInput, TaskNetwork, TaskResources, TaskSpec, Tier

MAX_TASKS_PER_JOB = 10_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
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


class ResourcesReq(_Strict):
    cpu: float = Field(gt=0, default=1)
    memory: str | int = "1Gi"
    disk: str | int = "1Gi"
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
            raise ValueError("name must match ^[a-z0-9][a-z0-9-]{0,62}$")
        return v


class Spec(_Strict):
    shape: Literal["single", "array", "service", "gang"] = "single"
    image: str
    command: list[str] = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    matrix: dict[str, list[str | int | float | bool]] | None = None
    resources: ResourcesReq = Field(default_factory=ResourcesReq)
    isolation: IsolationReq = Field(default_factory=IsolationReq)
    network: NetworkReq = Field(default_factory=NetworkReq)
    data: DataReq = Field(default_factory=DataReq)
    checkpoint: CheckpointReq | None = None
    retries: int = Field(ge=0, le=20, default=3)
    max_runtime: str | int = "1h"
    data_class: Literal["public", "internal", "confidential"] = "internal"
    secrets: list[str] = Field(default_factory=list)
    os: Literal["linux", "windows"] | None = None

    @field_validator("image")
    @classmethod
    def _image_pinned(cls, v: str) -> str:
        if not DIGEST_RE.match(v):
            raise ValueError("image must be pinned by digest: <repo>@sha256:<64 hex>")
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
        if self.shape in ("service", "gang"):
            raise ValueError(f"shape {self.shape!r} is not available yet (plan phases 3 and 4)")
        if self.shape == "array" and not self.matrix:
            raise ValueError("shape 'array' needs a non-empty matrix")
        if self.shape == "single" and self.matrix:
            raise ValueError("shape 'single' cannot have a matrix; use shape: array")
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
                if not values:
                    raise ValueError(f"matrix key {key!r} has no values")
            count = 1
            for values in self.matrix.values():
                count *= len(values)
            if count > MAX_TASKS_PER_JOB:
                raise ValueError(f"matrix expands to {count} tasks; the limit is {MAX_TASKS_PER_JOB}")
        used = set()
        for text in [*self.command, *self.env.values()]:
            used.update(PLACEHOLDER_RE.findall(text))
        unknown = used - set(self.matrix or {})
        if unknown:
            raise ValueError(f"unknown placeholders: {sorted(unknown)}")
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
        """Cartesian product of the matrix, in a stable order. [{}] for single."""
        if not self.spec.matrix:
            return [{}]
        keys = list(self.spec.matrix)
        return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*self.spec.matrix.values())]

    def resources(self) -> TaskResources:
        r = self.spec.resources
        gpu = r.gpu or GPUReq()
        return TaskResources(
            cpu=r.cpu,
            memory_mb=parse_quantity_mb(r.memory),
            disk_mb=parse_quantity_mb(r.disk),
            gpus=gpu.count,
            gpu_vendor=gpu.vendor if gpu.count else None,
            gpu_memory_mb=parse_quantity_mb(gpu.min_memory) if (gpu.count and gpu.min_memory) else 0,
        )

    def render_task(self, params: dict[str, Any]) -> TaskSpec:
        def sub(text: str) -> str:
            return PLACEHOLDER_RE.sub(lambda m: str(params[m.group(1)]), text)

        s = self.spec
        return TaskSpec(
            image=s.image,
            command=[sub(c) for c in s.command],
            env={k: sub(v) for k, v in s.env.items()},
            resources=self.resources(),
            min_tier=s.isolation.min_tier,
            network=TaskNetwork(egress=s.network.egress, allow=s.network.allow),
            data_class=s.data_class,
            max_runtime_s=parse_duration_s(s.max_runtime),
            outputs_path=s.data.outputs.path,
            checkpoint=(
                TaskCheckpoint(path=s.checkpoint.path, every_s=parse_duration_s(s.checkpoint.every))
                if s.checkpoint
                else None
            ),
            inputs=[TaskInput(ref=i.ref, mount=i.mount) for i in s.data.inputs],
        )


def load_job_spec(data: dict[str, Any]) -> JobSpec:
    try:
        spec = JobSpec.model_validate(data)
        # Force evaluation of derived values so bad quantities fail at submit time.
        spec.resources()
        parse_duration_s(spec.spec.max_runtime)
        if spec.spec.checkpoint:
            parse_duration_s(spec.spec.checkpoint.every)
    except JobSpecError:
        raise
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        raise JobSpecError(str(exc)) from exc
    return spec
