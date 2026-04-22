"""
Regression tests for Russian language full-page HTML translation.

A judge whose first language is Russian relies on this. The previous
after_request hook in app.py translated only Arapaho — Russian was
configured in strings.py but never invoked, so switching to Russian
translated only the navbar labels and left every page body in English.
"""

import re

import strings as text


class TestRussianPhraseMap:
    """strings.translate_html on its own."""

    def test_phrase_map_loaded(self):
        assert len(text._phrase_map("ru")) > 100

    def test_translate_html_replaces_common_phrases(self):
        html = "<p>Tournaments</p><p>Save</p><p>Cancel</p>"
        out = text.translate_html(html, lang="ru")
        assert "Турниры" in out
        assert "Сохранить" in out
        assert "Отмена" in out

    def test_translate_html_preserves_tags(self):
        html = '<a href="/x" class="btn">Save</a>'
        out = text.translate_html(html, lang="ru")
        assert 'href="/x"' in out
        assert 'class="btn"' in out
        assert "Сохранить" in out

    def test_translate_html_skips_script_and_style(self):
        html = "<style>body{color:#fff}</style><script>var Save=1;</script><p>Save</p>"
        out = text.translate_html(html, lang="ru")
        assert "var Save=1" in out
        assert "body{color:#fff}" in out
        assert "<p>Сохранить</p>" in out

    def test_english_passes_through_unchanged(self):
        html = "<p>Tournaments</p>"
        assert text.translate_html(html, lang="en") == html


class TestRussianAfterRequestHook:
    """Full Flask round-trip: GET / with Russian session and check body."""

    def test_root_page_translated_when_russian_active(self, client):
        # Switch language by hitting the route (sets session['lang']='ru')
        switch = client.get("/language/ru", follow_redirects=False)
        assert switch.status_code in (200, 302)

        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert re.search(r"[А-Яа-яЁё]", body), (
            "No Cyrillic characters in response body — Russian translation "
            "after_request hook is not running."
        )

    def test_root_page_english_when_no_language_set(self, client):
        # Fresh client (default language)
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert (
            not re.search(r"[А-Яа-яЁё]", body) or "Русский" in body
        ), "English session leaked Cyrillic content unexpectedly."
