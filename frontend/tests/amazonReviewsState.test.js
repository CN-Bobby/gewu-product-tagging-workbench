import test from 'node:test';
import assert from 'node:assert/strict';
import {
  AMAZON_VOC_FROM_REVIEWS_API,amazonReviewsVocHandoffEndpoint,amazonVocRunHash,buildAmazonReviewsVocHandoffRequest,
  canStartAmazonReviewsVoc,isAmazonReviewsVocEligible,isAmazonReviewsVocPartial,parseAmazonVocRoute,
} from '../src/amazonReviewsState.js';

const ready={id:'job-1',status:'completed',can_download:true,review_count:12};

test('VOC handoff is available only for downloadable nonempty jobs without an existing handoff',()=>{
  assert.equal(isAmazonReviewsVocEligible(ready),true);
  assert.equal(isAmazonReviewsVocEligible({...ready,can_download:false}),false);
  assert.equal(isAmazonReviewsVocEligible({...ready,review_count:0}),false);
  assert.equal(isAmazonReviewsVocEligible({...ready,status:'running'}),false);
  assert.equal(isAmazonReviewsVocEligible({...ready,voc_handoff:{run_id:'run-1'}}),false);
  assert.equal(canStartAmazonReviewsVoc(ready,'job-1'),false);
  assert.equal(canStartAmazonReviewsVoc(ready,''),true);
});

test('partial jobs are identified for the successful-review-only warning',()=>{
  assert.equal(isAmazonReviewsVocPartial({...ready,status:'completed_with_errors'}),true);
  assert.equal(isAmazonReviewsVocPartial({...ready,status:'partial'}),true);
  assert.equal(isAmazonReviewsVocPartial(ready),false);
});

test('handoff URL and exact-run route safely encode identifiers',()=>{
  assert.equal(AMAZON_VOC_FROM_REVIEWS_API,'/api/amazon-voc/from-amazon-reviews');
  assert.equal(amazonReviewsVocHandoffEndpoint('job/a b'),'/api/amazon-voc/from-amazon-reviews/job%2Fa%20b');
  assert.deepEqual(buildAmazonReviewsVocHandoffRequest('job-1'),{
    path:'/api/amazon-voc/from-amazon-reviews/job-1',body:{confirmed_reviewed:true},
  });
  assert.equal(amazonVocRunHash('run/a b'),'amazon-voc/run/run%2Fa%20b');
  assert.deepEqual(parseAmazonVocRoute('#/amazon-voc/run/run%2Fa%20b'),{page:'amazon-voc',runId:'run/a b'});
});

test('ordinary VOC route has no pinned run and other routes are not captured',()=>{
  assert.deepEqual(parseAmazonVocRoute('#/amazon-voc'),{page:'amazon-voc',runId:''});
  assert.deepEqual(parseAmazonVocRoute('#/amazon-voc/'),{page:'amazon-voc',runId:''});
  assert.equal(parseAmazonVocRoute('#/amazon-reviews'),null);
});
