export const AMAZON_VOC_API='/api/amazon-voc';
export const AMAZON_VOC_STATUS={
  UPLOADED:'uploaded',CLEANED:'cleaned',EXTRACTING:'extracting',REVIEWING:'reviewing',
  NORMALIZING:'normalizing',SEGMENTING:'segmenting',JOURNEY:'journey',COMPILING:'compiling',
  COMPLETED:'completed',FAILED:'failed',CANCELLED:'cancelled',
};
export const AMAZON_VOC_ARTIFACTS=['html','excel','quality'];

export const amazonVocEndpoints={
  settings:()=>AMAZON_VOC_API+'/settings',
  projects:()=>AMAZON_VOC_API+'/projects',
  runs:projectId=>AMAZON_VOC_API+'/runs'+(projectId?'?project_id='+encodeURIComponent(projectId):''),
  run:runId=>AMAZON_VOC_API+'/runs/'+encodeURIComponent(runId),
  cancel:runId=>AMAZON_VOC_API+'/runs/'+encodeURIComponent(runId)+'/cancel',
  retry:runId=>AMAZON_VOC_API+'/runs/'+encodeURIComponent(runId)+'/retry',
  events:runId=>AMAZON_VOC_API+'/runs/'+encodeURIComponent(runId)+'/events',
  correctEvent:(runId,eventId)=>AMAZON_VOC_API+'/runs/'+encodeURIComponent(runId)+'/events/'+encodeURIComponent(eventId),
  artifact:(runId,kind)=>AMAZON_VOC_API+'/runs/'+encodeURIComponent(runId)+'/artifacts/'+encodeURIComponent(kind),
  projectRuns:projectId=>AMAZON_VOC_API+'/projects/'+encodeURIComponent(projectId)+'/runs',
};

export function buildAmazonVocRunForm({file,model,credentialId}={}){
  if(!file)throw new Error('请选择评论 Excel 文件');
  const name=String(file.name||'').toLowerCase();
  if(!name.endsWith('.xlsx')&&!name.endsWith('.xlsm'))throw new Error('仅支持 .xlsx 或 .xlsm 评论文件');
  const form=new FormData();
  form.append('reviews',file,file.name);
  for(const [key,value] of Object.entries({model,credential_id:credentialId})){
    if(value!=null&&String(value).trim())form.append(key,String(value).trim());
  }
  return form;
}

export function isAmazonVocActive(status){
  return [AMAZON_VOC_STATUS.UPLOADED,AMAZON_VOC_STATUS.CLEANED,AMAZON_VOC_STATUS.EXTRACTING,
    AMAZON_VOC_STATUS.REVIEWING,AMAZON_VOC_STATUS.NORMALIZING,AMAZON_VOC_STATUS.SEGMENTING,
    AMAZON_VOC_STATUS.JOURNEY,AMAZON_VOC_STATUS.COMPILING].includes(status);
}

export function hasAmazonVocArtifacts(run){
  return run?.status==='completed'&&AMAZON_VOC_ARTIFACTS.slice(0,2).every(kind=>run.artifacts?.includes(kind));
}
