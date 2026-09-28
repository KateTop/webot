"""Parse WeChat app-message quotes without relying on display names."""

from dataclasses import dataclass
from html import unescape
import re
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class QuoteInfo:
    valid: bool = False
    quotes_this_account: bool = False
    new_text: str = ""
    quoted_text: str = ""
    mentions_this_account: bool = False


def inspect_quote(content: str, own_wxid: str) -> QuoteInfo:
    """Inspect type-57 XML; keep quoted text separate from the new reply."""
    start = content.find("<msg")
    if start < 0 and "&lt;msg" in content:
        content = unescape(content)
        start = content.find("<msg")
    if start < 0:
        return QuoteInfo()
    try:
        root = ET.fromstring(content[start:])
    except ET.ParseError:
        return QuoteInfo()
    appmsg = root.find(".//appmsg")
    if appmsg is None or (appmsg.findtext("type") or "").strip() != "57":
        return QuoteInfo()
    refer = appmsg.find("refermsg")
    if refer is None:
        return QuoteInfo()

    new_text = (appmsg.findtext("title") or appmsg.findtext("des") or "").strip()
    fromusr = (refer.findtext("fromusr") or "").strip()
    quotes_this_account = bool(own_wxid and fromusr == own_wxid)

    # Inspect only the outer source; an @ inside quoted old text must not
    # trigger a reply. The visible @label can differ from the bot name.
    source = unescape(root.findtext("msgsource") or "")
    at_list = re.search(r"<atuserlist>(.*?)</atuserlist>", source, re.S)
    ids = re.split(r"[,;\s]+", at_list.group(1)) if at_list else []
    mentions_this_account = bool(own_wxid and own_wxid in ids)
    return QuoteInfo(
        valid=True,
        quotes_this_account=quotes_this_account,
        new_text=new_text,
        quoted_text=(refer.findtext("content") or "").strip()
                    if quotes_this_account else "",
        mentions_this_account=mentions_this_account,
    )


def parse_quote(content: str, own_wxid: str) -> tuple[bool, str, str]:
    """Backward-compatible (quotes_this_account, new_text, quoted_text)."""
    info = inspect_quote(content, own_wxid)
    return info.quotes_this_account, info.new_text if info.valid else content, info.quoted_text
