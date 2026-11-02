import pytest

from pool_protocol.jobspec import JobSpecError, load_job_spec, parse_duration_s, parse_quantity_mb

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
        (lambda s: s["spec"].update(image="python:3.12"), "pinned by digest"),
        (lambda s: s["spec"].update(command=["echo", "{{nope}}"]), "unknown placeholders"),
        (lambda s: s["spec"].update(shape="gang"), "not available yet"),
        (lambda s: s["spec"].update(shape="single"), "cannot have a matrix"),
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
