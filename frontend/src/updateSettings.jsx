import React,{useState}from'react';

export function UpdateSettings({api}){
 const[busy,setBusy]=useState(false),[result,setResult]=useState(null),[error,setError]=useState('');
 const check=async()=>{setBusy(true);setError('');setResult(null);try{setResult(await api('/api/updates/check',undefined,'GET'))}catch(e){setError(e.message)}finally{setBusy(false)}};
 return <section className="home-card sellersprite-settings">
  <div className="home-card-head"><div><small>APPLICATION UPDATES</small><h3>软件更新</h3></div><span>手动检查</span></div>
  <p>检查 GitHub 上已发布的绿色版。下载后请退出工作台，备份原程序目录，再把 ZIP 解压到原程序目录覆盖。程序数据和缓存目录独立保存，升级时不要移动或覆盖它们。</p>
  <div className="button-row"><button className="primary" disabled={busy} onClick={check}>{busy?'正在检查…':'检查更新'}</button></div>
  {error&&<p role="alert" className="settings-error">检查失败：{error}</p>}
  {result&&!result.update_available&&<p role="status" className="settings-saved">已是最新版本（{result.current_version}）。GitHub 最新发布：{result.latest_version}。</p>}
  {result?.update_available&&<div role="status" className="settings-saved"><p>发现新版本：{result.latest_version}</p>{result.release_notes_summary&&<p>{result.release_notes_summary}</p>}<p>下载后可用 PowerShell 校验：<code>Get-FileHash .\GeWuWorkbench-{result.latest_version}-portable.zip -Algorithm SHA256</code>，结果应与下面的值一致。</p><p>SHA-256：<code>{result.sha256}</code></p><div className="button-row"><a className="text-link" href={result.download_url} target="_blank" rel="noopener noreferrer">下载绿色版 ZIP</a><a className="text-link" href={result.release_url} target="_blank" rel="noopener noreferrer">查看 Release 说明</a></div></div>}
 </section>;
}
