import test from 'node:test';
import assert from 'node:assert/strict';
import {
  AMAZON_REVIEWS_VOC_DEFAULT_GOAL,
  AMAZON_REVIEWS_VOC_GOALS,
  AMAZON_REVIEWS_VOC_TRANSLATION_DEFAULT,
  defaultAmazonReviewsVocWorkspaceId,
  initialAmazonReviewsVocState,
  normalizeAmazonReviewsVocGoal,
} from '../src/amazonReviewsVocState.js';

test('the unified workflow defaults to combined reviews and VOC with translation enabled', () => {
  const state = initialAmazonReviewsVocState();
  assert.equal(AMAZON_REVIEWS_VOC_DEFAULT_GOAL, 'combined');
  assert.equal(state.goal, 'combined');
  assert.equal(state.translateEnabled, true);
  assert.equal(AMAZON_REVIEWS_VOC_TRANSLATION_DEFAULT, true);
});

test('all three task goals are accepted and unknown values fall back to combined', () => {
  assert.deepEqual(Object.values(AMAZON_REVIEWS_VOC_GOALS), ['combined', 'reviews_only', 'voc_only']);
  for (const goal of Object.values(AMAZON_REVIEWS_VOC_GOALS)) {
    assert.equal(normalizeAmazonReviewsVocGoal(goal), goal);
  }
  assert.equal(normalizeAmazonReviewsVocGoal('invalid'), 'combined');
});

test('an Amazon VOC run deep link opens the VOC-only goal and retains its run id', () => {
  const state = initialAmazonReviewsVocState({ hash: '#/amazon-voc/run/run%2F42' });
  assert.equal(state.goal, 'voc_only');
  assert.equal(state.initialRunId, 'run/42');
  assert.equal(initialAmazonReviewsVocState({ initialRunId: 'run-7' }).initialRunId, 'run-7');
});

test('workspace scope defaults to the first existing project or the local scope', () => {
  assert.equal(defaultAmazonReviewsVocWorkspaceId([{ id: 'workspace-1' }, { id: 'workspace-2' }]), 'workspace-1');
  assert.equal(defaultAmazonReviewsVocWorkspaceId([]), '');
  assert.equal(defaultAmazonReviewsVocWorkspaceId(null), '');
});
