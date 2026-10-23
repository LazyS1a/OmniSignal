import io
from PIL import Image
import pytest
from omnisignal.knowledge.curation import image_candidates, inspect_image
from omnisignal.knowledge.release import KnowledgeError


def test_excludes_recommendations_variants_and_non_content_without_guessing_cover():
    html = '''<img src="https://cdn.example/header.jpg"><article>
      <img src="https://cdn.example/a.jpg?class=w1080" width="1600" height="900">
      <img src="https://cdn.example/a.jpg?class=w320">
      <div class="listing-item"><img src="https://cdn.example/unrelated.jpg"></div>
      <img src="https://cdn.example/spin.gif">
      <img src="https://cdn.example/pixel.png" width="1" height="1">
      <img src="https://elsewhere.example/other.jpg">
      <img src="https://cdn.example/b.jpg?asset=2">
      </article>'''
    result = image_candidates(html, 'https://site.example/article', allowed_hosts=['cdn.example'])
    assert len(result['candidates']) == 2
    assert {x['reason'] for x in result['rejected']} == {
        'same_image_variant', 'related_or_navigation', 'unsupported_static_format', 'small_or_banner', 'outside_image_scope'}
    assert all(x['selection_status'] == 'candidate_needs_visual_review' for x in result['candidates'])


def test_image_validation_and_byte_dedup():
    stream = io.BytesIO()
    Image.new('RGB', (640, 480), 'blue').save(stream, format='PNG')
    seen = set()
    assert inspect_image(stream.getvalue(), seen)['width'] == 640
    with pytest.raises(KnowledgeError, match='duplicate_image_bytes'):
        inspect_image(stream.getvalue(), seen)
    with pytest.raises(KnowledgeError):
        inspect_image(b'<html>error page</html>', set())


def test_keeps_distinct_resource_ids_and_handles_lazy_src():
    html = '''<article><img src="data:image/gif;base64,xxx" data-src="https://cdn.example/p.png?id=1">
    <img src="https://cdn.example/p.png?id=2"></article>'''
    result = image_candidates(html, 'https://site.example/a', allowed_hosts=['cdn.example'])
    assert len(result['candidates']) == 2
