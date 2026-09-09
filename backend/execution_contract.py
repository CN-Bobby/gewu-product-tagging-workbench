"""Freeze prompt text at confirmation; upgrades must not silently change pending inputs."""
import hashlib
FILL_PREFIX='\n你是批量填表工具。按以下冻结定义返回每个商品一项 JSON results，不能创建列或批准提案。定义：'
TEXT_PREFIX='\n这是上传后的文字底稿提取，不创建标签。返回JSON {"results":[{"product_id":"...","facts":[{"attribute":"中文属性","value":"中文标准值或null","raw_value":"原始值","state":"observed或conflict或unknown","reason":"简短原因","evidence":[{"field":"输入text中的字段名","quote":"逐字原文片段"}]}]}]}。每商品一项，所有证据片段必须逐字存在于对应字段。'


def snapshot():
    from .batch import VISION_RULES
    return {'version':1,'vision_rules':VISION_RULES,'fill_prefix':FILL_PREFIX,'text_prefix':TEXT_PREFIX}


def require_contract(spec):
    value=spec.get('execution_contract')
    if not value or any(not isinstance(value.get(k),str) for k in ('vision_rules','fill_prefix','text_prefix')):
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
    if any(c['definition'].get('source') in ('image','both') for c in spec.get('columns',[])):keys.append('vision_model')
    for key in keys:
        result[key]=model_identity(spec.get(key))
    return result


def check_models(spec):
    frozen=require_contract(spec)
    if frozen.get('models')!=model_identities(spec):
        raise ValueError('模型服务配置与确认时不同，请生成新的执行提案；原任务和结果保留。')
