"""Request parsing and selectors, shared by ordinary and final follow-up requests."""
import re


def coerce_requests(value):
    from services.ai.protocol import ContextRequest, ProtocolError, _as_str

    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProtocolError("requests 必须是数组")
    return tuple(ContextRequest(**{key: _as_str(row.get(key)) for key in
        ("type", "commit", "path", "name", "lines", "query", "repository_id", "cursor")})
        for row in value if isinstance(row, dict))


def normalize_line_window(value):
    text = str(value or "").strip()
    if "!" in text:
        from services.ai.excel_range import parse_selection

        selected = parse_selection(text)
        return selected.address(selected.offset)
    match = re.fullmatch(r"([1-9]\d{0,6})(?:-([1-9]\d{0,6}))?", text)
    if not match:
        return ""
    first, last = int(match[1]), int(match[2] or match[1])
    return (str(first) if not match[2] else f"{first}-{last}") if last >= first else ""
