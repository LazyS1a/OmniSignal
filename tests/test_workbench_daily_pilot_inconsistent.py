"""An isolated legacy asset must not prevent the day's healthy work from admission."""
from copy import deepcopy

from test_workbench_daily_pilot import add, pilot, prepared, when


def test_inconsistent_asset_is_skipped_without_blocking_healthy_daily_batch(prepared):
    store, analysis, provider, _ = prepared
    damaged = add(store, 1)
    healthy = add(store, 2)
    with store.db() as db:
        db.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (damaged["id"],))
        original = dict(db.execute("SELECT * FROM assets WHERE id=?", (damaged["id"],)).fetchone())
    first = pilot(prepared)
    result = first.tick(when())
    assert result["diagnostic"] is None
    assert result["report"]["submitted"] == 1
    assert result["report"]["skip_reasons"] == {"storage_inconsistent": 1}
    assert analysis.jobs()[0]["asset_id"] == healthy["id"]
    assert provider.calls == 0
    # Restart revalidates the persisted skip code, reuses the original receipt,
    # and never silently repairs the bad row or submits it under another revision.
    restarted = pilot(prepared).tick(when(hour=17))
    assert restarted["diagnostic"] is None
    assert restarted["report"]["submitted"] == 1
    assert restarted["report"]["skip_reasons"] == {"storage_inconsistent": 1}
    assert len(analysis.jobs()) == 1 and provider.calls == 0
    with store.db() as db:
        assert dict(db.execute("SELECT * FROM assets WHERE id=?", (damaged["id"],)).fetchone()) == original


def test_all_inconsistent_assets_persist_an_empty_day_without_calls(prepared):
    store, analysis, provider, _ = prepared
    damaged = add(store, 1)
    with store.db() as db:
        db.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (damaged["id"],))
    p = pilot(prepared)
    result = p.tick(when())
    assert result["state"] == "no_eligible" and result["diagnostic"] is None
    assert result["report"]["skip_reasons"] == {"storage_inconsistent": 1}
    assert result["report"]["submitted"] == 0
    assert pilot(prepared).tick(when(hour=17))["diagnostic"] is None
    assert not analysis.jobs() and provider.calls == 0


def test_unknown_preflight_reason_still_blocks_new_work(prepared, monkeypatch):
    store, analysis, provider, _ = prepared
    add(store, 1)
    original = analysis.preview_batch

    def changed(items):
        preview = deepcopy(original(items))
        preview["items"][0].update(disposition="skipped", reason_code="unrecognized_reason")
        return preview

    monkeypatch.setattr(analysis, "preview_batch", changed)
    result = pilot(prepared).tick(when())
    assert result["state"] == "blocked" and result["diagnostic"] == "invalid_state"
    assert not analysis.jobs() and provider.calls == 0
