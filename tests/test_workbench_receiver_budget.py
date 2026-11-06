from copy import deepcopy
from datetime import datetime, timezone
import json

import httpx
import pytest

from omnisignal.workbench import receiver_budget as gate
from omnisignal.workbench.store import Store


def sha(n):
    return f"{n:064x}"


def item(n):
    return {"id": str(n), "revision": 1, "sha256": sha(n)}


POLICY = {"recipient": {"origin": "https://receiver.example", "workspace_id": "synthetic"}}


class Receiver:
    origin = "https://receiver.example"
    def __init__(self, known=(), received=0):
        self.known, self.received, self.calls = set(known), received, []
        self.day, self.revision = "2026-11-05", "r1"
    def preflight(self, hashes):
        self.calls.append(hashes)
        return {"schema_version": 1, "timezone": "Asia/Shanghai", "day": self.day,
            "enabled": True, "initialized": True, "metric": "first_successful_unique_image_sha256",
            "received": self.received, "target": 100, "remaining": max(0, 100 - self.received),
            "baseline_count": 229, "known_sha256": sorted(self.known & set(hashes)),
            "new_unique_images": len(set(hashes) - self.known), "advisory": True,
            "atomic_package_required": True, "receipt_revision": self.revision, "baseline_revision": "b1"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path, clock=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp())
    signed = []
    monkeypatch.setattr(gate, "current_publication_cases", lambda root: {str(n): {"images": [{"path": f"images/{sha(n)}.jpg"}]} for n in signed})
    monkeypatch.delenv(gate.CONFIG_ENV, raising=False)
    return store, signed


def test_target_is_actual_receipt_not_signed_count(setup):
    store, signed = setup
    signed.extend(range(1, 11))
    receiver = Receiver([sha(n) for n in range(1, 8)], received=95)
    selected, report = gate.select_publication_batch(store, [item(n) for n in range(11, 21)], POLICY, client=receiver)
    assert [x["id"] for x in selected] == ["11", "12"]
    assert report["received"] == 95
    assert report["signed_unreceived"] == 3
    assert report["selected_new_unique"] == 2
    assert report["human_reviewed"] is None


@pytest.mark.parametrize("target", [400, 500])
def test_extended_target_reserves_actual_outstanding_images(setup, target):
    store, signed = setup
    signed.extend([1, 2])
    receiver = Receiver([sha(1)], received=target-2)
    original = receiver.preflight
    receiver.preflight = lambda hashes: {**original(hashes), "target": target, "remaining": 2}
    selected, report = gate.select_publication_batch(store, [item(3), item(4)], POLICY, client=receiver)
    assert selected == [item(3)]
    assert report["signed_unreceived"] == 1


def test_restart_and_next_day_preserve_unreceived_reservation(setup):
    store, signed = setup
    signed.extend(range(1, 101))
    receiver = Receiver()
    assert gate.select_publication_batch(store, [item(101)], POLICY, client=receiver)[0] == []
    # A date reset alone cannot forget already signed unreceived images.
    store.clock = lambda: datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()
    receiver.day = "2026-11-06"
    assert gate.select_publication_batch(store, [item(101)], POLICY, client=receiver)[0] == []
    receiver.known.update(sha(n) for n in range(1, 101))
    assert gate.select_publication_batch(store, [item(101)], POLICY, client=receiver)[0] == [item(101)]


def test_existing_and_duplicate_hashes_do_not_consume_quota(setup):
    store, signed = setup
    receiver = Receiver([sha(1)], received=99)
    clone = {**item(2), "id": "clone"}
    selected, report = gate.select_publication_batch(store, [item(1), item(2), clone, item(3)], POLICY, client=receiver)
    assert selected == [item(1), item(2), clone]
    assert report["selected_new_unique"] == 1


def test_permanent_known_cache_only_reuses_same_baseline(setup):
    store, signed = setup
    signed.extend(range(1, 1002))
    receiver = Receiver([sha(n) for n in signed])
    gate.select_publication_batch(store, [item(1002)], POLICY, client=receiver)
    assert all(len(x) <= 1000 for x in receiver.calls)
    receiver.calls.clear()
    gate.select_publication_batch(store, [item(1002)], POLICY, client=receiver)
    assert receiver.calls == [[], [sha(1002)]]
    original = receiver.preflight
    receiver.preflight = lambda hashes: {**original(hashes), "baseline_revision": "b2"}
    receiver.calls.clear()
    gate.select_publication_batch(store, [], POLICY, client=receiver)
    assert len(receiver.calls) == 3


@pytest.mark.parametrize("patch", [
    {"initialized": False}, {"enabled": False}, {"day": "2026-11-04"},
    {"target": 301}, {"received": True}, {"remaining": 101},
    {"known_sha256": [sha(555)]}, {"metric": "signed"}, {"receipt_revision": None},
])
def test_malformed_or_stale_receipts_fail_closed(setup, patch):
    store, _ = setup
    receiver = Receiver()
    original = receiver.preflight
    receiver.preflight = lambda hashes: {**original(hashes), **patch}
    with pytest.raises(gate.ReceiptUnavailable):
        gate.select_publication_batch(store, [item(1)], POLICY, client=receiver)


def test_concurrent_receiver_change_does_not_mix_snapshots(setup):
    store, _ = setup
    receiver = Receiver()
    original = receiver.preflight
    receiver.preflight = lambda hashes: {**original(hashes), "receipt_revision": "changed" if hashes else "first"}
    with pytest.raises(gate.ReceiptUnavailable, match="changed"):
        gate.select_publication_batch(store, [item(1)], POLICY, client=receiver)


def test_disabled_preserves_legacy_behavior(setup):
    store, _ = setup
    batch = [item(1)]
    assert gate.select_publication_batch(store, batch, None) == (batch, {"status": "disabled", "receipt_status": "unconfirmed"})


def test_config_scope_mismatch_stops_without_network(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("synthetic-test-token-not-a-secret")
    config = tmp_path / "receipt.json"
    config.write_text(json.dumps({"schema_version": 1, "origin": "https://receiver.example",
        "token_file": str(token), "recipient": {**POLICY["recipient"], "workspace_id": "different"}}))
    monkeypatch.setenv(gate.CONFIG_ENV, str(config))
    with pytest.raises(gate.ReceiptUnavailable, match="config_invalid"):
        gate.configured_client(POLICY)


def test_http_redirect_and_error_bodies_are_never_exposed():
    for status in (302, 401, 403, 500):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(status, headers={"location": "https://other.example"}, text="sensitive upstream body")
        client = gate.ReceiptClient("https://receiver.example", "synthetic-test-token-not-a-secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(gate.ReceiptUnavailable, match="^receiver_receipt_unavailable$"):
            client.preflight([sha(1)])
        assert len(calls) == 1


def test_corrupt_known_cache_fails_closed(setup):
    store, _ = setup
    receiver = Receiver()
    gate.select_publication_batch(store, [item(1)], POLICY, client=receiver)
    with store.db() as db:
        db.execute("UPDATE meta SET value='[]' WHERE key LIKE 'receiver_known.v1:%'")
    with pytest.raises(gate.ReceiptUnavailable):
        gate.select_publication_batch(store, [item(1)], POLICY, client=receiver)
