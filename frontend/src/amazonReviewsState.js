export const AMAZON_VOC_FROM_REVIEWS_API='/api/amazon-voc/from-amazon-reviews';

export function amazonReviewsVocHandoffEndpoint(jobId){
  return `${AMAZON_VOC_FROM_REVIEWS_API}/${encodeURIComponent(jobId)}`;
}

export function buildAmazonReviewsVocHandoffRequest(jobId){
  return {path:amazonReviewsVocHandoffEndpoint(jobId),body:{confirmed_reviewed:true}};
}

export function isAmazonReviewsVocEligible(job){
  return Boolean(job&&['completed','completed_with_errors','partial'].includes(job.status)
    &&job.can_download&&Number(job.review_count)>0
    &&(!job.voc_handoff||job.voc_handoff.status==='failed'));
}

export function isAmazonReviewsVocPartial(job){
  return ['completed_with_errors','partial'].includes(job?.status);
}

export function canStartAmazonReviewsVoc(job,pendingJobId=''){
  return isAmazonReviewsVocEligible(job)&&pendingJobId!==job.id;
}

export function amazonVocRunHash(runId){
  return `amazon-voc/run/${encodeURIComponent(runId)}`;
}

export function amazonReviewsWorkflowCopy({translateEnabled=true,workflowMode='reviews_only'}={}){
 const translates=Boolean(translateEnabled);
 const combined=workflowMode==='combined';
 return {
  heading:translates?'批量拉取 · 逐条翻译 · 表格交付':'批量拉取 · 原文导出 · 表格交付',
  task:combined
   ?`提交后自动拉取评论${translates?'并翻译标题和正文':''}，完成后启动 VOC。`
   :`提交后自动拉取评论${translates?'并翻译标题和正文':''}。`,
  introduction:translates?'查看采集与翻译进度，并下载中英对照结果。':'查看评论采集进度，并下载保留原文的结果。',
  footer:translates
   ?`结果包含英文原文、逐条中文译文、来源商品与按星级映射的情感信息${combined?'；评论导出完成后会自动启动 VOC':''}；可下载含「全部评论 / 商品评论统计 / 统计汇总」三个工作表的 Excel。`
   :`结果保留英文原文，不生成中文译文；包含来源商品与按星级映射的情感信息${combined?'；评论导出完成后会自动启动 VOC':''}；可下载含「全部评论 / 商品评论统计 / 统计汇总」三个工作表的 Excel。`,
 };
}

export function parseAmazonVocRoute(hash){
  const parts=String(hash||'').replace(/^#\/?/,'').split('/').filter(Boolean);
  if(parts[0]!=='amazon-voc')return null;
  let runId='';
  if(parts[1]==='run'&&parts[2]){
    try{runId=decodeURIComponent(parts[2])}catch{runId=parts[2]}
  }
  return {page:'amazon-voc',runId};
}
