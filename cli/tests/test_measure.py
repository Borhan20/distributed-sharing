"""Measuring a run from `docker stats`, and saying what it used (docs/design/pool-decides.md §4.2)."""

from pool_cli import measure
from pool_cli.run import usage_lines


def test_parse_line_reads_docker_stats_frames():
    frame = '\x1b[H{"CPUPerc":"101.10%","MemUsage":"876KiB / 256MiB","Name":"x"}\x1b[K'
    assert measure.parse_line(frame) == (101.10, 876 * 1024)
    for bad in ("", "\x1b[K", "{nope}", '{"CPUPerc":"--","MemUsage":"-- / --"}',
                '{"CPUPerc":"-1%","MemUsage":"1B / 2B"}', '{"CPUPerc":"nan%","MemUsage":"1B / 2B"}',
                '{"MemUsage":"1B / 2B"}'):
        assert measure.parse_line(bad) is None, bad
    assert measure.parse_size("1.5GiB") == 1610612736 and measure.parse_size("12.3kB") == 12300
    assert measure.parse_size("lots") is None


def test_meter_extends_cpu_time_over_the_whole_run():
    m = measure.Meter("docker", "x")
    m.add(0, 10 << 20, 100.0)
    m.add(200, 300 << 20, 101.0)  # 2 cores for 1 s
    m.add(0, 100 << 20, 103.0)    # idle for 2 s
    m.add(100, 1, 1000.0)         # a gap: not counted
    m._stop.set()                 # no thread was started
    used = m.stop(30.0)
    assert used == {"measured": 1, "peak_memory_mb": 300, "avg_cpu": 0.67, "max_wall_seconds": 30.0}


def test_usage_lines():
    req = {"cpu": 2, "memory_mb": 8192, "time_limit_s": 7200}
    one = usage_lines(req, {"measured": 1, "peak_memory_mb": 600, "avg_cpu": 1.5, "max_wall_seconds": 754})
    assert one[0] == "It used: memory 600 MiB of 8.0 GiB, CPU 1.5 of 2 on average, run time 12 min 34 s of 2 h."
    assert "Asking for less (e.g. -m 1200m)" in one[1]
    many = usage_lines(req, {"measured": 3, "peak_memory_mb": 6000, "avg_cpu": 2, "max_wall_seconds": 60})
    assert many[0].startswith("Most used by one task: ") and len(many) == 1
    assert usage_lines(req, None) == [] and usage_lines(req, {"measured": 0, "oom_killed": 1}) == []
