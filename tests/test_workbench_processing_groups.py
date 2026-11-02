"""Filtering must see the whole inventory before slicing pages."""
import io

import pytest
from PIL import Image

from omnisignal.workbench.store import Store, Conflict


def test_processing_group_paginates_the_matching_inventory(tmp_path):
    store = Store(tmp_path / "store")
    for number in range(7):
        image = io.BytesIO()
        Image.new("RGB", (100, 100), (number * 25, 50, 70)).save(image, "PNG")
        store.import_asset({"title": f"fixture-{number}", "filename": f"{number}.png", "source_url": "https://example.invalid/fixture"}, image.getvalue(), "test")
    first = store.list_assets(status="pending", page_size=3, processing_group="waiting_analysis")
    second = store.list_assets(status="pending", page=2, page_size=3, processing_group="waiting_analysis")
    third = store.list_assets(status="pending", page=3, page_size=3, processing_group="waiting_analysis")
    assert first["total"] == second["total"] == third["total"] == 7
    assert [len(part["items"]) for part in [first, second, third]] == [3, 3, 1]
    assert len({item["id"] for part in [first, second, third] for item in part["items"]}) == 7
    assert store.list_assets(status="pending", q="fixture-6", processing_group="waiting_analysis")["total"] == 1
    assert store.list_assets(status="pending", processing_group="technical_failure")["total"] == 0
    with pytest.raises(Conflict, match="分组"):
        store.list_assets(processing_group="all-clear-bypass")
