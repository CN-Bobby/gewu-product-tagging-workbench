"""One versioned, project-owned rule source for the coordinator and direct workers."""
import hashlib
import json
from .db import ROOT

PACKAGE=ROOT/'skills'/'product-tagging'
TOPICS={'text':'text-extraction.md','attributes':'attributes.md','normalization':'normalization.md','tools':'tools.md','output':'output.md'}

def read(topic='core'):
    if topic!='core' and topic not in TOPICS:raise ValueError('未知规则主题')
    path=PACKAGE/'SKILL.md' if topic=='core' else PACKAGE/'references'/TOPICS[topic]
    return path.read_text(encoding='utf-8')

def freeze():
    version=json.loads((PACKAGE/'manifest.json').read_text(encoding='utf-8'))['version']
    content={topic:read(topic) for topic in ['core',*TOPICS]}
    digest=hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'name':'product-tagging','version':version,'sha256':digest,'content':content}

def prompt(bundle,topics):
    return '\n\n'.join(bundle['content'][t] for t in topics)

def judgment(definition):
    supplied=definition.get('judgment',{})
    if not isinstance(supplied,dict):raise ValueError('判断规则必须是对象')
    default_fields=['price'] if '价格' in definition['definition'] or 'price' in definition.get('key','') else ['sku','parameters','title']
    defaults={'target':definition['definition'],'fields':definition.get('fields',default_fields),
              'include':definition['definition'],'exclude':'无充分依据不得判定命中或否定',
              'synonyms':{},'conflict':'保留冲突证据，标准值未知，等待确认',
              'unknown':'保留原文及原因，使用 null，不猜测','examples':[]}
    for key,value in supplied.items():
        if key not in defaults:raise ValueError('未知判断规则字段：'+key)
        if not isinstance(value,type(defaults[key])):raise ValueError('判断规则字段类型无效：'+key)
    defaults.update(supplied)
    if not all(isinstance(x,str) for x in defaults['fields']):raise ValueError('所需字段必须为字符串列表')
    return defaults
