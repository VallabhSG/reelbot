"""Tiny templating for provider request bodies and dotted-path extraction of responses.

Model input schemas differ per endpoint and change often, so request bodies are declared in the
series YAML instead of hard-coded. Rules:
  * a string that is exactly "{key}" is replaced by the raw value (keeps lists / ints intact)
  * "{key}" inside a longer string is replaced by str(value)
  * a list item that is exactly "{key}" whose value is a list is spliced in
  * keys whose rendered value is None or an empty list are dropped (APIs reject `"image_urls": []`)
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class TemplateError(KeyError):
    pass


def _lookup(ctx: dict[str, Any], key: str) -> Any:
    if key not in ctx:
        raise TemplateError(f"template placeholder '{{{key}}}' has no value; available: {sorted(ctx)}")
    return ctx[key]


def render(template: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole:
            return _lookup(ctx, whole.group(1))
        return _PLACEHOLDER.sub(lambda m: str(_lookup(ctx, m.group(1))), template)
    if isinstance(template, list):
        out: list[Any] = []
        for item in template:
            value = render(item, ctx)
            if isinstance(item, str) and _PLACEHOLDER.fullmatch(item) and isinstance(value, list):
                out.extend(value)
            elif value is not None:
                out.append(value)
        return out
    if isinstance(template, dict):
        rendered = {k: render(v, ctx) for k, v in template.items()}
        return {k: v for k, v in rendered.items() if v is not None and v != []}
    return template


def extract(data: Any, path: str) -> Any:
    """extract({"images": [{"url": "x"}]}, "images.0.url") -> "x" """
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(f"cannot descend into {type(cur).__name__} at '{part}' (path '{path}')")
    return cur
