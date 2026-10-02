export const AMAZON_VOC_FROM_REVIEWS_API='/api/amazon-voc/from-amazon-reviews';

export function amazonReviewsVocHandoffEndpoint(jobId){
  return `${AMAZON_VOC_FROM_REVIEWS_API}/${encodeURIComponent(jobId)}`;
}

export function buildAmazonReviewsVocHandoffRequest(jobId){
  return {path:amazonReviewsVocHandoffEndpoint(jobId),body:{confirmed_reviewed:true}};
}

export function isAmazonReviewsVocEligible(job){
  return Boolean(job&&['completed','completed_with_errors','partial'].includes(job.status)
    &&job.can_download&&Number(job.review_count)>0&&!job.voc_handoff);
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

export function parseAmazonVocRoute(hash){
  const parts=String(hash||'').replace(/^#\/?/,'').split('/').filter(Boolean);
  if(parts[0]!=='amazon-voc')return null;
  let runId='';
  if(parts[1]==='run'&&parts[2]){
    try{runId=decodeURIComponent(parts[2])}catch{runId=parts[2]}
  }
  return {page:'amazon-voc',runId};
}
