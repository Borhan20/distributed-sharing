from pathlib import Path

import pytest
import yaml

from pool_protocol.jobspec import (
    JobSpecError,
    default_name,
    is_pinned,
    load_job_spec,
    parse_duration_s,
    parse_quantity_mb,
    parse_values,
)

IMG = "registry.example.org/research/probe@sha256:" + "b" * 64


def plan_example():
    # The array example from plan §6.5 (with a valid digest).
    return {
        "apiVersion": "pool/v1alpha1",
        "kind": "Job",
        "metadata": {"name": "probe-seed-sweep", "group": "research-team"},
        "spec": {
            "shape": "array",
            "image": IMG,
            "command": ["python", "train_probe.py", "--seed", "{{seed}}", "--dataset", "{{dataset}}"],
            "matrix": {"seed": list(range(10)), "dataset": ["imdb", "amazon_polarity", "ag_news"]},
            "resources": {"cpu": 4, "memory": "16Gi", "gpu": {"count": 1, "vendor": "nvidia", "min_memory": "8Gi"}},
            "isolation": {"min_tier": "T1"},
            "network": {"egress": "none"},
            "data": {"outputs": {"path": "/out"}},
            "checkpoint": {"path": "/ckpt", "every": "10m"},
            "retries": 3,
            "max_runtime": "2h",
            "data_class": "internal",
            "secrets": [],
        },
    }


def test_plan_example_expands_to_30_tasks():
    spec = load_job_spec(plan_example())
    params = spec.task_params()
    assert len(params) == 30
    assert params[0] == {"seed": 0, "dataset": "imdb"}
    t = spec.render_task(params[4])
    assert t.command == ["python", "train_probe.py", "--seed", "1", "--dataset", "amazon_polarity"]
    assert t.resources.memory_mb == 16384 and t.resources.gpu_memory_mb == 8192
    assert t.max_runtime_s == 7200 and t.checkpoint.every_s == 600


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda s: s["spec"].update(image="python 3.12"), "image name like"),
        (lambda s: s["spec"].update(image="python@sha256:short"), "image name like"),
        (lambda s: s["spec"].update(command=["echo", "{{nope}}"]), "unknown placeholders"),
        (lambda s: s["spec"].update(shape="gang"), "not available yet"),
        (lambda s: s["spec"].update(shape="single"), "runs one task"),
        (lambda s: s["spec"].update(secrets=["db-password"]), "secrets are not available"),
        (lambda s: s["spec"]["data"].update(inputs=[{"ref": "sha256:" + "c" * 64, "mount": "/data"}]),
         "inputs are not available"),
        (lambda s: s["spec"].update(env={"POOL_X": "1"}), "reserved env"),
        (lambda s: s["spec"]["resources"].update(memory="lots"), "invalid quantity"),
        (lambda s: s["spec"].update(max_runtime="forever"), "invalid duration"),
        (lambda s: s["spec"]["data"]["outputs"].update(path="../etc"), "absolute"),
        (lambda s: s["spec"].update(unexpected=True), "Extra inputs"),
        (lambda s: s["spec"].update(matrix={"a": list(range(200)), "b": list(range(100))}), "limit"),
    ],
)
def test_rejections(mutate, needle):
    data = plan_example()
    mutate(data)
    with pytest.raises(JobSpecError) as exc:
        load_job_spec(data)
    assert needle in str(exc.value)


def test_quantities_and_durations():
    assert parse_quantity_mb("512Mi") == 512
    assert parse_quantity_mb("1Gi") == 1024
    assert parse_quantity_mb("1G") == 954
    assert parse_duration_s("1h30m") == 5400
    assert parse_duration_s("45") == 45
    with pytest.raises(JobSpecError):
        parse_duration_s("10x")


# ---- the short form, parts, built-in placeholders (decisions of 3 November 2026) ----------------


def test_short_form_is_the_full_form():
    js = load_job_spec({
        "name": "pi",
        "image": "ghcr.io/lab/pi-sweep:1",
        "command": "python /app/pi.py --seed {{seed}} --samples {{samples}}",
        "sweep": {"seed": "0..4", "samples": [1000000, 10000000]},
        "cpu": 1, "memory": "512Mi", "disk": "256Mi", "gpu": 1,
        "time_limit": "30m", "retries": 2, "isolation": "T2",
    })
    assert js.metadata.name == "pi" and js.spec.shape == "array"
    assert js.spec.image == "ghcr.io/lab/pi-sweep:1" and not is_pinned(js.spec.image)
    params = js.task_params()
    assert len(params) == 10 and params[0] == {"seed": 0, "samples": 1000000}
    t = js.render_task(params[3], index=3, count=10)
    assert t.command == ["python", "/app/pi.py", "--seed", "1", "--samples", "10000000"]
    assert (t.task_index, t.task_count) == (3, 10)
    r = js.resources()
    assert (r.cpu, r.memory_mb, r.disk_mb, r.gpus) == (1, 512, 256, 1)
    assert t.max_runtime_s == 1800 and js.spec.retries == 2 and js.spec.isolation.min_tier == "T2"


def test_parts_give_each_copy_its_number():
    js = load_job_spec({"image": "borhan-lab/sim:v1", "parts": 4, "command": "sim --part {{index}} --of {{count}}"})
    assert js.metadata.name == "sim" and js.spec.shape == "array"
    tasks = [js.render_task(p, i, 4) for i, p in enumerate(js.task_params())]
    assert [t.command for t in tasks] == [["sim", "--part", str(i), "--of", "4"] for i in range(4)]
    assert [t.task_index for t in tasks] == [0, 1, 2, 3] and {t.task_count for t in tasks} == {4}


def test_auto_parts_wait_for_the_server():
    js = load_job_spec({"image": "lab/app:1", "parts": "auto"})
    assert js.spec.parts == "auto"
    with pytest.raises(JobSpecError, match="decided by the server"):
        js.task_params()
    js.spec.parts = 6
    assert len(js.task_params()) == 6


def test_command_is_optional_and_may_be_one_string():
    assert load_job_spec({"image": "lab/app:1"}).render_task({}).command == []
    js = load_job_spec({"image": "lab/app:1", "command": "sh -c 'echo hi > /out/a.txt'"})
    assert js.spec.command == ["sh", "-c", "echo hi > /out/a.txt"]


def test_single_task_has_index_zero_of_one():
    t = load_job_spec({"image": "lab/app:1", "command": "run {{index}}/{{count}}"}).render_task({})
    assert t.command == ["run", "0/1"] and (t.task_index, t.task_count) == (0, 1)


@pytest.mark.parametrize(
    "data, needle",
    [
        ({"image": "lab/app:1", "colour": "blue"}, "unknown field(s) colour"),
        ({"command": "run"}, "image"),
        ({"image": "lab/app:1", "parts": 2, "sweep": {"a": [1]}}, "either parts"),
        ({"image": "lab/app:1", "parts": 0}, "parts must be between"),
        ({"image": "lab/app:1", "parts": "many"}, "parts"),
        ({"image": "lab/app:1", "sweep": {"index": [1, 2]}}, "built-in placeholder"),
        ({"image": "lab/app:1", "command": "run {{seed}}"}, "{{seed}}"),
        ({"image": "lab/app:1", "command": "run 'x"}, "check the quotes"),
        ({"image": "lab/app:1", "cpu": 0}, "cpu:"),
        ({"image": "lab/app:1", "sweep": {"seed": "5..1"}}, "empty"),
        ({"image": "lab/app:1", "name": "Bad Name"}, "name: use lowercase"),
    ],
)
def test_short_form_rejections_name_the_field(data, needle):
    with pytest.raises(JobSpecError) as exc:
        load_job_spec(data)
    assert needle in str(exc.value)
    assert "pydantic" not in str(exc.value), "errors are plain sentences, not pydantic's report"


def test_values_and_names():
    assert parse_values("0..3") == [0, 1, 2, 3]
    assert parse_values("small, large") == ["small", "large"]
    assert parse_values("1,2.5,x") == [1, 2.5, "x"]
    assert parse_values(7) == [7] and parse_values([1, "a"]) == [1, "a"]
    assert default_name("ghcr.io/lab/Pi_Sweep:1@sha256:" + "a" * 64) == "pi-sweep"
    assert default_name("busybox") == "busybox"
    assert is_pinned("ghcr.io/lab/app:1@sha256:" + "a" * 64) and not is_pinned("ghcr.io/lab/app:1")


def test_full_form_without_shape_infers_it():
    data = plan_example()
    del data["spec"]["shape"]
    assert load_job_spec(data).spec.shape == "array"
    data["spec"].pop("matrix")
    data["spec"]["command"] = ["python", "train_probe.py"]
    assert load_job_spec(data).spec.shape == "single"


@pytest.mark.parametrize("path", sorted((Path(__file__).resolve().parents[2] / "examples").rglob("*.yaml")),
                         ids=lambda p: p.parent.name + "/" + p.name)
def test_every_example_job_file_is_valid(path):
    """The files in examples/ are the ones docs/user-guide.md walks through."""
    js = load_job_spec(yaml.safe_load(path.read_text()))
    assert js.spec.image and js.metadata.name
