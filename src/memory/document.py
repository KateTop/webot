"""Stable memory identities and evidence, with an editable Markdown projection."""
import datetime as dt
import hashlib
import json
import re
import uuid

SECTIONS = ("每个人", "群里的事", "我说过的立场", "观察中")
NO_UPDATE = "NO_UPDATE"
MAX_CHARS = 12000


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(observation=False):
    return ("o" if observation else "m") + uuid.uuid4().hex[:16]


def render(entries):
    parts = []
    for section in SECTIONS:
        parts.append(f"【{section}】")
        for entry in entries:
            if entry["section"] != section:
                continue
            meta = {k: v for k, v in entry.items() if k not in ("id", "text", "section")}
            text = entry["text"].replace("\n", " ").replace("<!--", "〈").replace("-->", "〉")
            parts.append(f"- [{entry['id']}] {text} <!-- {json.dumps(meta, ensure_ascii=False)} -->")
        parts.append("")
    return "\n".join(parts)


def parse(text):
    if not text.strip():
        return []
    found, section, entries = set(), None, []
    for line in text.splitlines():
        if line.strip() in [f"【{s}】" for s in SECTIONS]:
            section = line.strip()[1:-1]
            found.add(section)
        elif line.strip():
            match = re.fullmatch(r"- \[([mo][a-f0-9]{16})\] (.*?) <!-- (\{.*\}) -->", line)
            if not match or section is None:
                raise ValueError("记忆需先迁移为带稳定 ID 的四块结构；请保留条目末尾元数据")
            item = json.loads(match[3])
            if not isinstance(item.get("subject_id", ""),str) or not isinstance(item.get("dates", []),list) or not isinstance(item.get("evidence", []),list):
                raise ValueError("记忆元数据类型无效")
            for date in item.get("dates", []):
                if not isinstance(date,str) or dt.date.fromisoformat(date).isoformat() != date:
                    raise ValueError("证据日期必须为 ISO 日期")
            item.update(id=match[1], text=match[2], section=section)
            entries.append(item)
    if found != set(SECTIONS) or len({e["id"] for e in entries}) != len(entries):
        raise ValueError("记忆结构缺失或条目 ID 重复")
    return entries


def migration_preview(text):
    """Preserve legacy wording, make no AI guesses about owners or facts."""
    try:
        parse(text)
        return text or render([])
    except ValueError:
        section, entries = "群里的事", []
        for line in text.splitlines():
            line = line.strip()
            if line in [f"【{s}】" for s in SECTIONS]:
                section = line[1:-1]
            elif line and line not in ("（暂无）", "暂无"):
                entries.append(dict(id=new_id(section == "观察中"), section=section, text=line,
                    subject_id="", topic="迁移条目", dates=[], evidence=[]))
        return render(entries)


def apply_write(old, proposal, messages, blocked):
    """Model proposes changes; code owns identifiers and evidence dates."""
    if proposal.strip() == NO_UPDATE:
        return old
    data = json.loads(proposal)
    if not isinstance(data, dict) or not isinstance(data.get("changes"), list):
        raise ValueError("写入必须返回 NO_UPDATE 或 changes 对象")
    entries = [dict(e) for e in old]
    by_id = {e["id"]: e for e in entries}
    evidence = {str(m["message_id"]): m for m in messages}
    if len(data["changes"]) > 6:
        raise ValueError("每批最多六项变更")
    touched = set()
    for change in data["changes"]:
        target = change.get("id")
        if target and target not in by_id:
            raise ValueError("未知记忆 ID")
        if target in touched and target is not None:
            raise ValueError("重复修改同一条目")
        touched.add(target)
        refs = change.get("evidence_message_ids", [])
        if not isinstance(refs, list) or not refs or any(str(r) not in evidence for r in refs):
            raise ValueError("证据必须来自当前批次")
        section = change.get("section")
        subject = str(change.get("subject_id", ""))
        topic = str(change.get("topic", "")).strip()
        text = change.get("text", "")
        if section not in SECTIONS or not isinstance(text, str) or not 1 <= len(text) <= 1000 or not topic:
            raise ValueError("记忆内容或归属无效")
        if section in ("每个人", "观察中") and not subject:
            raise ValueError("人物观察必须绑定稳定成员 ID")
        if not target and section == "我说过的立场" and any(
            evidence[str(r)].get("sender_id") != "__assistant__" or
            evidence[str(r)].get("action") not in ("表态", "反对") for r in refs):
            raise ValueError("立场必须来自确认发出的表态或反对")
        if subject and section in ("每个人", "观察中") and subject not in {str(m.get("sender_id", "")) for m in messages} and subject not in {e.get("subject_id") for e in old}:
            raise ValueError("未知人物身份，不能猜测归属")
        if target:
            entry = by_id[target]
            if entry.get("subject_id", "") != subject:
                raise ValueError("不能改变条目的身份归属")
        else:
            entry = dict(id=new_id(section == "观察中"), dates=[], evidence=[])
            entries.append(entry)
        if any(b["subject_id"] == subject and (b["topic"] in topic or b["topic"] in text) for b in blocked):
            raise ValueError("该主题已被要求不再记录")
        dates = set(entry.get("dates", []))
        dates.update(dt.datetime.fromtimestamp(evidence[str(r)]["timestamp"]).date().isoformat() for r in refs)
        if section == "每个人" and (entry.get("section") == "观察中" or change.get("kind") == "impression") and len(dates) < 3:
            raise ValueError("观察升级需要三个不同日期的证据")
        entry.update(section=section, text=text, subject_id=subject, topic=topic,
            dates=sorted(dates), evidence=sorted(set(entry.get("evidence", [])) | set(map(str, refs))))
    old_size = sum(len(e["text"]) for e in old)
    new_size = sum(len(e["text"]) for e in entries)
    if old_size >= 50 and new_size < old_size * 0.6:
        raise ValueError("普通写入异常缩水，请使用沉淀或授权遗忘操作")
    from src.conversation_policy import load_policy
    if len(render(entries)) > MAX_CHARS or sum(len(e["text"]) for e in entries) > load_policy()["memory_body_max_chars"]:
        raise ValueError("记忆超过长度上限，请先沉淀或人工整理")
    return entries


def apply_operations(old, proposal, forget_subject=None):
    """Extractive operations only: replacements cannot invent new prose."""
    if proposal.strip() == NO_UPDATE:
        return old, []
    data = json.loads(proposal)
    operations = data.get("operations") if isinstance(data, dict) else None
    if not isinstance(operations, list) or len(operations) > 100:
        raise ValueError("操作格式无效")
    items = {e["id"]: dict(e) for e in old}
    audit = []
    for operation in operations:
        ids = operation.get("ids", [])
        action = operation.get("action")
        if not ids or len(set(ids)) != len(ids) or any(i not in items for i in ids):
            raise ValueError("未知或重复的条目 ID")
        sources = [items[i] for i in ids]
        if forget_subject is not None:
            if action != "delete" or any(e.get("subject_id") != forget_subject for e in sources):
                raise ValueError("只能删除本人所属条目")
        if action == "delete":
            for i in ids:
                del items[i]
        elif action in ("merge", "compress") and forget_subject is None:
            if len({(e["section"], e.get("subject_id")) for e in sources}) != 1:
                raise ValueError("不能合并不同人物或不同区域")
            excerpts = operation.get("excerpts", [])
            if not excerpts or any(not isinstance(t, str) or not t or not any(t in e["text"] for e in sources) for t in excerpts):
                raise ValueError("压缩只能选取原文片段")
            text = "；".join(excerpts)
            if len(text) > sum(len(e["text"]) for e in sources):
                raise ValueError("沉淀不能扩写")
            head = sources[0]
            head.update(text=text, dates=sorted({d for e in sources for d in e.get("dates", [])}),
                        evidence=sorted({d for e in sources for d in e.get("evidence", [])}))
            for i in ids[1:]:
                del items[i]
        else:
            raise ValueError("不允许的记忆操作")
        audit.append({"action": action, "ids": ids})
    return list(items.values()), audit
