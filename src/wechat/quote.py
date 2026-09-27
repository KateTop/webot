"""Parse WeChat app-message quotes without relying on display names."""

import xml.etree.ElementTree as ET


def parse_quote(content: str, own_wxid: str) -> tuple[bool, str, str]:
    """Return (quotes_this_account, new_text, quoted_text).

    WeChat type 49 / appmsg type 57 carries the original sender in
    refermsg/fromusr. A display name is not an identity and is never enough.
    """
    if not own_wxid or "<refermsg>" not in content:
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
    if refer is None or (refer.findtext("fromusr") or "").strip() != own_wxid:
        return False, content, ""
    return (True, (appmsg.findtext("title") or "").strip(),
            (refer.findtext("content") or "").strip())
