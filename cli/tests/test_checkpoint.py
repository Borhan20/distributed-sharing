from pool import checkpoint


def test_save_latest_and_prune(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    assert checkpoint.latest() is None
    src = tmp_path / "model.pt"
    for i in range(5):
        src.write_text(f"step {i}")
        stored = checkpoint.save(src, keep=3)
    assert stored.read_text() == "step 4"
    assert checkpoint.latest() == stored
    assert len([p for p in (tmp_path / "ckpt").iterdir() if p.name.startswith("ckpt-")]) == 3
    assert not [p for p in (tmp_path / "ckpt").iterdir() if p.name.startswith(".tmp-")]


def test_directory_checkpoint_and_resume_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    d = tmp_path / "state"
    d.mkdir()
    (d / "a.txt").write_text("1")
    stored = checkpoint.save(d, name="state")
    assert (stored / "a.txt").read_text() == "1"
    monkeypatch.setenv("POOL_RESUME_DIR", str(tmp_path / "ckpt"))
    assert checkpoint.latest() == stored
