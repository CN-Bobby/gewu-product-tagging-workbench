"""Small, audited text transforms. No eval, code execution, or model inference."""
import re

def validate(parser):
    if not isinstance(parser,dict) or parser.get('operation') not in ('size_age','size_value','split','mapping'):raise ValueError('文字规则operation需为size_age/size_value/split/mapping')
    if not isinstance(parser.get('field'),str):raise ValueError('文字规则需要来源字段')
    if parser['operation']=='size_age':
        if not isinstance(parser.get('bins'),list) or not parser['bins']:raise ValueError('年龄分档需明确bins')
        last=None
        for i,b in enumerate(parser['bins']):
            low,high=b.get('min'),b.get('max')
            if not isinstance(b.get('value'),str) or any(v is not None and not isinstance(v,(int,float)) for v in (low,high)):raise ValueError('年龄区间无效')
            if low is not None and high is not None and low>=high:raise ValueError('年龄区间上下界无效')
            if i and (last is None or low is None or low<last):raise ValueError('年龄区间不能重叠');
            last=high
    return parser

def extract_size(text):
    match=re.search(r'(?:Size|尺码)\s*[:：]\s*([^|;；,，]+)',text,re.I)
    part=match[1].strip() if match else text.strip()
    size=re.fullmatch(r'(\d{1,2})(?:\s*[-–~至]\s*(\d{1,2}))?\s*(?:Years?|Y|T|岁)?',part,re.I)
    return (int(size[2] or size[1]),part) if size else (None,part)

def apply(parser,fields):
    raw=fields.get(parser['field']);value=None;reason=''
    if isinstance(raw,(dict,list)):return {'normalized_value':None,'raw_value':raw,'evidence':'来源包含多个值，请确认处理口径'}
    text=str(raw or '').strip();op=parser['operation']
    if op=='mapping':value=parser.get('mapping',{}).get(text,parser.get('unknown'))
    elif op=='split':
        parts=text.split(parser.get('separator','|'));index=parser.get('index',0)
        if isinstance(index,int) and -len(parts)<=index<len(parts):value=parts[index].strip()
    else:
        age,part=extract_size(text)
        if age is not None:
            value=str(age) if op=='size_value' else next((b['value'] for b in parser['bins'] if (b.get('min') is None or age>=b['min']) and (b.get('max') is None or age<b['max'])),None)
            reason=f'提取{part}，按已确认规则取上界{age}'
        else:value=parser.get('unknown');reason='字母码、缺失或复杂尺码，使用已确认未知策略'
    return {'normalized_value':value,'raw_value':raw,'evidence':reason or '按已确认文字规则计算','method':'text_rule'}
