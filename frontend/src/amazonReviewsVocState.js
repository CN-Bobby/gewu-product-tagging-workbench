export const AMAZON_REVIEWS_VOC_GOALS = Object.freeze({
  COMBINED: 'combined',
  REVIEWS_ONLY: 'reviews_only',
  VOC_ONLY: 'voc_only',
});

export const AMAZON_REVIEWS_VOC_DEFAULT_GOAL = AMAZON_REVIEWS_VOC_GOALS.COMBINED;
export const AMAZON_REVIEWS_VOC_TRANSLATION_DEFAULT = true;

const VALID_GOALS = new Set(Object.values(AMAZON_REVIEWS_VOC_GOALS));

export function normalizeAmazonReviewsVocGoal(value) {
  return VALID_GOALS.has(value) ? value : AMAZON_REVIEWS_VOC_DEFAULT_GOAL;
}

export function parseAmazonReviewsVocRunId(hash) {
  const parts = String(hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts[0] !== 'amazon-voc' || parts[1] !== 'run' || !parts[2]) return '';
  try {
    return decodeURIComponent(parts[2]);
  } catch {
    return parts[2];
  }
}

export function initialAmazonReviewsVocState({ initialRunId = '', hash = '' } = {}) {
  const runId = String(initialRunId || parseAmazonReviewsVocRunId(hash) || '');
  return {
    goal: runId ? AMAZON_REVIEWS_VOC_GOALS.VOC_ONLY : AMAZON_REVIEWS_VOC_DEFAULT_GOAL,
    translateEnabled: AMAZON_REVIEWS_VOC_TRANSLATION_DEFAULT,
    initialRunId: runId,
  };
}

export function defaultAmazonReviewsVocWorkspaceId(workspaces) {
  return Array.isArray(workspaces) && workspaces.length ? String(workspaces[0]?.id || '') : '';
}
