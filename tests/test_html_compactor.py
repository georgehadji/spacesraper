# Tests for the prompt HTML compactor: it must cut token-heavy noise while
# preserving the structure a model needs to infer CSS selectors.

from src.infrastructure.ai.html_compactor import compact_html_for_prompt


def test_drops_scripts_and_styles():
    html = """
    <html><head><style>.a{color:red}</style><script>var x=1;</script></head>
    <body><div class="row"><h2 class="title">Tender A</h2></div></body></html>
    """
    out = compact_html_for_prompt(html)
    assert "var x=1" not in out
    assert "color:red" not in out
    assert 'class="row"' in out
    assert "Tender A" in out


def test_strips_noise_attributes_but_keeps_selectors():
    html = '<div class="row" id="r1" style="color:red" onclick="go()" data-track="xyz">Hi</div>'
    out = compact_html_for_prompt(html)
    assert 'class="row"' in out
    assert 'id="r1"' in out
    assert "style" not in out
    assert "onclick" not in out
    assert "data-track" not in out


def test_removes_comments():
    out = compact_html_for_prompt("<div><!-- tracking pixel beacon --><p>Body</p></div>")
    assert "tracking pixel" not in out
    assert "Body" in out


def test_keeps_href_for_link_selectors():
    out = compact_html_for_prompt('<a href="/tender/1" class="lnk">Open</a>')
    assert 'href="/tender/1"' in out
    assert 'class="lnk"' in out


def test_truncates_long_href_payloads():
    html = '<img src="data:image/png;base64,' + ("A" * 5000) + '"><a href="' + ("b" * 5000) + '">x</a>'
    out = compact_html_for_prompt(html)
    assert len(out) <= 6000
    assert "A" * 1000 not in out


def test_respects_max_chars():
    html = "<div>" + ("<p class='x'>text</p>" * 5000) + "</div>"
    assert len(compact_html_for_prompt(html, max_chars=1000)) <= 1000


def test_reduces_size_on_realistic_markup():
    html = """
    <html><head>
      <script>window.dataLayer=[];function t(){for(var i=0;i<100;i++){console.log(i);}}</script>
      <style>body{margin:0}.grid{display:flex}.card{padding:12px}</style>
    </head><body>
      <div class="grid" style="display:flex" data-analytics="grid-1" onclick="track()">
        <div class="card" data-id="1" style="padding:12px"><h3 class="t">A</h3></div>
        <div class="card" data-id="2" style="padding:12px"><h3 class="t">B</h3></div>
      </div>
    </body></html>
    """
    out = compact_html_for_prompt(html)
    assert len(out) < len(html)
    # Structure a selector needs survives.
    assert 'class="card"' in out and 'class="t"' in out


def test_handles_empty_and_non_string():
    assert compact_html_for_prompt("") == ""
    assert compact_html_for_prompt(None) == ""


def test_malformed_html_falls_back_safely():
    out = compact_html_for_prompt("<div><span>unclosed", max_chars=100)
    assert "unclosed" in out
    assert len(out) <= 100
