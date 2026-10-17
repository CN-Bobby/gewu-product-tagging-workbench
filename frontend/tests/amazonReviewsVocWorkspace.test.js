import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {defaultAmazonReviewsVocWorkspaceId} from '../src/amazonReviewsVocState.js';

const page = readFileSync(new URL('../src/amazonReviewsVoc.jsx', import.meta.url), 'utf8');

test('the unified page derives cache scope from the workbench Workspace listing', () => {
  const workspaces = [
    {id: 'workspace-1', name: 'My workspace'},
    {id: 'workspace-2', name: 'Another workspace'},
  ];
  assert.equal(defaultAmazonReviewsVocWorkspaceId(workspaces), 'workspace-1');
  assert.equal(defaultAmazonReviewsVocWorkspaceId([]), '');
});

test('Reviews receives a Workspace id and never an Amazon VOC project id', () => {
  assert.match(page, /api\('\/api\/projects', undefined, 'GET'\)/);
  assert.match(page, /const \[workspaces, setWorkspaces\] = useState\(\[\]\)/);
  assert.match(page, /workspaces\.map\(workspace => <option key=\{workspace\.id\} value=\{workspace\.id\}>/);
  assert.match(page, /<AmazonReviews\s+api=\{api\}\s+workspaceId=\{workspaceId\}/);
  assert.doesNotMatch(page, /\/api\/amazon-voc\/projects/);
});
