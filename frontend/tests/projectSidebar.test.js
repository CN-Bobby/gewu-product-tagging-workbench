import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const sidebar = readFileSync(new URL('../src/projectSidebar.jsx', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../src/projectSidebar.css', import.meta.url), 'utf8');

test('collapsed project tree keeps a keyboard accessible project library shortcut separate from expand', () => {
  assert.match(sidebar, /!open && <button[^>]*className=\{`ps-collapsed-library/);
  assert.match(sidebar, /ps-collapsed-library \$\{route\?\.page === 'projects' \? 'active' : ''\}/);
  assert.match(sidebar, /aria-label="项目资料"[^>]*title="项目资料" onClick=\{\(\) => navigate\('projects'\)\}/);
  assert.match(sidebar, /aria-current=\{route\?\.page === 'projects' \? 'page' : undefined\}/);
  assert.match(sidebar, /aria-label=\{open \? '折叠项目树' : '展开项目树'\} onClick=\{toggleOpen\}/);
  assert.ok(sidebar.includes('<button type="button" className={`ps-all'));
  assert.ok(sidebar.includes("onClick={() => navigate('projects')}><FolderOpen size={15}/>项目资料</button>"));
  assert.match(styles, /\.ps-collapsed-library\{width:100%;height:32px;display:grid;place-items:center/);
  assert.match(styles, /\.ps-collapsed-library:focus-visible\{/);
});
