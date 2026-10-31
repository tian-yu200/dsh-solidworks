"""Shared offline attachment resolution for both OCR and crops."""
import hashlib
import pytest
from PIL import Image
import intake
import ocr


def test_attachment_id_reaches_crop_and_ocr(tmp_path, monkeypatch):
    source = tmp_path / 'drawing.png'
    Image.new('RGB', (120, 90), 'white').save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    home = tmp_path / 'instance-home'
    obj = home / 'attachments/v1/objects' / digest[:2] / digest
    obj.parent.mkdir(parents=True)
    obj.write_bytes(source.read_bytes())
    monkeypatch.setenv('DSH_HOME', str(home))
    monkeypatch.setattr(intake, 'INTAKE_ROOT', tmp_path / 'artifacts/intake')
    monkeypatch.setattr(ocr, '_run_helper', lambda *a, **k: {'lang': 'en-US', 'lines': []})
    assert intake.read_region('sha256:' + digest)[1]['source_pixels'] == [120, 90]
    assert ocr.read_text('sha256:' + digest)['source_pixels'] == [120, 90]


@pytest.mark.parametrize('source,code', [
    ('sha256:bad', 'INVALID_ATTACHMENT_ID'),
    ('sha256:' + 'a' * 64, 'ATTACHMENT_NOT_FOUND'),
])
@pytest.mark.parametrize('reader', [intake.read_region, ocr.read_text])
def test_invalid_attachment_fails_once_without_path_guessing(tmp_path, monkeypatch, source, code, reader):
    monkeypatch.setenv('DSH_HOME', str(tmp_path))
    with pytest.raises(RuntimeError, match=code):
        reader(source)
