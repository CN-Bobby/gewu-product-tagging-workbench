"""Versioned product-tagging Skill loader, deterministic router and runtime compiler."""
import copy
import hashlib
import json
from .db import ROOT

PACKAGE=ROOT/'skills'/'product-tagging'
TOPICS={'text':'text-extraction.md','attributes':'attributes.md','normalization':'normalization.md','tools':'tools.md','output':'output.md'}

CATEGORY_KEYWORDS={
    'apparel':['服装','衣服','上衣','裤','裙','泳装','泳衣','swim','dress','shirt','apparel'],
    'drinkware':['水杯','杯子','饮具','保温杯','吸管杯','tumbler','bottle','cup','mug'],
    'electronics':['3c','数码','电子','音箱','麦克风','手机','耳机','充电','speaker','microphone','phone','electronic'],
    'home_decor':['家居装饰','摆件','挂饰','花瓶','灯饰','装饰画','decor'],
    'storage':['收纳','置物','储物','整理箱','抽屉','organizer','storage'],
    'tools':['工具','扳手','电钻','螺丝','夹具','刀具','tool'],
    'craft_components':['手工配件','首饰配件','吊坠','串珠','字母珠','charm','bead','jewelry making'],
    'toys_activity_kits':['玩具','儿童化妆','过家家','活动套装','toy makeup','dress-up','activity kit'],
    'gifts':['礼品','礼物','赠礼','gift'],
}
DIMENSION_KEYWORDS={
    'function':['功能','用途','性能','防水','保温','兼容','承重','安全','function'],
    'structure':['结构','部件','组件','连接','安装','形状','轮廓','开合','肩带','领口','structure'],
    'material':['材质','材料','面料','material','fabric'],
    'theme':['主题','风格','角色','节日','theme','style'],
    'appearance':['外观','颜色','配色','主色','印花','图案','纹理','表面','工艺','cmf','color','pattern','print','texture','finish'],
}
FORM_KEYWORDS={
    'multi_component':['多组件','配件','随附','组成件','多件套','2件','3件','piece','accessor'],
    'multi_variant':['多变体','多色可选','颜色缩略图','色卡','variant','color option'],
    'mounting_system':['支架','固定','安装系统','吸盘','夹持','磁吸','mount','holder'],
    'homogeneous_collection':['同质集合','合集','混装','大量','多枚','吊坠','串珠','collection','assorted'],
    'heterogeneous_kit':['异质套装','活动套装','工具套装','化妆套装','kit','set includes'],
    'electronic_system':['电子系统','主机','麦克风','音箱','输入设备','输出设备','speaker','microphone'],
    'gift_set':['礼品套装','礼盒','赠送','gift set','gift box'],
}
CATEGORY_ALIASES={'服装':'apparel','水杯':'drinkware','饮具':'drinkware','3C数码':'electronics','数码':'electronics','家居装饰':'home_decor','收纳用品':'storage','收纳':'storage','工具类':'tools','工具':'tools','手工配件':'craft_components','礼品':'gifts','礼品套装':'gifts','玩具':'toys_activity_kits','活动套装':'toys_activity_kits'}
DIMENSION_ALIASES={'功能':'function','结构':'structure','材质':'material','结构/材质':'structure','主题':'theme','外观':'appearance','形态':'appearance','颜色':'appearance','配色':'appearance','印花':'appearance','图案':'appearance','纹理':'appearance','表面处理':'appearance','工艺':'appearance','CMF':'appearance'}
FORM_ALIASES={'多组件':'multi_component','多变体展示':'multi_variant','安装系统':'mounting_system','同质集合':'homogeneous_collection','异质套装':'heterogeneous_kit','电子系统':'electronic_system','礼品套装':'gift_set'}

def manifest():
    value=json.loads((PACKAGE/'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(value.get('modules'),dict):raise ValueError('Skill manifest 缺少 modules')
    return value

def read(topic='core'):
    meta=manifest()
    if topic=='core':path=PACKAGE/'SKILL.md'
    elif topic in TOPICS:path=PACKAGE/'references'/TOPICS[topic]
    elif topic in meta['modules']:path=PACKAGE/meta['modules'][topic]['path']
    else:raise ValueError('未知规则主题或模块')
    return path.read_text(encoding='utf-8')

def _values(value):
    if value is None:return []
    if isinstance(value,str):return [value]
    if isinstance(value,list):return [x for x in value if isinstance(x,str)]
    return []

def _normalized(value,aliases):return list(dict.fromkeys(aliases.get(x,x) for x in _values(value)))

def _match(text,mapping):
    folded=text.casefold()
    return [key for key,words in mapping.items() if any(word.casefold() in folded for word in words)]

def route(definition=None,mode='execution',context=None):
    """Select the smallest relevant module set; explicit routing fields override keyword hints."""
    definition=definition or {};context=context or {}
    text=json.dumps({k:v for k,v in {**context,**definition}.items() if k not in ('skill','runtime_profile')},ensure_ascii=False,sort_keys=True)
    categories=_normalized(definition.get('product_category') or context.get('product_category'),CATEGORY_ALIASES) or _match(text,CATEGORY_KEYWORDS)
    dimensions=_normalized(definition.get('dimension') or context.get('dimension'),DIMENSION_ALIASES) or _match(text,DIMENSION_KEYWORDS)
    forms=_normalized(definition.get('product_forms') or context.get('product_forms'),FORM_ALIASES) or _match(text,FORM_KEYWORDS)
    if definition.get('source') in ('image','both') and not dimensions:dimensions=['appearance']
    meta=manifest();selected=[]
    for module_id,spec in meta['modules'].items():
        if spec.get('always') or mode in spec.get('modes',[]) or set(categories)&set(spec.get('categories',[])) or set(dimensions)&set(spec.get('dimensions',[])) or set(forms)&set(spec.get('forms',[])):
            selected.append(module_id)
    return {'mode':mode,'categories':categories,'dimensions':dimensions,'forms':forms,'module_ids':selected}

def freeze():
    meta=manifest()
    content={'core':read('core'),**{topic:read(topic) for topic in TOPICS}}
    content.update({module_id:read(module_id) for module_id in meta['modules']})
    module_hashes={key:hashlib.sha256(value.encode()).hexdigest() for key,value in content.items()}
    digest=hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'name':meta['name'],'version':meta['version'],'sha256':digest,'content':content,'module_hashes':module_hashes,'manifest':copy.deepcopy(meta)}

def prompt(bundle,topics):
    missing=[t for t in topics if t not in bundle.get('content',{})]
    if missing:raise ValueError('冻结规则缺少模块：'+','.join(missing))
    return '\n\n'.join(bundle['content'][t] for t in topics)

def compile_runtime_profile(definition,mode='execution',bundle=None,context=None):
    bundle=bundle or freeze();selection=route(definition,mode,context)
    judgment_value=definition.get('judgment') if isinstance(definition.get('judgment'),dict) else judgment(definition)
    profile={
        'profile_version':'runtime-profile-v1','label':definition.get('name'),'key':definition.get('key'),'mode':mode,
        'target_scope':definition.get('target_scope','primary_product'),'dimensions':selection['dimensions'],
        'categories':selection['categories'],'product_forms':selection['forms'],
        'include':judgment_value.get('include'),'exclude':judgment_value.get('exclude'),
        'allowed_values':definition.get('options',[]),'result_shape':definition.get('result_shape'),
        'cardinality':definition.get('cardinality','auto'),'unknown':judgment_value.get('unknown'),
        'conflict':judgment_value.get('conflict'),'source':definition.get('source'),'fields':definition.get('fields',judgment_value.get('fields',[])),
        'input_policy':definition.get('input_policy','single_main_image_v1'),
        'expansion_policy':definition.get('expansion_policy','end_of_task_suggestion'),
        'visual_standard':definition.get('visual_standard'),
        'module_ids':selection['module_ids'],
        'module_hashes':{mid:bundle.get('module_hashes',{}).get(mid) for mid in selection['module_ids']},
        'examples':judgment_value.get('examples',[]),
    }
    raw=json.dumps(profile,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    profile['sha256']=hashlib.sha256(raw.encode()).hexdigest()
    return profile

def runtime_prompt(definition,bundle=None,mode='execution'):
    bundle=bundle or definition.get('skill') or freeze()
    profile=definition.get('runtime_profile')
    if not isinstance(profile,dict) or profile.get('mode')!=mode:profile=compile_runtime_profile(definition,mode,bundle)
    module_ids=[mid for mid in profile.get('module_ids',[]) if mid in bundle.get('content',{})]
    if not module_ids:return prompt(bundle,['core','attributes','normalization','output'])
    legacy=['attributes','normalization','output'] if mode in ('execution','repair') else []
    return prompt(bundle,module_ids+legacy)+'\n\n# 本次命中及排除规则（紧凑运行配置）\n'+json.dumps(profile,ensure_ascii=False,sort_keys=True)

def prepare_definition(definition,mode='execution'):
    value=copy.deepcopy(definition);bundle=freeze();value['skill']=bundle
    value['runtime_profile']=compile_runtime_profile(value,mode,bundle)
    return value

def judgment(definition):
    supplied=definition.get('judgment',{})
    if not isinstance(supplied,dict):raise ValueError('判断规则必须是对象')
    target=definition.get('definition') or definition.get('name') or '待明确的标签目标'
    default_fields=['price'] if '价格' in target or 'price' in definition.get('key','') else ['sku','parameters','title']
    defaults={'target':target,'fields':definition.get('fields',default_fields),
              'include':target,'exclude':'无充分依据不得判定命中或否定',
              'synonyms':{},'conflict':'保留冲突证据，标准值未知，等待确认',
              'unknown':'保留原文及原因，使用 null，不猜测','examples':[]}
    for key,value in supplied.items():
        if key not in defaults:raise ValueError('未知判断规则字段：'+key)
        if not isinstance(value,type(defaults[key])):raise ValueError('判断规则字段类型无效：'+key)
    defaults.update(supplied)
    if not all(isinstance(x,str) for x in defaults['fields']):raise ValueError('所需字段必须为字符串列表')
    return defaults
