export function imageDownloadSummary(rows=[],jobs=[]){
  const urls=rows.flatMap(row=>row.assets||[]).filter(asset=>asset.url);
  const job=[...jobs].reverse().find(item=>item.spec?.kind==='image_download')||null;
  return {
    total:urls.length,
    saved:urls.filter(asset=>asset.path).length,
    missing:urls.filter(asset=>!asset.path).length,
    active:!!job&&['queued','running','pausing','cancelling'].includes(job.status),
    job,
  };
}
