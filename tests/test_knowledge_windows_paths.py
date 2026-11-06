"""Windows realpath aliases observed while creating publication directories."""
import os
from pathlib import Path

import pytest

from omnisignal.knowledge.release import KnowledgeError, _file, _resolved, load_private_key
from omnisignal.workbench import publishing
from test_workbench_publishing import publication

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Win32 extended path semantics')


@pytest.mark.parametrize('extend_root', [False, True])
def test_standard_and_extended_drive_paths_share_containment(tmp_path, monkeypatch, extend_root):
    root = tmp_path/'published'
    candidate = root/'.workbench/staging'
    original = Path.resolve
    def mixed(path, *args, **kwargs):
        resolved = original(path, *args, **kwargs)
        if path == (root if extend_root else candidate):
            return Path('\\\\?\\' + str(resolved))
        return resolved
    monkeypatch.setattr(Path, 'resolve', mixed)
    assert _file(root, '.workbench/staging') == candidate


def test_extended_path_outside_root_stays_rejected(tmp_path, monkeypatch):
    root = tmp_path/'published'
    candidate = root/'images/test.png'
    outside = tmp_path/'outside/test.png'
    original = Path.resolve
    def changed(path, *args, **kwargs):
        if path == candidate:
            return Path('\\\\?\\' + str(original(outside)))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', changed)
    with pytest.raises(KnowledgeError, match='path escapes root'):
        _file(root, 'images/test.png')


def test_extended_private_key_within_root_is_not_treated_as_external(tmp_path, monkeypatch):
    root = tmp_path/'published'
    key = root/'private.pem'
    root.mkdir()
    key.write_bytes(b'synthetic invalid key must never be parsed')
    original = Path.resolve
    def mixed(path, *args, **kwargs):
        resolved = original(path, *args, **kwargs)
        return Path('\\\\?\\' + str(resolved)) if path == key else resolved
    monkeypatch.setattr(Path, 'resolve', mixed)
    with pytest.raises(KnowledgeError, match='private key must be outside'):
        load_private_key(key, root)


@pytest.mark.parametrize(('resolved', 'expected'), [
    (r'\\?\UNC\synthetic-server\share\dir', r'\\synthetic-server\share\dir'),
    (r'\\?\Volume{synthetic}\dir', r'\\?\Volume{synthetic}\dir'),
    (r'\\.\synthetic-device', r'\\.\synthetic-device'),
])
def test_only_standard_unc_alias_normalized_without_network(resolved, expected, monkeypatch):
    # Stub resolve before providing fake UNC strings: no filesystem/network I/O.
    monkeypatch.setattr(Path, 'resolve', lambda self: Path(resolved))
    assert str(_resolved(Path('C:/synthetic'))) == expected


def test_extended_unique_staging_path_still_publishes(publication, monkeypatch):
    root, asset, _ = publication
    original = Path.resolve
    def mixed(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return Path('\\\\?\\' + str(result)) if path.name.startswith('omni-publish-') else result
    monkeypatch.setattr(Path, 'resolve', mixed)
    receipt = publishing.publish_assets(root, [asset], 'synthetic-operator')
    assert receipt['status'] == 'published'
    assert list((root/'.workbench/staging').iterdir()) == []
