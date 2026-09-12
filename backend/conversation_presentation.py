"""Turn raw Reasonix output into a user-facing view without losing audit text."""
import re

OPERATION_PATTERNS = (
    re.compile(r'^\s*(我先|先来|现在|接下来)(读取|读一下|调用|查询|检查|看一下|看一批|核实|获取|提交|重试)'),
    re.compile(r'^\s*(参数|调用).{0,30}(错误|失败|重试|重新提交)'),
    re.compile(r'\b(?:mcp__|use_capability|toolCallId|session/request_permission)\b', re.I),
    re.compile(r'\b[0-9a-f]{24,64}\b', re.I),
)

def _paragraphs(text):
    return [part.strip() for part in re.split(r'\n\s*\n', text or '') if part.strip()]

def _is_operation(part):
    first=part.splitlines()[0].strip()
    return any(pattern.search(first) for pattern in OPERATION_PATTERNS)

def build(text,status='completed',error_code=None,error_message=None,technical=None,events=None):
    """Preserve raw text on Message; return a separate compact display contract."""
    business=[];operations=[]
    for part in _paragraphs(text):
        if _is_operation(part):
            match=re.search(r'[。！？]\s*',part)
            if match and part[match.end():].strip():
                operations.append(part[:match.end()].strip());business.append(part[match.end():].strip())
            else:operations.append(part)
        else:business.append(part)
    discussion='\n\n'.join(business).strip()
    if status=='failed' or error_code:
        heading='需要处理'
        summary=error_message or 'Reasonix 会话异常结束，未执行后续操作。'
    elif re.search(r'(^|\n)\s*(执行结果|已完成|已提交|已确认)',discussion):
        heading='执行结果';summary=''
    else:
        heading='当前判断';summary=''
    # The web card owns its title. Keep the complete discussion without
    # repeating an identical Markdown title immediately below it.
    if discussion:
        discussion=re.sub(
            rf'^\s*(?:#{{1,6}}\s*|\*\*)?{re.escape(heading)}(?:\*\*)?\s*',
            '',discussion,count=1,
        )
    if not summary and discussion:
        clean=re.sub(r'^#{1,6}\s*[^\n]+\n*','',discussion).strip()
        summary=(re.split(r'(?<=[。！？])\s*|\n',clean,maxsplit=1)[0] or clean)[:300]
    activity=[]; positions={}
    for event in events or []:
        if event.get('type')=='tool' and event.get('title'):
            item={'title':event['title'],'status':event.get('status')}
            key=event.get('toolCallId')
            if key and key in positions:activity[positions[key]]=item
            elif key:
                positions[key]=len(activity);activity.append(item)
            elif item not in activity:activity.append(item)
    return {
        'heading':heading,
        'summary':summary,
        'discussion':discussion,
        'activity':activity[-12:],
        'technical_notes':operations[-20:],
        'technical':technical or {},
    }
