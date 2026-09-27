from pathlib import Path
import subprocess
import pytest
from scripts.sync_workspace import sync


def source_repo(path):
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path / 'tool.py').write_text('new code')
    (path / '.env').write_text('source secret')
    (path / 'data').mkdir()
    (path / 'data/db').write_text('source data')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)


def test_preserves_runtime_and_excludes_source_secrets(tmp_path):
    src, dst = tmp_path/'src', tmp_path/'dst'
    source_repo(src)
    (dst/'data').mkdir(parents=True)
    (dst/'data/db').write_text('live database')
    (dst/'.env').write_text('live secret')
    (dst/'tool.py').write_text('old code')
    assert sync(src, dst) == 1
    assert (dst/'tool.py').read_text() == 'new code'
    assert (dst/'data/db').read_text() == 'live database'
    assert (dst/'.env').read_text() == 'live secret'


def test_rejects_symlink_before_writing(tmp_path):
    src, dst = tmp_path/'src', tmp_path/'dst'
    source_repo(src)
    dst.mkdir()
    other = tmp_path/'other'
    other.write_text('untouched')
    (dst/'tool.py').symlink_to(other)
    with pytest.raises(ValueError):
        sync(src, dst)
    assert other.read_text() == 'untouched'
