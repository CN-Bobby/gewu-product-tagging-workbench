"""Small, attributable term research. External references never become product facts."""
import hashlib,ipaddress,socket,time
from html.parser import HTMLParser
from urllib.parse import urlparse,quote,urljoin
import httpx
from sqlalchemy import select
from .db import *

class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)

def plain(html):
    p=Text();p.feed(html);return ' '.join(' '.join(p.parts).split())

def check_url(url):
    p=urlparse(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):raise ValueError('资料链接需为公开HTTPS网页')
    addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('不能读取本机或内网资料地址')
    return url

def fetch(url,params=None):
    for _ in range(4):
        check_url(url)
        with httpx.Client(timeout=20,follow_redirects=False) as client:
            with client.stream('GET',url,params=params,headers={'User-Agent':'ProductTaggingWorkbench/0.4 (local research tool)'}) as r:
                if r.is_redirect:url=urljoin(url,r.headers['location']);params=None;continue
                r.raise_for_status();data=bytearray()
                for chunk in r.iter_bytes():
                    data.extend(chunk)
                    if len(data)>2_000_000:raise ValueError('参考网页过大，请换用具体资料页面')
                return bytes(data).decode('utf-8',errors='replace')
    raise ValueError('参考网页重定向过多')

def read(did):
    with Session() as s:
        require(s,Dataset,did)
        return [dict(id=m.id,**m.data) for m in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='knowledge').order_by(WorkbenchMeta.updated_at.desc()).limit(30))]

def research(did,query='',urls=None,scope='',refresh=False):
    """Search Wikimedia product terminology; supplied URLs support primary-source verification."""
    import json
    from . import draft
    if not isinstance(query,str) or len(query)>300 or not query.strip() and not urls:raise ValueError('请输入具体术语或提供参考链接')
    urls=urls or []
    if not isinstance(urls,list) or len(urls)>3 or any(not isinstance(u,str) for u in urls):raise ValueError('每次最多3个参考链接')
    with Session() as s:require(s,Dataset,did)
    key='knowledge:'+hashlib.sha256(json.dumps([did,query,urls,scope],ensure_ascii=False).encode()).hexdigest()
    with Session() as s:
        cached=s.get(WorkbenchMeta,key)
        if cached and cached.data.get('status')=='ready' and not refresh:return dict(id=key,cached=True,**cached.data)
    started=time.perf_counter();sources=[];errors=[]
    if query.strip():
        language='zh' if any('\u4e00'<=c<='\u9fff' for c in query) else 'en'
        try:
            answer=json.loads(fetch(f'https://{language}.wikipedia.org/w/api.php',{'action':'query','list':'search','srsearch':query,'srlimit':3,'format':'json'}))
            for hit in answer.get('query',{}).get('search',[]):sources.append({'title':hit['title'],'url':f'https://{language}.wikipedia.org/wiki/'+quote(hit['title'].replace(' ','_')),'excerpt':plain(hit.get('snippet','')),'publisher':'Wikipedia','kind':'术语检索摘要，非一手产品证据','retrieved_at':now()})
        except Exception as e:errors.append('术语检索失败：'+str(e)[:200])
    for url in urls:
        try:
            content=plain(fetch(url));sources.append({'title':urlparse(url).hostname,'url':url,'excerpt':content[:1500],'publisher':urlparse(url).hostname,'kind':'指定来源网页摘录，需核对正文与适用范围','retrieved_at':now()})
        except Exception as e:errors.append(url+'：'+str(e)[:200])
    data={'query':query,'scope':scope,'sources':sources,'errors':errors,'status':'ready' if sources and not errors else 'partial' if sources else 'failed','retrieved_at':now(),'seconds':time.perf_counter()-started,'note':'外部资料仅辅助术语、规则和灵感；不能证明本商品事实。无结果不由训练语料补造。','confirmed':False,'correction':''}
    with draft.WRITE_LOCK,Session.begin() as s:
        m=s.get(WorkbenchMeta,key)
        if not m:m=WorkbenchMeta(id=key,dataset_id=did,kind='knowledge');s.add(m)
        m.data=data;m.updated_at=now();audit(s,did,'knowledge_researched',{'id':key,'query':query,'urls':urls,'status':data['status']})
    return dict(id=key,cached=False,**data)

def review(did,ident,correction,confirmed):
    from . import draft
    if not isinstance(correction,str) or len(correction)>10000:raise ValueError('资料修正不能超过10000字')
    with draft.WRITE_LOCK,Session.begin() as s:
        m=require(s,WorkbenchMeta,ident)
        if m.dataset_id!=did or m.kind!='knowledge':raise ValueError('资料不属于当前数据集')
        m.data={**m.data,'correction':correction,'confirmed':bool(confirmed),'reviewed_at':now()};audit(s,did,'knowledge_reviewed',{'id':ident,'confirmed':bool(confirmed)})
        return dict(id=m.id,**m.data)
