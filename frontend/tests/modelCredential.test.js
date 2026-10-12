import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { primaryCredentialForModel } from '../src/modelCredential.js';

test('module model references resolve to the selected provider primary Key', () => {
  const settings = { providers: [
    { name: 'text', primary_credential_id: 'text-primary', credentials: [
      { id: 'text-backup', enabled: true, authorized: true },
      { id: 'text-primary', enabled: true, authorized: true },
    ] },
    { name: 'vision', primary_credential_id: 'vision-disabled', credentials: [
      { id: 'vision-disabled', enabled: false, authorized: true },
      { id: 'vision-ready', enabled: true, authorized: true },
    ] },
  ] };

  assert.equal(primaryCredentialForModel(settings, 'text/model-a'), 'text-primary');
  assert.equal(primaryCredentialForModel(settings, 'vision/model-b'), 'vision-ready');
  assert.equal(primaryCredentialForModel({ model_settings: settings }, 'text/model-a'), 'text-primary');
  assert.equal(primaryCredentialForModel(settings, 'missing/model'), '');
});

test('password inputs are centralized in Total Settings components', () => {
  const sourceRoot = new URL('../src/', import.meta.url);
  const passwordFiles = [];
  const visit = (directory, prefix = '') => {
    for (const entry of readdirSync(new URL(prefix, directory), { withFileTypes: true })) {
      const relative = join(prefix, entry.name).replaceAll('\\', '/');
      if (entry.isDirectory()) visit(directory, relative);
      else if (/\.(jsx?|tsx?)$/.test(entry.name)) {
        const source = readFileSync(new URL(relative, directory), 'utf8');
        if (/type\s*=\s*["']password["']/.test(source)) passwordFiles.push(relative);
      }
    }
  };
  visit(sourceRoot);
  passwordFiles.sort();
  assert.deepEqual(passwordFiles, ['settingsTools.jsx', 'workbenchPages.jsx']);
});

test('business pages keep task model and credential references without Key entry controls', () => {
  const sourceRoot = new URL('../src/', import.meta.url);
  for (const file of ['Xunlongjue.jsx', 'amazonReviews.jsx', 'amazonVoc.jsx', 'keywordResearch.jsx']) {
    const source = readFileSync(new URL(file, sourceRoot), 'utf8');
    assert.doesNotMatch(source, /type\s*=\s*["']password["']/);
    assert.doesNotMatch(source, /使用的 Key|模型 Key|使用的凭据|Sorftime API Key/);
  }
  assert.match(readFileSync(new URL('amazonReviews.jsx', sourceRoot), 'utf8'), /credential_id/);
  assert.match(readFileSync(new URL('amazonVoc.jsx', sourceRoot), 'utf8'), /credentialId/);
  assert.match(readFileSync(new URL('keywordResearch.jsx', sourceRoot), 'utf8'), /credential_id/);
});
