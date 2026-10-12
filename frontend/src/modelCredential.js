export function providersFromSettings(settings) {
  if (Array.isArray(settings?.providers)) return settings.providers;
  if (Array.isArray(settings?.model_settings?.providers)) return settings.model_settings.providers;
  if (Array.isArray(settings?.models?.providers)) return settings.models.providers;
  return [];
}

export function primaryCredentialForModel(settings, modelRef) {
  const providerName = String(modelRef || '').split('/')[0];
  const provider = providersFromSettings(settings).find(item => item.name === providerName);
  if (!provider) return '';
  const available = (provider.credentials || []).filter(item =>
    item?.id && item.enabled !== false && item.authorized !== false,
  );
  return available.find(item => item.id === provider.primary_credential_id)?.id
    || available[0]?.id
    || '';
}
