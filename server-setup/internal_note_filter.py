"""Narrow outbound rules for explicit background working notes.

This is a final delivery guard, not a classifier for social appropriateness.
Only known standalone meta statements are rejected. A normal direct message,
quoted discussion, or a message merely containing "quiet" is not a match.
"""
import re


_HEADING=re.compile(
    r'\A内部记录\s*(?:[（(]\s*(?:静默|仅内部|后台|不外发)\s*[）)]\s*)?'
    r'(?:\*\*|__)?[ \t]*(?:[:：]|$)',re.IGNORECASE)
_ROUND=r'(?:这一轮|这轮|本轮|这一次|这次|本次|此次)'
_THIRD_PARTY=r'(?:jg|他|用户)'
_END=r'(?=$|[，,。.!！；;：:）)])'
_STATEMENT=re.compile(
    r'\A'+_ROUND+r'\s*(?:(?:我|的)?(?:决定|结论|判定)\s*[:：]?\s*)?'
    r'(?:(?:(?:就|先|暂时|继续)\s*)*(?:保持静默|静默结束|安静结束|静默收尾|不外发|不发送消息|不发消息)'
    r'|(?:(?:就|先|暂时|决定)\s*)*(?:不再|暂不|先不|不|没有|没再)\s*(?:去)?打扰\s*'+_THIRD_PARTY+r'(?:了)?'
    r'|(?:(?:就|先|暂时)\s*)*(?:不再|暂不|不|无需)\s*(?:向\s*'+_THIRD_PARTY+r'\s*)?(?:外发|发送|联系)(?:消息|内容)?(?:了)?)'
    +_END,re.IGNORECASE)
_WAIT=re.compile(
    r'\A(?:(?:接下来|那|我)(?:就)?\s*)?(?:(?:先|继续|就先)\s*)?'
    r'(?:保持安静[，,]?\s*)?等(?:待)?\s*'
    +_THIRD_PARTY+r'\s*(?:自己|先)?\s*(?:开口|来叫(?:我)?|来找我|回复)'+_END,re.IGNORECASE)
_INTERNAL_ROUND=re.compile(r'\A'+_ROUND+r'\s*(?:的)?内部记录\s*(?:[:：]|$)',re.IGNORECASE)
_EXPLANATION=re.compile(r'(?:例如|比如|原话|引用|示例|例子|她说|他说|这句话|解释)[：:]\s*$')


def _lines(text):
    """Yield presentation-normalized lines, excluding quoted examples and code."""
    fence=None;previous=''
    for raw in text.splitlines():
        line=raw.strip()
        marker=re.match(r'(`{3,}|~{3,})',line)
        if marker:
            symbol=marker[1][0]
            if fence is None:fence=symbol
            elif fence==symbol:fence=None
            previous=''
            continue
        if fence:continue
        if not line:
            previous='';continue
        if line.startswith('>') or _EXPLANATION.search(previous):
            previous=line;continue
        previous=line
        line=re.sub(r'^(?:#{1,6}\s+|[-+*]\s+)','',line)
        line=re.sub(r'^(?:\*\*|__)','',line)
        line=re.sub(r'^[（(]\s*','',line)
        # Unwrap only emphasis, never quotation marks or inline-code delimiters.
        line=re.sub(r'(?:\*\*|__)(?=[:：，,。.!！；;）)]|$)','',line)
        yield line


def internal_note_reason(texts,*,background=False,origin=''):
    """Return a delivery rejection reason for known automatic meta statements."""
    if not background or origin!='send_tool':return None
    if isinstance(texts,str):texts=[texts]
    for text in texts or ():
        if not isinstance(text,str):continue
        for line in _lines(text):
            if any(rule.match(line)for rule in (_HEADING,_STATEMENT,_WAIT,_INTERNAL_ROUND)):
                return 'explicit_internal_note'
    return None
