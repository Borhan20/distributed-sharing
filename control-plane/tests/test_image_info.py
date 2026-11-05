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
    assert any("opens port 8000/tcp" in r for r in j["sizing"]["reasons"])
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
