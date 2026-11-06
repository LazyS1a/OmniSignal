"""Checkpoint staging on the data volume, never the bounded container tmpfs."""
from copy import deepcopy
import errno
from pathlib import Path
import threading

import pytest

import omnisignal.workbench.publishing as publishing
from omnisignal.knowledge.release import KnowledgeError, digest, verify_release
from omnisignal.workbench.taxonomy import bind_reviewed_taxonomy
from test_workbench_publishing import publication


def second_asset(first):
    item = {**deepcopy(first), "id": "synthetic-second", "title": "第二个合成案例"}
    item["reviewed_taxonomy"] = bind_reviewed_taxonomy(item,
        {k: first["reviewed_taxonomy"][k] for k in ("tags", "note")}, "reviewer", item["revision"])
    return item


def inventory(root):
    return {p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock")}


def test_full_checkpoint_succeeds_when_default_tempfs_is_unavailable(publication, monkeypatch):
    root, first, key = publication
    real = publishing.tempfile.TemporaryDirectory
    stages = []
    def restricted_tmpfs(*args, **kwargs):
        if kwargs.get("dir") is None:
            raise OSError(errno.ENOSPC, "Synthetic bounded /tmp has no space")
        assert Path(kwargs["dir"]).resolve() == (root / ".workbench/staging").resolve()
        stage = real(*args, **kwargs)
        stages.append(Path(stage.name))
        return stage
    monkeypatch.setattr(publishing.tempfile, "TemporaryDirectory", restricted_tmpfs)
    monkeypatch.setattr(publishing, "CHECKPOINT_CHAIN_LIMIT", 1)
    first_receipt = publishing.publish_assets(root, [first], "synthetic-operator")
    before = inventory(root / "releases" / first_receipt["version"])
    result = publishing.publish_assets(root, [second_asset(first)], "synthetic-operator")
    manifest, records = verify_release(root / "releases" / result["version"], {"synthetic-key": key.public_key()})
    assert manifest["kind"] == "full" and len(records) == 2
    assert inventory(root / "releases" / first_receipt["version"]) == before
    assert len(stages) == 2 and stages[0] != stages[1] and all(not stage.exists() for stage in stages)
    assert list((root / ".workbench/staging").iterdir()) == []


def test_failed_checkpoint_cleans_only_its_temporary_directory(publication, monkeypatch):
    root, first, _ = publication
    publishing.publish_assets(root, [first], "synthetic-operator")
    before = inventory(root)
    other_stage = root / ".workbench/staging/independent-operation"
    other_stage.mkdir()
    sentinel = other_stage / "keep.txt"
    sentinel.write_text("unrelated operation", encoding="utf-8")
    monkeypatch.setattr(publishing, "CHECKPOINT_CHAIN_LIMIT", 1)
    def fail_before_sign():
        raise OSError(errno.ENOSPC, "Synthetic disk error after checkpoint preparation")
    with pytest.raises(OSError):
        publishing.publish_assets(root, [second_asset(first)], "synthetic-operator", before_sign=fail_before_sign)
    after = inventory(root)
    assert all(after.get(path) == digest_before for path, digest_before in before.items())
    assert sentinel.read_text(encoding="utf-8") == "unrelated operation"
    assert sorted(p.name for p in (root / ".workbench/staging").iterdir()) == ["independent-operation"]
    assert len(list((root / "releases").iterdir())) == 1


@pytest.mark.parametrize("component", ["root", "workbench", "staging"])
def test_staging_symlink_is_rejected_without_touching_destination(publication, tmp_path, component):
    root, first, _ = publication
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("preserve", encoding="utf-8")
    link = root if component == "root" else root / ".workbench" if component == "workbench" else root / ".workbench/staging"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(KnowledgeError, match="symlink"):
        publishing.publish_assets(root, [first], "synthetic-operator")
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]


def test_concurrent_preparation_uses_distinct_directories_and_leaves_no_stage(publication, monkeypatch):
    root, first, _ = publication
    barrier = threading.Barrier(2)
    real_prepare = publishing._prepare_asset
    stages = []
    outcomes = []
    def synchronize(asset, stage):
        stages.append(stage)
        barrier.wait(timeout=10)
        return real_prepare(asset, stage)
    monkeypatch.setattr(publishing, "_prepare_asset", synchronize)
    def publish(item):
        try:outcomes.append(publishing.publish_assets(root, [item], "synthetic-operator"))
        except Exception as error:outcomes.append(error)
    threads = [threading.Thread(target=publish, args=(item,)) for item in [first, second_asset(first)]]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)
    assert len(stages) == 2 and stages[0] != stages[1], [(type(o).__name__, str(o)) for o in outcomes if isinstance(o, Exception)]
    assert all(not stage.exists() for stage in stages)
    assert len(outcomes) == 2 and any(isinstance(outcome, dict) for outcome in outcomes)
    assert all(isinstance(outcome, dict) or isinstance(outcome, KnowledgeError) and "locked" in str(outcome) for outcome in outcomes)
