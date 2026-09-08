from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
import yaml
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from omnisignal.connectors.youtube_visibility import YouTubeVisibilityConnector
from omnisignal.connectors.youtube_visibility_policy import YouTubeVisibilityPolicy
from omnisignal.connectors.youtube_visibility_analysis import SearchSample, analyze_sample, alias_present
from omnisignal.connectors.archive import FileRawResponseArchive
from omnisignal.connectors.durable import run_connector_durably
from omnisignal.contracts import ConnectorFailure, ConnectorSpec
from omnisignal.governance import load_source_approval
from omnisignal.storage import Base, SourceControlState, create_database_engine

ROOT = Path(__file__).parents[1]
OWN_CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22


def policy(top_k=3):
    return YouTubeVisibilityPolicy(query="notes", top_k=top_k, brands=(
        {"id": "own", "name": "Alpha", "role": "owned", "aliases": ["Alpha"], "official_channel_ids": [OWN_CHANNEL]},
        {"id": "rival", "name": "Beta", "role": "competitor", "aliases": ["Beta"]}))


def sample(entries=None, warnings=None):
    if entries is None:
        entries = [
            {"position": 1, "video_id": "aaaaaaaaaaa", "title": "Alpha versus Beta", "channel_id": OTHER_CHANNEL},
            {"position": 2, "video_id": "bbbbbbbbbbb", "title": "Product release", "channel_id": OWN_CHANNEL},
            {"position": 3, "video_id": "ccccccccccc", "title": "Alphabet soup", "channel_id": None}]
    return SearchSample(query="notes", top_k=3, fetched_at="2026-09-06T00:00:00Z", collector_version="fixture/1",
                        entries=entries, warnings=warnings or [])


def make_connector(tmp_path, worker):
    spec = ConnectorSpec.model_validate(yaml.safe_load((ROOT / "examples/connectors/youtube_visibility.yaml").read_text()))
    approval = load_source_approval(ROOT / "governance/source_registry.yaml", spec)
    return YouTubeVisibilityConnector(spec, policy(), approval, workspace=tmp_path,
                                      archive=FileRawResponseArchive(tmp_path / "raw"), worker_runner=worker)


def test_counts_overlap_official_origin_and_real_denominator():
    report = analyze_sample(policy(), sample())
    own, rival = report["brands"]
    assert own["count"] == 2 and own["denominator"] == 3
    assert own["sample_share_percent"] == 66.6667
    assert own["official_count"] == 1 and own["third_party_title_count"] == 1
    assert rival["count"] == 1 and rival["first_position"] == 1
    assert report["evidence"][0]["matches"][0]["aliases"] == ["alpha"]
    assert report["evidence"][2]["matches"] == []


@pytest.mark.parametrize("text,alias,expected", [
    ("Notional tools", "Notion", False), ("NOTION AI", "notion", True),
    ("Ｎｏｔｉｏｎ", "notion", True), ("试用山峡哨兵", "山峡哨兵", True),
    ("C++ review", "C++", True)])
def test_alias_boundaries(text, alias, expected):
    assert alias_present(text, alias) == expected


@pytest.mark.parametrize("case", ["empty", "short", "duplicate", "warning"])
def test_incomplete_samples_never_emit_percentage(case):
    data = sample().model_dump(mode="json")
    if case == "empty": data["entries"] = []
    if case == "short": data["entries"] = data["entries"][:2]
    if case == "duplicate": data["entries"][2]["video_id"] = data["entries"][0]["video_id"]
    if case == "warning": data["warnings"] = ["extractor_warning"]
    report = analyze_sample(policy(), SearchSample.model_validate(data))
    assert report["quality_status"] == "incomplete"
    assert all(brand["sample_share_percent"] is None for brand in report["brands"])


def test_scope_drift_and_channel_name_impersonation_are_rejected():
    with pytest.raises(ValueError):
        analyze_sample(policy(4), sample())
    data = policy().model_dump(mode="json")
    data["brands"][0]["official_channel_ids"] = ["@Alpha"]
    with pytest.raises(ValueError): YouTubeVisibilityPolicy.model_validate(data)
    data = sample().model_dump(mode="json")
    data["entries"][1]["position"] = 1
    with pytest.raises(ValueError): analyze_sample(policy(), SearchSample.model_validate(data))


def test_snapshot_replay_is_idempotent_and_archive_linked(tmp_path):
    async def scenario():
        async def worker(**kwargs): return sample().model_dump(mode="json")
        url = URL.create("sqlite+pysqlite", database=str(tmp_path / "test.db")).render_as_string()
        async def run():
            connector = make_connector(tmp_path, worker)
            return await run_connector_durably(connector, database_url=url, database_target="test", initialize_schema=True)
        first, second = await run(), await run()
        assert first.inserted == 1 and second.unchanged == 1 and second.inserted == 0
        assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == 1
    asyncio.run(scenario())


def test_disabled_source_makes_no_network_call(tmp_path):
    url = URL.create("sqlite+pysqlite", database=str(tmp_path / "test.db")).render_as_string()
    engine = create_database_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(SourceControlState(source_id="youtube_visibility", enabled=False, updated_by="test", reason="test"))
        session.commit()
    engine.dispose()
    async def scenario():
        async def worker(**kwargs): pytest.fail("disabled worker called")
        with pytest.raises(ConnectorFailure):
            await run_connector_durably(make_connector(tmp_path, worker), database_url=url,
                                        database_target="test", initialize_schema=False)
    asyncio.run(scenario())
