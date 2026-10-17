"""Freeze prompt text at confirmation; upgrades must not silently change pending inputs."""
import hashlib
FILL_PREFIX='\n你是批量填表工具。按以下冻结定义和result_shape返回每个商品一项 JSON results，不能创建列或批准提案。scalar_open/scalar_enum只返回单个normalized_value，不返回attributes；multi_enum返回字符串列表及逐属性证据，所有value=true的attribute集合必须与normalized_value集合完全相同；boolean返回true/false/null。不要在证据里写自我纠正、候选草稿或与value矛盾的结论。定义：'
FORMAT_REPAIR_PREFIX='\n上一次仅有下列商品未通过输出校验。只重写这些商品，保持观察事实不变；修正JSON结构、候选值、normalized_value与attributes正命中集合的一致性。不要重写已成功商品。校验错误：'
TEXT_PREFIX='\n这是上传后的文字底稿提取，不创建标签。返回JSON {"results":[{"product_id":"...","facts":[{"attribute":"中文属性","value":"中文标准值或null","raw_value":"原始值","state":"observed或conflict或unknown","reason":"简短原因","evidence":[{"field":"输入text中的字段名","quote":"逐字原文片段"}]}],"source_attributes":[{"source":"sku或parameters","attribute":"规范化后的属性名","value":"原文中的值","evidence":"包含属性名和值的连续原文片段"}]}]}。每商品一项。facts规则：field必须原样使用输入text对象中实际存在的键名，禁止写text[\'字段\']或自行缩写；quote必须是对应字段中非空、连续、逐字一致的原文片段，不能省略中间文字；state为conflict或unknown时value必须为null；没有可定位证据的事实不要输出。source_attributes规则：只解析输入text的sku和parameters两个来源；attribute优先沿用来源文本中明确出现的属性键名并保留原语言；同一来源同一键名跨商品必须一致，例如Color统一为Color、Size统一为Size、Fabric type统一为Fabric type；只统一大小写和空格等格式差异，不要把Color、Size、Fabric type翻译成中文；原文有明确同义键名时可归一到同一规范名；不得跨sku与parameters两个来源合并；value必须逐字保留源文本中的值，不得翻译、推断或补全；evidence必须是对应源字段里连续、逐字一致且同时包含属性名和值的片段；没有明确键值证据的片段不要输出。'


def snapshot():
    from .batch import VISION_RULES
    return {'version':5,'vision_rules':VISION_RULES,'fill_prefix':FILL_PREFIX,'text_prefix':TEXT_PREFIX,'format_repair_prefix':FORMAT_REPAIR_PREFIX}


def require_contract(spec):
    value=spec.get('execution_contract')
    if not value or any(not isinstance(value.get(k),str) for k in ('vision_rules','fill_prefix','text_prefix','format_repair_prefix')):
        raise ValueError('旧任务缺少完整冻结提示词，请从失败项生成新修复提案；文字底稿请重新安排提取。原结果保留。')
    return value


def model_identity(ref):
    from .vision import provider_config
    try:
        endpoint,_,model=provider_config(ref or '')
        return {'reference':ref,'model':model,'endpoint_sha256':hashlib.sha256(endpoint.encode()).hexdigest()}
    except ValueError:return None


def model_identities(spec):
    result={}
    keys=['text_model']
    if spec.get('requires_visual') or any(c['definition'].get('source') in ('image','both') for c in spec.get('columns',[])):keys.append('vision_model')
    for key in keys:
        result[key]=model_identity(spec.get(key))
    return result


def check_models(spec):
    frozen=require_contract(spec)
    if frozen.get('models')!=model_identities(spec):
        raise ValueError('模型服务配置与确认时不同，请生成新的执行提案；原任务和结果保留。')
