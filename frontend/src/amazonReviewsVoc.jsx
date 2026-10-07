import React, { useEffect, useState } from 'react';
import { AmazonReviews } from './amazonReviews';
import { AmazonVOC } from './amazonVoc';
import {
  AMAZON_REVIEWS_VOC_GOALS,
  defaultAmazonReviewsVocWorkspaceId,
  initialAmazonReviewsVocState,
  normalizeAmazonReviewsVocGoal,
} from './amazonReviewsVocState';
import './amazonReviewsVoc.css';

const GOAL_OPTIONS = [
  { value: AMAZON_REVIEWS_VOC_GOALS.COMBINED, label: '评论拉取 + VOC', detail: '拉取评论后继续进行 VOC 分析' },
  { value: AMAZON_REVIEWS_VOC_GOALS.REVIEWS_ONLY, label: '仅评论拉取', detail: '拉取并导出评论，不启动分析' },
  { value: AMAZON_REVIEWS_VOC_GOALS.VOC_ONLY, label: '仅 VOC 分析', detail: '上传一份已有评论 Excel' },
];

export function AmazonReviewsVoc({
  api,
  initialRunId = '',
  initialNotice = '',
  onNoticeConsumed,
  onNavigate,
}) {
  const [initial] = useState(() => initialAmazonReviewsVocState({
    initialRunId,
    hash: typeof window === 'undefined' ? '' : window.location.hash,
  }));
  const [goal, setGoal] = useState(initial.goal);
  const [translateEnabled, setTranslateEnabled] = useState(initial.translateEnabled);
  const [forceRefresh, setForceRefresh] = useState(false);
  const [workspaces, setWorkspaces] = useState([]);
  const [workspaceId, setWorkspaceId] = useState('');
  useEffect(() => {
    let alive = true;
    // This legacy route returns backend Workspace records (id/name), not Amazon VOC projects.
    api('/api/projects', undefined, 'GET').then(value => {
      if (!alive) return;
      const list = Array.isArray(value) ? value : [];
      setWorkspaces(list);
      setWorkspaceId(current => current || defaultAmazonReviewsVocWorkspaceId(list));
    }).catch(() => {
      if (alive) setWorkspaces([]);
    });
    return () => { alive = false; };
  }, [api]);
  const selectedGoal = normalizeAmazonReviewsVocGoal(goal);
  const reviewsMode = selectedGoal === AMAZON_REVIEWS_VOC_GOALS.COMBINED
    ? 'combined'
    : 'reviews_only';

  return (
    <div className="arv-workbench">
      <section className="arv-controls" aria-label="评论与 VOC 任务设置">
        <div className="arv-controls-heading">
          <div>
            <span className="arv-eyebrow">REVIEWS · VOICE OF CUSTOMER</span>
            <h1>评论与 VOC 工作台</h1>
            <p>选择本次任务目标；可从评论采集开始，也可直接分析现有评论表。</p>
          </div>
        </div>
        <fieldset className="arv-goals">
          <legend>任务目标</legend>
          {GOAL_OPTIONS.map(option => (
            <label className={selectedGoal === option.value ? 'selected' : ''} key={option.value}>
              <input
                type="radio"
                name="amazon-reviews-voc-goal"
                value={option.value}
                checked={selectedGoal === option.value}
                onChange={() => setGoal(option.value)}
              />
              <span><b>{option.label}</b><small>{option.detail}</small></span>
            </label>
          ))}
        </fieldset>
        <label className="arv-workspace">
          缓存工作区
          <select value={workspaceId} onChange={event => setWorkspaceId(event.target.value)}>
            <option value="">本地默认范围</option>
            {workspaces.map(workspace => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
          </select>
          <small>选择工作区以隔离七天内的评论、翻译和 VOC 阶段缓存；留空使用本地默认范围。</small>
        </label>
        <label className="arv-force-refresh">
          <input type="checkbox" checked={forceRefresh} onChange={event => setForceRefresh(event.target.checked)} />
          <span><b>强制刷新缓存</b><small>重新请求评论与模型；本次结果仍会写回缓存。</small></span>
        </label>
        {selectedGoal !== AMAZON_REVIEWS_VOC_GOALS.VOC_ONLY && (
          <label className="arv-translation">
            <input
              type="checkbox"
              checked={translateEnabled}
              onChange={event => setTranslateEnabled(event.target.checked)}
            />
            <span><b>翻译评论标题和正文</b><small>默认开启；关闭后保留原文并跳过翻译。</small></span>
          </label>
        )}
      </section>

      {selectedGoal === AMAZON_REVIEWS_VOC_GOALS.VOC_ONLY ? (
        <AmazonVOC
          api={api}
          workspaceId={workspaceId}
          forceRefresh={forceRefresh}
          initialRunId={initial.initialRunId}
          initialNotice={initialNotice}
          onNoticeConsumed={onNoticeConsumed}
          onNavigate={onNavigate}
        />
      ) : (
        <AmazonReviews
          api={api}
          workspaceId={workspaceId}
          workflowMode={reviewsMode}
          translateEnabled={translateEnabled}
          forceRefresh={forceRefresh}
          onNavigate={onNavigate}
        />
      )}
    </div>
  );
}
