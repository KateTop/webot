"""Parse WeChat app-message quotes without relying on display names."""

import xml.etree.ElementTree as ET


def parse_quote(content: str, own_wxid: str) -> tuple[bool, str, str]:
    """Return (quotes_this_account, new_text, quoted_text).

    WeChat type 49 / appmsg type 57 carries the original sender in
    refermsg/fromusr. Extract the new text for every valid quote so mentions
    in a reply to another person do not pass raw XML to the router.
    A display name is not an identity and is never enough.
    """
    if "<refermsg>" not in content:
        return False, content, ""
    try:
        start = content.find("<msg")
        root = ET.fromstring(content[start:] if start >= 0 else content)
    except ET.ParseError:
        return False, content, ""
    appmsg = root.find(".//appmsg")
    if appmsg is None or appmsg.findtext("type") != "57":
        return False, content, ""
    refer = appmsg.find("refermsg")
    if refer is None:
        return False, content, ""
    new_text = (appmsg.findtext("title") or "").strip()
    quotes_this_account = bool(
        own_wxid and (refer.findtext("fromusr") or "").strip() == own_wxid
    )
    return (quotes_this_account, new_text,
            (refer.findtext("content") or "").strip() if quotes_this_account else "")
