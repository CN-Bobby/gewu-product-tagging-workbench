// Turn activity presentation: how one turn's tool execution is shown as a single card.
//
// This is a projection, not a data model. The event store keeps every tool event in full and the
// reducer keeps one activity object per turn; everything here turns that activity into copy and
// timeline rows, so the component stays dumb and the projection can be tested without a DOM.
//
// Pure module on purpose — no React import.

export const ACTIVITY_STATUS={RUNNING:'running',DONE:'done',CANCELLED:'cancelled',FAILED:'failed'};

// Hand-written action names for the tools this workbench actually exposes. A tool that is not listed
// is formatted generically instead of being shown as snake_case.
const TOOL_DISPLAY={
  read_worksheet:'读取工作表', read_tasks:'检查任务', read_fields:'读取字段',
  read_product_source:'读取商品原始数据', read_profile:'读取项目配置',
  route_tagging_skill:'选择打标技能', read_tagging_rules:'读取标签规则',
  query_scope:'筛选商品范围', propose_changes:'提交标签提案',
  record_product_tags:'保存打标结果', get_products:'读取商品数据',
  inspect_representative_images:'查看代表样本', view_product_image:'查看商品图片',
  view_chat_image:'查看对话图片', get_product_images:'获取商品图片',
  research_knowledge:'检索外部资料', read_knowledge:'读取已确认资料',
};

// A small verb/noun vocabulary is enough for the generic case: "get_product_images" reads as
// 获取商品图片 without pretending we know what an unmapped tool really does.
const VERBS={read:'读取',get:'获取',fetch:'获取',list:'列出',load:'加载',view:'查看',
  inspect:'检查',check:'检查',query:'查询',search:'搜索',scan:'扫描',propose:'提案',
  record:'记录',save:'保存',write:'写入',update:'更新',create:'创建',delete:'删除',
  route:'选择',resolve:'解析',run:'运行',apply:'应用',expand:'展开',repair:'修复',extract:'提取'};
const NOUNS={worksheet:'工作表',worksheets:'工作表',sheet:'工作表',task:'任务',tasks:'任务',
  profile:'项目配置',field:'字段',fields:'字段',scope:'范围',product:'商品',products:'商品',
  image:'图片',images:'图片',photo:'图片',photos:'图片',tag:'标签',tags:'标签',rule:'规则',
  rules:'规则',skill:'技能',skills:'技能',knowledge:'资料',row:'行',rows:'行',
  metadata:'信息',meta:'信息',config:'配置',setting:'设置',settings:'设置',
  credential:'凭据',credentials:'凭据',session:'会话',dataset:'数据集',draft:'草稿',
  proposal:'提案',source:'原始数据',record:'记录',records:'记录'};

// The original name is part of the evidence, so the raw tool name is always available; only the
// headline goes through this mapping.
export function displayName(tool){
  const raw=typeof tool==='string'?tool.trim():'';
  if(!raw)return '执行操作';
  if(TOOL_DISPLAY[raw])return TOOL_DISPLAY[raw];
  const tail=raw.includes('__')?raw.split('__').pop():raw;
  if(TOOL_DISPLAY[tail])return TOOL_DISPLAY[tail];
  const parts=tail.split(/[_\-\s]+/).filter(Boolean);
  const verb=VERBS[String(parts[0]||'').toLowerCase()]||null;
  const nouns=parts.slice(verb?1:0).map(part=>NOUNS[part.toLowerCase()]||null);
  if(verb&&nouns.length&&nouns.every(Boolean))return verb+nouns.join('');
  if(verb)return verb+'操作';
  return '执行 '+tail;
}

function toMillis(value){
  if(typeof value!=='string'||!value)return null;
  const parsed=Date.parse(value);
  return Number.isFinite(parsed)?parsed:null;
}

// Durations come from the event log's own timestamps, so a replayed history reports the same numbers
// as the live turn did.
export function secondsBetween(from,to){
  const start=toMillis(from),end=toMillis(to);
  if(start===null||end===null||end<start)return null;
  return Math.round((end-start)/100)/10;
}

export function formatSeconds(value){
  if(value===null||value===undefined)return null;
  return (Number.isInteger(value)?String(value):value.toFixed(1))+' 秒';
}

export function shortDuration(value){
  if(value===null||value===undefined)return '';
  return (value<10?value.toFixed(1):String(Math.round(value)))+'s';
}

export const GLYPH={completed:'✓',failed:'⚠',pending:'◌'};

// One activity, as the card renders it. Everything the component needs is derived here so the
// component never decides what a status means.
//
// ``expand`` is the caller's default: the preference from §11 OR the user's own toggle for this card.
// While a turn is running the card never expands, so a long turn cannot push the reply off screen.
export function activityView(activity,{expand=false}={}){
  if(!activity)return null;
  const running=activity.status===ACTIVITY_STATUS.RUNNING;
  const lines=(activity.tools||[]).map(tool=>({
    id:tool.id, tool:tool.tool, label:displayName(tool.tool), status:tool.status,
    glyph:GLYPH[tool.status]||GLYPH.pending, duration:shortDuration(tool.duration),
    note:tool.status==='failed'?(tool.retry?'自动重试':'未完成'):null,
    error:tool.status==='failed'?(tool.error||null):null,
  }));
  const retryNote=activity.retryCount?` · ${activity.retryCount} 次自动重试`:'';
  let headline,detail=null;
  if(running){
    headline=activity.cancelling?'正在取消…':'Agent 正在处理';
    if(!activity.cancelling){
      if(activity.waiting)detail='等待你的回答…';
      else if(activity.currentTool)detail='正在'+displayName(activity.currentTool)+'…';
      else if(activity.resumeHint)detail='Agent 正在继续处理…';
    }
  }else if(activity.status===ACTIVITY_STATUS.DONE){
    headline='已完成 · '+activity.completedCount+' 个操作';
    const seconds=formatSeconds(activity.seconds);
    if(seconds)headline+=' · '+seconds;
    headline+=retryNote;
  }else if(activity.status===ACTIVITY_STATUS.CANCELLED){
    headline='已取消 · '+activity.completedCount+' 个操作'+retryNote;
  }else{
    headline='执行失败 · '+activity.completedCount+' 个操作'+retryNote;
  }
  const counts=running?('已完成 '+activity.completedCount+' 个操作'+retryNote):headline;
  // A running card is always compact: the timeline is only ever shown once the turn is over.
  const expanded=!running&&expand===true;
  return {
    status:activity.status, running, headline, detail, counts, expanded, collapsed:!expanded,
    expandLabel:running?'查看过程':'查看执行过程',
    completedCount:activity.completedCount, warningCount:activity.warningCount,
    retryCount:activity.retryCount, toolCount:(activity.tools||[]).length,
    hasFailures:lines.some(line=>line.status==='failed'),
    lines,
  };
}

// A turn with nothing to show stays out of the transcript; a turn that is still running keeps its
// placeholder so the reply below it never moves.
export function activityVisible(activity){
  if(!activity)return false;
  return activity.status===ACTIVITY_STATUS.RUNNING||(activity.tools||[]).length>0;
}
