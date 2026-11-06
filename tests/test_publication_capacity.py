"""Offline capacity boundaries: real local signatures, files, and SQLite."""
from copy import deepcopy
import json

from PIL import Image
import pytest

import omnisignal.knowledge.release as protocol
import omnisignal.workbench.publishing as publishing
from test_knowledge_release import setup as protocol_setup
from test_workbench_publishing import publication
from test_workbench_auto_editorial import setup as auto_setup, asset
from omnisignal.workbench.taxonomy import bind_reviewed_taxonomy


def forbid_sign(monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("must_reject_before_signature")
    monkeypatch.setattr(protocol, "_signed", forbidden)
    return calls


def test_aggregate_record_and_image_budget_rejected_before_signature(protocol_setup, monkeypatch):
    base, _ = protocol_setup
    measured = protocol.release_capacity(base['records'], base['asset_root'])
    assert measured['record_bytes'] < measured['package_bytes']
    calls = forbid_sign(monkeypatch)
    with pytest.raises(protocol.KnowledgeError, match="package size"):
        protocol.publish_release(**base, max_package_bytes=measured['package_bytes']-1)
    assert calls == [] and not (base['root']/'latest.json').exists()
    assert list((base['root']/'releases').iterdir()) == []


def test_shared_image_counted_once_at_exact_limit(protocol_setup):
    base, case = protocol_setup
    second = {**deepcopy(case), 'case_id': 'second-case'}
    base['records'].append(protocol.make_record(second))
    measured = protocol.release_capacity(base['records'], base['asset_root'])
    assert measured['image_bytes'] == (base['asset_root']/'images/test.png').stat().st_size
    manifest = protocol.publish_release(**base, max_package_bytes=measured['package_bytes'])
    assert sum(item['size'] for item in manifest['files']) == measured['package_bytes']


def test_record_capacity_fails_before_signing(protocol_setup, monkeypatch):
    base, _ = protocol_setup
    calls = forbid_sign(monkeypatch)
    monkeypatch.setattr(protocol, 'MAX_RECORD_BYTES', 50)
    with pytest.raises(protocol.KnowledgeError, match='record too large|oversized record'):
        protocol.publish_release(**base)
    assert calls == [] and not (base['root']/'latest.json').exists()


def test_staged_bytes_are_rechecked_after_capacity_probe(protocol_setup, monkeypatch):
    base, _ = protocol_setup
    original = protocol.release_capacity
    measured = original(base['records'], base['asset_root'])
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        Image.new('RGB', (200, 200), 'blue').save(base['asset_root']/'images/test.png')
        return result
    monkeypatch.setattr(protocol, 'release_capacity', changed)
    calls = forbid_sign(monkeypatch)
    with pytest.raises(protocol.KnowledgeError, match='package size'):
        protocol.publish_release(**base, max_package_bytes=measured['package_bytes'])
    assert calls == [] and not (base['root']/'latest.json').exists()


def test_oversized_manifest_rejected_before_signature(protocol_setup, monkeypatch):
    base, case = protocol_setup
    records = []
    original = (base['asset_root']/'images/test.png').read_bytes()
    for index in range(20):
        item = {**deepcopy(case), 'case_id': f'case-{index}'}
        name = f'images/image-{index}.png'
        item['images'][0]['path'] = name
        (base['asset_root']/name).write_bytes(original)
        records.append(protocol.make_record(item))
    base['records'] = records
    monkeypatch.setattr(protocol, 'MAX_RECORD_BYTES', 2000)
    assert all(len(protocol.canonical(record)) < 2000 for record in records)
    calls = forbid_sign(monkeypatch)
    with pytest.raises(protocol.KnowledgeError, match='manifest exceeds'):
        protocol.publish_release(**base)
    assert calls == [] and not (base['root']/'latest.json').exists()


def test_manifest_exact_boundary_includes_atomic_json_newline(protocol_setup, monkeypatch):
    base, case = protocol_setup
    records = []
    original = (base['asset_root']/'images/test.png').read_bytes()
    for index in range(20):
        item = {**deepcopy(case), 'case_id': f'boundary-{index}'}
        name = f'images/boundary-{index}.png'
        item['images'][0]['path'] = name
        (base['asset_root']/name).write_bytes(original)
        records.append(protocol.make_record(item))
    base.update(records=records, created_at='2026-11-06T00:00:00.000Z')
    probe = {**base, 'root': base['root'].with_name('size-probe')}
    manifest = protocol.publish_release(**probe)
    encoded = protocol.canonical(manifest)
    assert (probe['root']/'releases/release-1/manifest.json').read_bytes() == encoded + b'\n'
    assert all(len(protocol.canonical(record)) < len(encoded) for record in records)
    # The canonical envelope alone fits exactly, but its actual file does not.
    with monkeypatch.context() as patch:
        patch.setattr(protocol, 'MAX_RECORD_BYTES', len(encoded))
        calls = forbid_sign(patch)
        with pytest.raises(protocol.KnowledgeError, match='manifest exceeds'):
            protocol.publish_release(**base)
        assert calls == [] and not (base['root']/'latest.json').exists()
        assert list((base['root']/'releases').iterdir()) == []
    # One additional byte permits the complete on-disk envelope at the boundary.
    monkeypatch.setattr(protocol, 'MAX_RECORD_BYTES', len(encoded) + 1)
    protocol.publish_release(**base)
    assert (base['root']/'releases/release-1/manifest.json').stat().st_size == len(encoded) + 1


def test_checkpoint_uses_historical_inventory_and_preserves_last_release(publication, monkeypatch):
    root, first, key = publication
    items = [deepcopy(first)]
    for index in (2, 3):
        item = {**deepcopy(first), 'id': f'synthetic-{index}'}
        item['reviewed_taxonomy'] = bind_reviewed_taxonomy(item,
            {k:first['reviewed_taxonomy'][k] for k in ('tags','note')}, 'reviewer', item['revision'])
        items.append(item)
    for item in items[:2]:
        publishing.publish_assets(root, [item], 'operator')
    previous = (root/'latest.json').read_bytes()
    previous_records = protocol.materialize_release(root)
    # Both single-item batches fit. The merged three-record checkpoint does not.
    two_raw = sum(len(protocol.canonical(protocol.make_record(case)))+1 for case in previous_records.values())
    image_size = next((root/'releases').glob('*/images/*')).stat().st_size
    monkeypatch.setattr(publishing, 'publication_package_budget', lambda: two_raw + image_size + 50)
    monkeypatch.setattr(publishing, 'CHECKPOINT_CHAIN_LIMIT', 2)
    called = []
    with pytest.raises(protocol.KnowledgeError, match='checkpoint exceeds|package size'):
        publishing.publish_assets(root, [items[2]], 'operator', before_sign=lambda: called.append(True))
    assert called == [] and (root/'latest.json').read_bytes() == previous
    assert protocol.materialize_release(root) == previous_records
    assert len(list((root/'releases').iterdir())) == 2
    assert list((root/'.workbench/staging').iterdir()) == []


@pytest.mark.parametrize('value', ['0','513','-1','1.5','abc','01','１２８'])
def test_invalid_receiver_limit_fails_closed(monkeypatch, value):
    monkeypatch.setenv(publishing.PUBLICATION_BUDGET_ENV, value)
    with pytest.raises(protocol.KnowledgeError, match='invalid receiver'):
        publishing.publication_package_budget()


def test_unconfigured_receiver_limit_is_explicit_default_not_legacy_512(auto_setup, monkeypatch):
    store, _, _, _, _ = auto_setup
    monkeypatch.delenv(publishing.PUBLICATION_BUDGET_ENV, raising=False)
    selected, report = publishing.select_capacity_batch(store, [])
    assert selected == [] and report['max_package_bytes'] == 128 * 1024 * 1024
    assert report['limit_source'] == 'receiver_default_128_mib'
    monkeypatch.setenv(publishing.PUBLICATION_BUDGET_ENV, '256')
    assert publishing.select_capacity_batch(store, [])[1]['limit_source'] == 'explicit'
    assert publishing.publication_package_budget() == 256 * 1024 * 1024


def test_automatic_unsigned_batch_spans_ticks_without_new_model_calls(auto_setup, monkeypatch):
    store, analysis, auto, provider, _ = auto_setup
    items = [asset(store, i, publish=True) for i in (1, 2)]
    auto.tick()
    for item in items:
        current = store.asset(item['id'])
        analysis.enqueue(current['id'], current['revision'], 'synthetic-operator')
        analysis.run_pending()
    calls = provider.calls
    limit = publishing.MAX_RECORD_BYTES + 1 + max((store.images/item['image_file']).stat().st_size for item in items)
    monkeypatch.setattr(publishing, 'publication_package_budget', lambda: limit)
    first = auto.tick()
    assert first['published'] == 1 and first['publication_capacity']['deferred'] == 1
    assert first['publication_capacity']['checkpoint_included'] is False
    second = auto.tick()
    assert second['published'] == 1 and provider.calls == calls == 2
    assert len(protocol.materialize_release(store.root/'published')) == 2


def test_frozen_batch_never_split_by_new_capacity_setting(auto_setup, monkeypatch):
    store, analysis, auto, provider, _ = auto_setup
    items = [asset(store, i, publish=True) for i in (1, 2)]
    auto.tick()
    for item in items:
        current = store.asset(item['id'])
        analysis.enqueue(current['id'], current['revision'], 'synthetic-operator')
        analysis.run_pending()
    import omnisignal.workbench.auto_editorial as worker
    original = worker.publish_store_assets
    def crash(*args, **kwargs):
        raise OSError('synthetic_pre_signature_failure')
    monkeypatch.setattr(worker, 'publish_store_assets', crash)
    auto.tick()
    key = auto._publication_batch_key(auto.configuration())
    with store.db() as db:
        frozen = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]
    assert len(json.loads(frozen)) == 2
    monkeypatch.setattr(worker, 'publish_store_assets', original)
    def no_resplit(*args, **kwargs):
        raise AssertionError('frozen_batch_must_not_be_reselected')
    monkeypatch.setattr(worker, 'select_capacity_batch', no_resplit)
    monkeypatch.setattr(publishing, 'publication_package_budget', lambda: 1)
    result = auto.tick()
    assert result['published'] == 0
    assert result['blocked_reasons']['publication_capacity_exceeded'] == 2
    assert result['publication_capacity'] == {'status': 'blocked', 'reason': 'final_release_capacity_exceeded'}
    with store.db() as db:
        assert db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0] == frozen
    assert provider.calls == 2
