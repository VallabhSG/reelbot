import pytest

from reelbot.templating import TemplateError, extract, render


def test_exact_placeholder_keeps_raw_type():
    out = render({"duration": "{duration}", "label": "{duration}s clip"}, {"duration": 5})
    assert out == {"duration": 5, "label": "5s clip"}


def test_list_placeholder_is_spliced_and_none_dropped():
    tpl = {"image_urls": ["{image_url}", "{reference_urls}"], "negative": "{neg}"}
    out = render(tpl, {"image_url": "a", "reference_urls": ["b", "c"], "neg": None})
    assert out == {"image_urls": ["a", "b", "c"]}


def test_empty_reference_list_splices_to_nothing():
    out = render(["{image_url}", "{reference_urls}"], {"image_url": "a", "reference_urls": []})
    assert out == ["a"]


def test_missing_placeholder_raises_with_context():
    with pytest.raises(TemplateError, match="image_url"):
        render({"x": "{image_url}"}, {"prompt": "p"})


def test_braces_in_values_are_not_reinterpreted():
    assert render("{prompt}!", {"prompt": "a {weird} prompt"}) == "a {weird} prompt!"


def test_extract_dotted_path():
    assert extract({"images": [{"url": "u0"}, {"url": "u1"}]}, "images.1.url") == "u1"
    assert extract({"video": {"url": "v"}}, "video.url") == "v"
    with pytest.raises(KeyError):
        extract({"video": {}}, "video.url")


def test_empty_lists_are_dropped_from_request_bodies():
    assert render({"prompt": "{p}", "image_urls": "{refs}"}, {"p": "x", "refs": []}) == {"prompt": "x"}
