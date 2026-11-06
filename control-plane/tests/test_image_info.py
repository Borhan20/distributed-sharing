"""Reading an image before it runs: platforms, CUDA, ports (docs/design/pool-decides.md §4.1, step C)."""

import hashlib
import json

import pytest
from fake_agent import CAPS, share
from sqlalchemy import select

from pool_cp import registry
from pool_cp.models import ImageInfoCache, Job
from pool_cp.registry import ImageInfo, RegistryError, image_info
from pool_protocol import messages as pm


def blob(obj) -> tuple[bytes, str]:
    data = json.dumps(obj).encode()
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


CONFIG, CONFIG_D = blob({"os": "linux", "architecture": "amd64",
                         "config": {"Env": ["PATH=/bin", "CUDA_VERSION=12.4.1", "SECRET=do-not-keep"],
                                    "ExposedPorts": {"8000/tcp": {}}, "Healthcheck": {"Test": ["NONE"]}}})
IMAGE, IMAGE_D = blob({"config": {"digest": CONFIG_D}, "layers": [{"size": 3 << 20}, {"size": 1}]})
INDEX, INDEX_D = blob({"manifests": [
    {"digest": IMAGE_D, "platform": {"os": "linux", "architecture": "amd64"}},
    {"digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
    {"digest": "sha256:" + "2" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},  # attestation
]})
STORE = {INDEX_D: INDEX, IMAGE_D: IMAGE, CONFIG_D: CONFIG}


def test_an_index_gives_every_platform_and_the_config_says_the_rest():
    info = image_info(STORE.__getitem__, STORE.__getitem__, INDEX_D)
    assert info.platforms == ["linux/amd64", "linux/arm64/v8"], "attestations are not platforms"
    assert info.cuda and info.ports == ["8000/tcp"] and info.healthcheck is False and info.size_mb == 4
    assert "do-not-keep" not in json.dumps(info.as_dict()), "values of environment variables are never kept"
    single = image_info(STORE.__getitem__, STORE.__getitem__, IMAGE_D)
    assert single.platforms == ["linux/amd64"]


def test_content_that_does_not_match_its_digest_is_refused():
    bad = {**STORE, IMAGE_D: IMAGE + b" "}
    with pytest.raises(RegistryError, match="does not match its digest"):
        image_info(bad.__getitem__, bad.__getitem__, INDEX_D)


def test_a_blob_redirect_is_followed_only_to_https_and_without_the_token(monkeypatch):
    ref = registry.ImageRef(registry="registry.example.org", repository="lab/app", api="https://registry.example.org",
                            auth_key="registry.example.org")
    calls = []

    def fetch(url, headers, timeout, allow_private):
        calls.append((url, dict(headers), allow_private))
        if "blobs" in url:
            return 307, {"Location": location}, b""
        return 200, {}, CONFIG

    monkeypatch.setattr(registry, "_fetch_once", fetch)
    location = "https://cdn.example.net/blob?sig=1"
    assert registry._blob(ref, CONFIG_D, "TOKEN", 5, True) == CONFIG
    assert calls[0][1] == {"Authorization": "Bearer TOKEN"}
    assert calls[1] == (location, {}, False), "no token to the CDN, and only to a public address"
    location = "http://cdn.example.net/blob"
    with pytest.raises(RegistryError, match="not https"):
        registry._blob(ref, CONFIG_D, "TOKEN", 5, True)


@pytest.fixture
def read_as(monkeypatch):
    def set_info(info: ImageInfo):
        monkeypatch.setattr("pool_cp.api.jobs.inspect_image", lambda image, cred, **kw: info)
    return set_info


def img(name: str = "app") -> str:
    """A new digest each time: what pool read is cached per digest, across tests too."""
    return f"registry.example.org/lab/{name}:1@sha256:" + __import__("secrets").token_hex(32)


def info(**kw) -> ImageInfo:
    return ImageInfo(**{"platforms": ["linux/amd64"], "cuda": False, "ports": [], "healthcheck": False,
                        "size_mb": 100, **kw})


@pytest.mark.asyncio
async def test_a_job_runs_only_on_computers_of_its_kind(group_with_device, read_as, db):
    read_as(info(platforms=["linux/amd64", "windows/amd64"]))
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": img(), "memory": "1Gi"}, expect=201)).json()
    row = await db.get(Job, __import__("uuid").UUID(job["id"]))
    assert row.architectures == ["amd64"] and row.image_info["platforms"] == ["linux/amd64", "windows/amd64"]
    assert (await db.execute(select(ImageInfoCache))).scalars().first() is not None, "read once, then cached"
    await agent.connect([share(g)], caps={**CAPS, "arch": "arm64"})
    assert isinstance(await agent.ready(), pm.NoWork)
    await agent.heartbeat([], shares=[share(g)])
    waiting = await coord.get(f"/api/v1/jobs/{job['id']}/waiting")
    assert waiting["reasons"][0]["code"] == "platform" and "built for amd64" in waiting["reasons"][0]["message"]
    platforms = await coord.get(f"/api/v1/groups/{g}/platforms")
    assert platforms == [{"platform": "linux/arm64", "computers": 1}]
    await agent.close()


@pytest.mark.asyncio
async def test_a_cuda_image_asks_for_a_gpu_only_where_the_group_has_one(group_with_device, read_as):
    read_as(info(cuda=True, ports=["8000/tcp"]))
    coord, g, agent = await group_with_device()
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": img()}, expect=201)).json()
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert any("no computer in the group shares a GPU" in r for r in j["sizing"]["reasons"])
    assert j["ports"] == [8000] and any(r.startswith("An app:") for r in j["sizing"]["reasons"]), \
        "an image that EXPOSEs a port runs as an app"
    assert j["requested"]["gpus"] == 0
    # A computer that shares a GPU with the group.
    gpu_share = {**share(g), "limits": {**share(g)["limits"], "gpu": True}}
    await agent.connect([gpu_share], caps={**CAPS, "gpus": [{"vendor": "nvidia", "model": "x", "memory_mb": 8192}],
                                           "gpu_tiers": ["T1", "T2"]})
    other = img("other")
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": other}, expect=201)).json()
    j = await coord.get(f"/api/v1/jobs/{job['id']}")
    assert j["requested"]["gpus"] == 1 and "GPU 1: the image is built for NVIDIA CUDA." in j["sizing"]["reasons"]
    # A GPU count you give is kept.
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": other, "gpu": 0}, expect=201)).json()
    assert (await coord.get(f"/api/v1/jobs/{job['id']}"))["requested"]["gpus"] == 0
    await agent.close()


@pytest.mark.asyncio
async def test_an_image_that_cannot_be_read_still_runs(group_with_device, monkeypatch):
    def fail(image, cred, **kw):
        raise RegistryError("cannot reach the registry")

    monkeypatch.setattr("pool_cp.api.jobs.inspect_image", fail)
    coord, g, agent = await group_with_device()
    await coord.post(f"/api/v1/groups/{g}/jobs", {"image": img()}, expect=201)
    await agent.connect([share(g)], caps={**CAPS, "arch": "arm64"})
    assert isinstance(await agent.ready(), pm.Offer), "unknown platforms: any computer"
    await agent.close()


# ---- from the security review of step C ----

def test_a_slow_registry_runs_out_of_time():
    import http.server
    import threading
    import time as _time

    class Trickle(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                _time.sleep(0.1)

        def log_message(self, *a):  # quiet
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        started = _time.monotonic()
        with pytest.raises(RegistryError, match="too slow"):
            registry._within(1.0)(registry._fetch_once)(f"http://127.0.0.1:{srv.server_port}/", {}, 5, True)
        assert _time.monotonic() - started < 3, "the whole lookup has one deadline, not one per byte"
    finally:
        srv.shutdown()


def test_odd_metadata_is_refused_or_left_out():
    inf, inf_d = (b'{"config": {"digest": "' + CONFIG_D.encode() + b'"}, "layers": [{"size": Infinity}]}', None)
    inf_d = "sha256:" + hashlib.sha256(inf).hexdigest()
    with pytest.raises(RegistryError, match="Infinity"):
        image_info({inf_d: inf}.__getitem__, STORE.__getitem__, inf_d)
    deep = b"[" * 100_000 + b"]" * 100_000
    deep_d = "sha256:" + hashlib.sha256(deep).hexdigest()
    with pytest.raises(RegistryError):
        image_info({deep_d: deep}.__getitem__, STORE.__getitem__, deep_d)
    config, config_d = blob({"os": "linux", "architecture": "amd64", "config": {
        "ExposedPorts": {"8000/tcp": {}, "x" * 5000: {}, "80": {}}}})
    image, image_d = blob({"config": {"digest": config_d},
                           "layers": [{"size": "big"}, {"size": -5}, {"size": 2 << 20}]})
    index, index_d = blob({"manifests": [
        {"digest": image_d, "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:" + "3" * 64, "platform": {"os": "linux", "architecture": "\x1b[31mred" * 10}},
        {"digest": "not-a-digest", "platform": {"os": "linux", "architecture": "arm64"}},
    ]})
    store = {index_d: index, image_d: image, config_d: config}
    info = image_info(store.__getitem__, store.__getitem__, index_d)
    assert info.platforms == ["linux/amd64"] and info.ports == ["80", "8000/tcp"] and info.size_mb == 2


def test_a_redirect_with_a_user_name_is_not_followed(monkeypatch):
    ref = registry.ImageRef(registry="registry.example.org", repository="lab/app", api="https://registry.example.org",
                            auth_key="registry.example.org")
    sneaky = {"Location": "https://127.0.0.1:8443@cdn.example.net/b"}
    monkeypatch.setattr(registry, "_fetch_once", lambda url, headers, timeout, allow_private: (307, sneaky, b""))
    with pytest.raises(RegistryError, match="does not follow"):
        registry._blob(ref, CONFIG_D, "TOKEN", 5, True)


@pytest.mark.asyncio
async def test_any_failure_while_reading_still_submits(group_with_device, monkeypatch):
    def boom(image, cred, **kw):
        raise RecursionError("deep")

    monkeypatch.setattr("pool_cp.api.jobs.inspect_image", boom)
    coord, g, agent = await group_with_device()
    await coord.post(f"/api/v1/groups/{g}/jobs", {"image": img()}, expect=201)


@pytest.mark.asyncio
async def test_the_cache_is_per_image_name(group_with_device, read_as, db):
    coord, g, agent = await group_with_device()
    digest = __import__("secrets").token_hex(32)
    read_as(info(cuda=True))
    await coord.post(f"/api/v1/groups/{g}/jobs", {"image": f"registry.example.org/lab/a:1@sha256:{digest}"},
                     expect=201)
    read_as(info(cuda=False))
    job = (await coord.post(f"/api/v1/groups/{g}/jobs", {"image": f"registry.example.org/other/b:1@sha256:{digest}"},
                            expect=201)).json()
    assert (await coord.get(f"/api/v1/jobs/{job['id']}"))["image_info"]["cuda"] is False, \
        "another name with the same digest is read on its own"


def test_a_device_cannot_report_an_odd_cpu_type():
    with pytest.raises(Exception):
        pm.Capabilities(os="linux", arch="\x1b[2Jamd64", cpu_cores=1, memory_mb=1, disk_mb=1, tiers=["T1"])
    assert pm.Capabilities(os="linux", arch="arm64", cpu_cores=1, memory_mb=1, disk_mb=1, tiers=["T1"]).arch == "arm64"
