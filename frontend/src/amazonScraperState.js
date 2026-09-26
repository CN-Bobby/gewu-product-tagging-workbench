export function amazonInputSnapshot(input, marketplace, profile) {
  return { input, marketplace, profile };
}

export function amazonPreviewMatches(preview, snapshot) {
  return Boolean(preview?.snapshot && snapshot &&
    preview.snapshot.input === snapshot.input &&
    preview.snapshot.marketplace === snapshot.marketplace &&
    preview.snapshot.profile === snapshot.profile);
}

function revisionOf(job) {
  const revision = job?.revision;
  if (revision !== undefined && revision !== null && Number.isFinite(Number(revision))) return Number(revision);
  const updatedAt = Number(job?.updated_at);
  return Number.isFinite(updatedAt) ? updatedAt : 0;
}

export function mergeAmazonJob(existing, incoming) {
  if (!incoming?.id) return existing;
  if (!existing) return incoming;
  if (existing._optimistic_cancel) {
    const observedRevision = Number(existing._optimistic_cancel_revision) || 0;
    const incomingRevision = revisionOf(incoming);
    const settled = ['cancelled', 'completed', 'completed_with_errors', 'failed', 'paused', 'interrupted'].includes(incoming.status);
    if (incomingRevision <= observedRevision && !settled) {
      return { ...existing, ...incoming, status: 'cancelling', _optimistic_cancel: true,
        _optimistic_cancel_revision: observedRevision,
        _optimistic_cancel_previous_status: existing._optimistic_cancel_previous_status };
    }
  }
  if (revisionOf(incoming) <= revisionOf(existing)) return existing;
  return { ...existing, ...incoming };
}

export function optimisticAmazonCancel(job) {
  if (!job || job.status === 'cancelling') return job;
  return { ...job, status: 'cancelling', _optimistic_cancel: true,
    _optimistic_cancel_revision: revisionOf(job),
    _optimistic_cancel_previous_status: job.status };
}

export function rollbackAmazonCancel(job) {
  if (!job?._optimistic_cancel) return job;
  const { _optimistic_cancel, _optimistic_cancel_revision, _optimistic_cancel_previous_status, ...rest } = job;
  return { ...rest, status: _optimistic_cancel_previous_status || 'running' };
}

export function mergeAmazonJobList(existing, incoming) {
  const byId = new Map((existing || []).map(job => [job.id, job]));
  for (const job of incoming || []) {
    if (!job?.id) continue;
    byId.set(job.id, mergeAmazonJob(byId.get(job.id), job));
  }
  return [...byId.values()].sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
}

export function initialAmazonSelection(selectedId, jobs) {
  return selectedId || jobs?.[0]?.id || '';
}

export function amazonPollIsCurrent(ticket, selectedId, generation) {
  return Boolean(ticket && ticket.jobId === selectedId && ticket.generation === generation);
}
