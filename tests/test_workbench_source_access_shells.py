"""Synthetic structures from saved responses; no network or challenge values."""
import pytest

from omnisignal.workbench.source_adapters import (
    SourceAdapterError, discover_projects, extract_project_metadata,
)


ALIYUN_SHELL = '''<html><head>
  <meta name="aliyun_waf_aa" content="synthetic-only">
  <meta name="aliyun_waf_bb" content="synthetic-only">
  <title></title></head><body><script>/* no challenge execution */</script></body></html>'''
EDGEONE_SHELL = '''<script>
  var x = "EO_Bot_Ssid=synthetic-only";
  var y = "__tst_status=synthetic-only";
</script>'''


@pytest.mark.parametrize("source,url,shell", [
    ("zcool", "https://www.zcool.com.cn/work/ZABC==.html", ALIYUN_SHELL),
    ("huaban", "https://huaban.com/pins/123/", EDGEONE_SHELL),
])
def test_verification_shell_stops_before_discovery_or_image_metadata(source, url, shell):
    with pytest.raises(SourceAdapterError, match="^access_barrier$"):
        discover_projects(source, shell)
    with pytest.raises(SourceAdapterError, match="^access_barrier$"):
        extract_project_metadata(source, shell, url)


def test_shell_error_does_not_include_challenge_fields():
    with pytest.raises(SourceAdapterError) as failure:
        discover_projects("zcool", ALIYUN_SHELL)
    assert str(failure.value) == "access_barrier"
    assert "synthetic-only" not in str(failure.value)


@pytest.mark.parametrize("body", [
    '<main><p>A CAPTCHA design with EO_Bot_Ssid= and __tst_status= text.</p></main>',
    '<main><script>var x="EO_Bot_Ssid="; var y="__tst_status=";</script></main>',
    '<meta name="aliyun_waf_aa" content="a single marker is inconclusive">',
])
def test_legitimate_project_content_or_single_marker_is_not_a_shell(body):
    page = '''<html><head><meta property="og:title" content="Synthetic project">
      <meta property="og:image" content="https://img.zcool.cn/community/test.jpg">
      </head><body>''' + body + '</body></html>'
    result = extract_project_metadata("zcool", page, "https://www.zcool.com.cn/work/ZABC==.html")
    assert len(result["candidates"]) == 1
    assert set(result["usage_permissions"].values()) == {"unknown"}


def test_empty_script_shell_without_observed_markers_remains_unknown_structure():
    with pytest.raises(SourceAdapterError, match="^listing_structure_or_access_unavailable$"):
        discover_projects("huaban", '<script>/* unknown app shell */</script>')
