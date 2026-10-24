export const KEYWORD_TREND_COLORS = [
  '#28624b', '#bc6c3f', '#5174a8', '#8a639f', '#9b791f',
  '#2f8584', '#ad5474', '#697746', '#a95843', '#5a7184',
];

export function trendRows(report, mode = 'index') {
  const keywords = Array.isArray(report?.keywords) ? report.keywords.slice(0, 10) : [];
  const months = Array.isArray(report?.months) ? report.months : [];
  const averages = keywords.map(item => {
    const values = (item.values || []).filter(value => Number.isFinite(value) && value >= 0);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
  });
  return months.map((month, monthIndex) => {
    const row = { month };
    keywords.forEach((item, index) => {
      const raw = item.values?.[monthIndex];
      row[`raw${index}`] = Number.isFinite(raw) && raw >= 0 ? raw : null;
      row[`k${index}`] = row[`raw${index}`] == null ? null
        : mode === 'index' ? (averages[index] > 0 ? Math.round(row[`raw${index}`] / averages[index] * 100) : null)
          : row[`raw${index}`];
    });
    return row;
  });
}

export const formatSearches = value => value == null ? '—' : Math.round(value).toLocaleString('zh-CN');
