import test from 'node:test';
import assert from 'node:assert/strict';
import {
  amazonInputSnapshot,
  amazonPreviewMatches,
  initialAmazonSelection,
  mergeAmazonJobList,
  amazonPollIsCurrent,
  optimisticAmazonCancel,
  rollbackAmazonCancel,
} from '../src/amazonScraperState.js';

test('a preview is valid only for the exact input, marketplace, and profile snapshot', () => {
  const snapshot = amazonInputSnapshot('B000000001', 'US', 'balanced');
  const preview = { snapshot, items: [{ asin: 'B000000001' }] };
  assert.equal(amazonPreviewMatches(preview, snapshot), true);
  assert.equal(amazonPreviewMatches(preview, amazonInputSnapshot('B000000002', 'US', 'balanced')), false);
  assert.equal(amazonPreviewMatches(preview, amazonInputSnapshot('B000000001', 'UK', 'balanced')), false);
  assert.equal(amazonPreviewMatches(preview, amazonInputSnapshot('B000000001', 'US', 'fast')), false);
});

test('job lists keep locally added tasks and ignore older revisions', () => {
  const current = [
    { id: 'kept-new-task', status: 'queued', created_at: 30, revision: 2 },
    { id: 'old-task', status: 'cancelled', created_at: 20, revision: 4 },
  ];
  const merged = mergeAmazonJobList(current, [
    { id: 'old-task', status: 'running', created_at: 20, revision: 3 },
    { id: 'server-task', status: 'completed', created_at: 10, revision: 1 },
  ]);
  assert.deepEqual(merged.map(job => job.id), ['kept-new-task', 'old-task', 'server-task']);
  assert.equal(merged.find(job => job.id === 'old-task').status, 'cancelled');
  assert.equal(initialAmazonSelection('user-selected-task', [{ id: 'newer-server-task' }]), 'user-selected-task');
});

test('legacy job snapshots use updated_at when revision is absent', () => {
  const merged = mergeAmazonJobList(
    [{ id: 'legacy-job', status: 'cancelled', updated_at: 22 }],
    [{ id: 'legacy-job', status: 'running', updated_at: 21 }],
  );
  assert.equal(merged[0].status, 'cancelled');
});

test('poll snapshots are rejected after selection or batch generation changes', () => {
  const oldPoll = { jobId: 'job-a', generation: 7 };
  assert.equal(amazonPollIsCurrent(oldPoll, 'job-a', 7), true);
  assert.equal(amazonPollIsCurrent(oldPoll, 'job-b', 7), false);
  assert.equal(amazonPollIsCurrent(oldPoll, 'job-a', 8), false);
});

test('optimistic cancellation is immediate and older snapshots cannot rewind it', () => {
  const running = { id: 'job-cancel', status: 'running', updated_at: 30 };
  const cancelling = optimisticAmazonCancel(running);
  const merged = mergeAmazonJobList([cancelling], [
    { id: 'job-cancel', status: 'running', updated_at: 29 },
  ]);
  assert.equal(cancelling.status, 'cancelling');
  assert.equal(merged[0].status, 'cancelling');

  const settled = mergeAmazonJobList(merged, [
    { id: 'job-cancel', status: 'cancelled', updated_at: 31 },
  ]);
  assert.equal(settled[0].status, 'cancelled');
  assert.equal(rollbackAmazonCancel(cancelling).status, 'running');
});
