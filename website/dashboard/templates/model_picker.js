// Account-scoped catalog. The server owns provider discovery and automatic routing.
let modelCatalog = null;
let catalogFailed = false;
let catalogFetchedAt = 0;
let catalogGeneration = 0;
let catalogPending = null;
const modelPickerScopes = ['hero', 'continue', 'restart', 'settings'];

function isAccountPicker(scope) {
  return scope === 'hero' || scope === 'settings';
}

function pickerCatalog(scope) {
  const select = document.getElementById(scope + '-model-select');
  return isAccountPicker(scope) || select.dataset.owner === '1' ? modelCatalog : null;
}

function resetModelPicker(scope, projectModel = '', owner = false) {
  const select = document.getElementById(scope + '-model-select');
  select.dataset.projectModel = projectModel || '';
  select.dataset.owner = owner ? '1' : '0';
  delete select.dataset.restoreSelection;
  document.getElementById(scope + '-model-search').value = '';
  select.value = '';
  document.getElementById(scope + '-model-manual').value = '';
  renderModelPicker(scope);
}

function renderModelPicker(scope) {
  const select = document.getElementById(scope + '-model-select');
  const catalog = pickerCatalog(scope);
  const previous = (catalog && select.dataset.restoreSelection) || select.value;
  const saved = catalog?.default_launch_model;
  const automatic = catalog?.automatic_model;
  const project = select.dataset.projectModel;
  let label = t('model_automatic');
  if (!isAccountPicker(scope)) {
    label = project ? t('model_project_current', project) : t('model_project_saved');
  } else if (saved && saved === automatic) {
    label = t('model_account_default', saved);
  } else if (automatic) {
    label = t('model_auto_id', automatic);
  } else if (catalog) {
    label = t('model_choose');
  }
  const options = document.createDocumentFragment();
  options.append(new Option(label, ''));
  const providers = catalog?.providers || [];
  const search = document.getElementById(scope + '-model-search');
  search.hidden = providers.reduce((n, p) => n + p.models.length, 0) < 25;
  const query = search.hidden ? '' : search.value.trim().toLowerCase();
  let matches = 0;
  for (const provider of providers) {
    if (!provider.models.length) continue;
    const group = document.createElement('optgroup');
    group.label = provider.label;
    for (const model of provider.models) {
      const name = model.name || model.id;
      const match = !query || [provider.label, model.id, name].some(s => s.toLowerCase().includes(query));
      if (match) matches++;
      else if (model.id !== previous) continue;
      const text = isOpenRouterFree(model.id) ? t('model_free_option', name) : name;
      const opt = new Option(text, model.id);
      opt.title = model.id;
      group.appendChild(opt);
    }
    if (group.childElementCount) options.appendChild(group);
  }
  options.append(new Option(t('model_manual'), '__manual__'));
  select.dataset.noMatches = query && !matches ? '1' : '';
  select.replaceChildren(options);
  select.value = previous;
  if (catalog) delete select.dataset.restoreSelection;
  if (select.selectedIndex === -1) select.value = '';
  const noKeys = catalog && !catalog.providers.length;
  document.getElementById(scope + '-model-no-keys').hidden = !noKeys;
  if (isAccountPicker(scope)) {
    document.getElementById(scope + '-clear-default-model').hidden = !saved;
  }
  syncModelPicker(scope);
}

function renderModelPickers() {
  modelPickerScopes.forEach(renderModelPicker);
}

function isOpenRouterFree(model) {
  return model === 'openrouter/openrouter/free' || (model.startsWith('openrouter/') && model.endsWith(':free'));
}

function syncModelPicker(scope) {
  const select = document.getElementById(scope + '-model-select');
  const manual = document.getElementById(scope + '-model-manual');
  const note = document.getElementById(scope + '-model-note');
  manual.hidden = select.value !== '__manual__';
  manual.setCustomValidity('');
  const catalog = pickerCatalog(scope);
  const model = manual.hidden
    ? select.value || (isAccountPicker(scope) ? catalog?.automatic_model : select.dataset.projectModel) || ''
    : manual.value.trim();
  const hints = [];
  const canViewCatalog = isAccountPicker(scope) || select.dataset.owner === '1';
  if (select.dataset.noMatches) hints.push(t('model_no_matches'));
  if (canViewCatalog && !catalog && catalogPending) hints.push(t('model_loading'));
  if (isOpenRouterFree(model)) hints.push(t('model_free_limits'));
  if (model.startsWith('gemini/')) hints.push(t('model_google_billing'));
  if (canViewCatalog && catalogFailed) hints.push(t('model_catalog_failure'));
  if (catalog) {
    const providerId = model.split('/')[0];
    const unavailable = catalog.providers.filter(p => !p.models.length);
    if (unavailable.length) hints.push(t('model_provider_unavailable', unavailable.map(p => p.label).join(', ')));
    const selectedProvider = catalog.providers.find(p => p.id === providerId);
    if (selectedProvider) hints.unshift(t('model_via', selectedProvider.label));
    const source = selectedProvider?.source;
    if (source === 'dsh') hints.push(t('model_source_dsh'));
    if (source === 'cache') hints.push(t('model_source_cache'));
    if (scope === 'settings' && catalog.default_launch_model && catalog.default_launch_model !== catalog.automatic_model) {
      hints.push(t('model_inactive_default', catalog.default_launch_model));
    }
  }
  note.textContent = hints.join(' ');
  note.hidden = !hints.length;
}

// Return null only for a selection the user needs to finish. Empty on new runs
// delegates automatic selection to the server, including during initial load.
function readModelPicker(scope) {
  const select = document.getElementById(scope + '-model-select');
  if (select.dataset.restoreSelection && !modelCatalog) {
    showToast(t(catalogPending ? 'model_loading' : 'model_catalog_failure'), 'warn');
    return null;
  }
  if (select.value === '__manual__') {
    const manual = document.getElementById(scope + '-model-manual');
    const value = manual.value.trim();
    const wireId = value.replace(/^openrouter\/~(?=[A-Za-z0-9])/, 'openrouter/');
    if (value.length > 200 || !/^[a-z0-9_]+\/[a-zA-Z0-9][a-zA-Z0-9._:/-]*$/.test(wireId)
        || value.split('/').some(part => !part || part === '.' || part === '..')) {
      manual.setCustomValidity(t('model_invalid_id'));
      manual.reportValidity();
      manual.focus();
      return null;
    }
    return value;
  }
  if (select.value) return select.value;
  if (!isAccountPicker(scope)) return select.dataset.projectModel || '';
  if (modelCatalog && !modelCatalog.automatic_model) {
    showToast(t(modelCatalog.providers.length ? 'model_choose' : 'model_add_key'), 'warn');
    select.focus();
    return null;
  }
  return '';
}

function loadModelCatalog(force = false, keysChanged = false) {
  if (!force && catalogPending) return catalogPending;
  if (!force && modelCatalog && Date.now() - catalogFetchedAt < 30 * 60 * 1000) return;
  const generation = ++catalogGeneration;
  if (keysChanged) {
    // Credentials may just have changed. Drop the old account groups immediately.
    for (const scope of modelPickerScopes) {
      const select = document.getElementById(scope + '-model-select');
      if (select.value && select.value !== '__manual__') select.dataset.restoreSelection = select.value;
    }
    modelCatalog = null;
    catalogFailed = false;
  }
  catalogPending = (async () => {
    try {
      const res = await fetch(APP_BASE + '/api/user/model-catalog', {cache: 'no-store'});
      if (!res.ok) throw new Error('catalog unavailable');
      const data = await res.json();
      if (!Array.isArray(data.providers)) throw new Error('invalid catalog');
      if (generation !== catalogGeneration) return;
      modelCatalog = data;
      catalogFetchedAt = Date.now();
      catalogFailed = false;
    } catch {
      if (generation !== catalogGeneration) return;
      catalogFailed = true;
    } finally {
      if (generation === catalogGeneration) {
        catalogPending = null;
        renderModelPickers();
      }
    }
  })();
  renderModelPickers();
  return catalogPending;
}

async function saveDefaultLaunchModel(scope, clear) {
  const model = clear ? '' : readModelPicker(scope);
  if (model === null) return;
  const value = clear ? '' : model || modelCatalog?.automatic_model;
  if (!clear && !value) { showToast(t('model_choose'), 'warn'); return; }
  try {
    const res = await fetch(APP_BASE + '/api/user/default-launch-model', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: value}),
    });
    if (!res.ok) {
      const data = await res.json();
      showToast(data.detail || t('model_save_failed'), 'warn');
      return;
    }
    resetModelPicker(scope);
    await loadModelCatalog(true);
    showToast(clear ? t('model_reset_done') : t('model_saved'));
  } catch { showToast(t('model_save_failed'), 'warn'); }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && currentUser) loadModelCatalog();
});
