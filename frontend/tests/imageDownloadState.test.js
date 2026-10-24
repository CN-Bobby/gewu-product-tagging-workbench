import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {imageDownloadSummary} from '../src/imageDownloadState.js';

test('image status counts only URL assets and preserves local image progress',()=>{
  const rows=[
    {assets:[{source:'excel',path:'embedded.png'},{url:'https://example.com/1',path:'one.png'}]},
    {assets:[{url:'https://example.com/2',path:null}]},
  ];
  const jobs=[{id:'j1',status:'running',spec:{kind:'image_download'},metrics:{images_completed:0,images_expected:1}}];
  assert.deepEqual(imageDownloadSummary(rows,jobs),{
    total:2,saved:1,missing:1,active:true,job:jobs[0],
  });
});

test('tagging page exposes a dedicated missing-image button',()=>{
  const main=readFileSync(new URL('../src/main.jsx',import.meta.url),'utf8');
  assert.match(main,/下载缺失图片/);
  assert.match(main,/\/api\/datasets\/\$\{did\}\/images\/download/);
  assert.match(main,/imageDownloadSummary\(/);
});
