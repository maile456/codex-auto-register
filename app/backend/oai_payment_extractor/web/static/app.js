(function () {
  "use strict";

  const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);
  const DELETABLE_STATES = new Set(["succeeded", "failed", "cancelled"]);
  const FORM_PREFERENCES_KEY = "payment_link_extractor.form_preferences";
  const TASK_VIEW_MODE_KEY = "payment_link_extractor.task_view_mode";
  const PASSWORD_STORAGE_KEY = "payment_link_extractor.workbench_password";
  const SERVER_PROXY_POOL_KEY = "payment_link_extractor.server_proxy_pool_id";
  const tasks = new Map();
  const taskCheckoutProxies = new Map();
  const taskUpdateProxies = new Map();
  const revealedProxyTasks = new Set();
  const selectedTaskIds = new Set();
  const bulkDeletePending = new Set();
  const copyFeedbackTimers = new WeakMap();
  let bulkNetworkRetryPending = false;
  const CSV_HEADERS = [
    "金额", "币种", "账号", "付款链接", "支付方式", "Checkout 类型", "账单国家",
    "Checkout 会话 ID", "支付方式 ID", "Stripe 跳转地址", "提交时间", "完成时间", "任务 ID",
  ];
  let taskFilter = "all";
  let taskViewMode = "card";
  let detailTaskId = "";
  let socket = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let authPassword = "";
  let authReady = false;
  let batchImportEntries = [];
  let batchImportValidated = false;
  let batchImportSubmitting = false;
  let batchImportFinished = false;
  let checkoutProxyPoolCursor = 0;
  let updateProxyPoolCursor = 0;

  const elements = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Workbench-Password", authPassword);
    return fetch(url, { ...options, headers }).then(response => {
      if (response.status === 401 && authReady) lockWorkbench("密码已失效，请重新登录");
      return response;
    });
  }

  function readSavedPassword() {
    try {
      return localStorage.getItem(PASSWORD_STORAGE_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function savePassword(password) {
    try {
      localStorage.setItem(PASSWORD_STORAGE_KEY, password);
    } catch (error) {
      // Keep the session usable when browser storage is unavailable.
    }
  }

  function clearSavedPassword() {
    try {
      localStorage.removeItem(PASSWORD_STORAGE_KEY);
    } catch (error) {
      // Ignore unavailable browser storage.
    }
  }

  function setAuthError(message) {
    elements.authError.textContent = message || "";
  }

  function lockWorkbench(message) {
    authReady = false;
    authPassword = "";
    clearSavedPassword();
    if (socket) {
      const currentSocket = socket;
      socket = null;
      currentSocket.close();
    }
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    tasks.clear();
    taskCheckoutProxies.clear();
    taskUpdateProxies.clear();
    revealedProxyTasks.clear();
    selectedTaskIds.clear();
    closeTaskDetails();
    elements.credentialInput.value = "";
    setFormError("");
    updateCredentialPreview();
    elements.workbench.hidden = true;
    elements.authGate.hidden = false;
    elements.authPassword.value = "";
    setAuthError(message || "请输入工作台密码");
  }

  function logout() {
    lockWorkbench("已退出认证，请重新登录");
  }

  async function authenticate(password) {
    elements.authSubmit.disabled = true;
    authPassword = password;
    try {
      const response = await apiFetch("/api/health");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "密码错误");
      savePassword(password);
      authReady = true;
      setAuthError("");
      elements.authGate.hidden = true;
      elements.workbench.hidden = false;
      await loadDefaultPreferences();
      await loadExistingTasks();
      connectTaskSocket();
    } catch (error) {
      authReady = false;
      authPassword = "";
      clearSavedPassword();
      setAuthError(error.message || "认证失败");
    } finally {
      elements.authSubmit.disabled = false;
    }
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function findAccessToken(value) {
    if (typeof value === "string") return "";
    if (Array.isArray(value)) {
      for (const item of value) {
        const token = findAccessToken(item);
        if (token) return token;
      }
      return "";
    }
    if (!value || typeof value !== "object") return "";
    for (const key of ["accessToken", "access_token", "token"]) {
      const token = typeof value[key] === "string" ? value[key].trim() : "";
      if (token) return token;
    }
    for (const item of Object.values(value)) {
      const token = findAccessToken(item);
      if (token) return token;
    }
    return "";
  }

  function decodeJwtPayload(token) {
    const parts = String(token || "").split(".");
    if (parts.length < 2) return {};
    try {
      const encoded = parts[1].replaceAll("-", "+").replaceAll("_", "/");
      const padded = encoded + "=".repeat((4 - encoded.length % 4) % 4);
      const binary = atob(padded);
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch (error) {
      return {};
    }
  }

  function extractAccountEmail(token) {
    const payload = decodeJwtPayload(token);
    const profile = payload["https://api.openai.com/profile"];
    const profileEmail = profile && typeof profile === "object" ? profile.email : "";
    if (typeof profileEmail === "string" && profileEmail.includes("@")) return profileEmail.trim();
    for (const key of ["email", "preferred_username", "upn"]) {
      const value = typeof payload[key] === "string" ? payload[key].trim() : "";
      if (value.includes("@")) return value;
    }
    return "";
  }

  function validateAccessToken(token) {
    const parts = String(token || "").split(".");
    if (parts.length !== 3 || parts.some(part => !part)) return "Access Token 格式无效";
    const payload = decodeJwtPayload(token);
    if (!payload || Object.keys(payload).length === 0) return "Access Token 内容无效";
    if (payload.exp && Number.isFinite(Number(payload.exp)) && Number(payload.exp) <= Date.now() / 1000) {
      return "Access Token 已过期";
    }
    if (!extractAccountEmail(token)) return "Access Token 未解析出有效账号";
    return "";
  }

  function inspectCredentialInput(raw) {
    const text = String(raw || "").trim();
    if (!text) return { valid: false, isJson: false, message: "请输入 Access Token 或 JSON" };
    if (!text.startsWith("{") && !text.startsWith("[")) {
      const validationMessage = validateAccessToken(text);
      return {
        valid: !validationMessage,
        isJson: false,
        accessToken: text,
        accountEmail: extractAccountEmail(text),
        message: validationMessage || "Access Token 校验通过",
      };
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      return { valid: false, isJson: true, message: "JSON 格式无效" };
    }
    if (typeof parsed === "string") {
      const token = parsed.trim();
      const validationMessage = token ? validateAccessToken(token) : "JSON 中未找到 Access Token";
      return {
        valid: Boolean(token) && !validationMessage,
        isJson: true,
        accessToken: token,
        accountEmail: extractAccountEmail(token),
        message: validationMessage || "JSON 中已识别 Access Token",
      };
    }
    if (!parsed || typeof parsed !== "object") {
      return { valid: false, isJson: true, message: "JSON 必须是对象或 Token" };
    }
    const token = findAccessToken(parsed);
    const validationMessage = token ? validateAccessToken(token) : "JSON 中未找到 Access Token";
    return {
      valid: Boolean(token) && !validationMessage,
      isJson: true,
      parsed,
      accessToken: token,
      accountEmail: extractAccountEmail(token),
      message: validationMessage || "JSON 中已识别 Access Token",
    };
  }

  function parseCredentialInput(raw) {
    const inspection = inspectCredentialInput(raw);
    if (!inspection.valid) throw new Error(inspection.message);
    if (!inspection.isJson || typeof inspection.parsed === "string") {
      return { access_token: inspection.accessToken };
    }
    if (Array.isArray(inspection.parsed)) return { access_token: inspection.accessToken };
    return { ...inspection.parsed, access_token: inspection.accessToken };
  }

  function saveFormPreferences() {
    syncCountryFromProxy();
    const preferences = {
      country: byId("country").value,
      payment_method: byId("payment-method").value,
      checkout_proxy: normalizeProxyPoolText(byId("checkout-proxy").value),
      update_proxy: normalizeProxyPoolText(byId("update-proxy").value),
      proxy_source_url: byId("proxy-source-url") ? byId("proxy-source-url").value.trim() : "",
      apply_checkout_update: byId("apply-update").checked,
      rotate_checkout_proxy: byId("rotate-checkout-proxy").checked,
      rotate_update_proxy: byId("rotate-update-proxy").checked,
      oaics_only: byId("oaics-only").checked,
    };
    try {
      localStorage.setItem(FORM_PREFERENCES_KEY, JSON.stringify(preferences));
    } catch (error) {
      // Storage may be unavailable in private browsing or restricted contexts.
    }
    updateProxyCounts();
  }

  function syncCountryFromProxy() {
    if (byId("country").disabled) return;
    const firstProxy = proxyPoolLines(byId("checkout-proxy").value)[0] || "";
    const match = firstProxy.match(/-(?:res|country|region|area|dc|res_sc)-([a-z]{2})(?:[-_:]|$)/i);
    if (!match) return;
    const country = match[1].toUpperCase();
    const option = Array.from(byId("country").options).find(item => item.value === country);
    if (option) byId("country").value = country;
  }

  function proxyPoolLines(value) {
    // Preserve every pasted row. Some providers intentionally issue several
    // entries with the same gateway but different sessions behind the scenes.
    return String(value || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  }

  function normalizeProxyPoolText(value) {
    return proxyPoolLines(value).join("\n");
  }

  function updateProxyCounts() {
    const checkoutCount = byId("checkout-proxy-count");
    const updateCount = byId("update-proxy-count");
    if (checkoutCount) checkoutCount.textContent = `已保存 ${proxyPoolLines(byId("checkout-proxy").value).length} 条`;
    if (updateCount) updateCount.textContent = `已保存 ${proxyPoolLines(byId("update-proxy").value).length} 条`;
  }

  function selectProxyFromPool(value, kind) {
    const lines = proxyPoolLines(value);
    if (!lines.length) return "";
    if (kind === "update") {
      const selected = lines[updateProxyPoolCursor % lines.length];
      updateProxyPoolCursor += 1;
      return selected;
    }
    const selected = lines[checkoutProxyPoolCursor % lines.length];
    checkoutProxyPoolCursor += 1;
    return selected;
  }

  async function loadDefaultPreferences() {
    try {
      const response = await apiFetch("/api/defaults");
      const defaults = await response.json();
      if (!response.ok || !defaults || typeof defaults !== "object") return;
      if (typeof defaults.force_country === "string" && defaults.force_country) {
        byId("country").value = defaults.force_country;
        byId("country").disabled = true;
      } else if (!byId("country").value && typeof defaults.country === "string") {
        byId("country").value = defaults.country;
      }
      if (!byId("payment-method").value && typeof defaults.payment_method === "string") byId("payment-method").value = defaults.payment_method;
      let savedPoolId = "";
      try { savedPoolId = localStorage.getItem(SERVER_PROXY_POOL_KEY) || ""; } catch (error) { /* ignore */ }
      const applyServerPool = Boolean(defaults.proxy_pool_id) && defaults.proxy_pool_id !== savedPoolId;
      if ((applyServerPool || !byId("checkout-proxy").value.trim()) && typeof defaults.checkout_proxy === "string") {
        byId("checkout-proxy").value = normalizeProxyPoolText(defaults.checkout_proxy);
      }
      if ((applyServerPool || !byId("update-proxy").value.trim()) && typeof defaults.update_proxy === "string") {
        byId("update-proxy").value = normalizeProxyPoolText(defaults.update_proxy);
      }
      if (applyServerPool) {
        try { localStorage.setItem(SERVER_PROXY_POOL_KEY, defaults.proxy_pool_id); } catch (error) { /* ignore */ }
      }
      if (byId("proxy-source-url") && !byId("proxy-source-url").value.trim() && typeof defaults.proxy_source_url === "string") {
        byId("proxy-source-url").value = defaults.proxy_source_url;
      }
      saveFormPreferences();
    } catch (error) {
      // Environment defaults are optional; browser-saved preferences still work.
    }
  }

  async function refreshProxySource() {
    const input = byId("proxy-source-url");
    const button = byId("refresh-proxy-source");
    const status = byId("proxy-source-status");
    const sourceUrl = input.value.trim();
    if (!sourceUrl) {
      status.textContent = "请先填写订阅地址";
      return;
    }
    button.disabled = true;
    status.textContent = "正在读取…";
    try {
      const response = await apiFetch(`/api/proxy/source?url=${encodeURIComponent(sourceUrl)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "代理订阅读取失败");
      const proxyText = Array.isArray(data.proxies) ? data.proxies.join("\n") : "";
      byId("checkout-proxy").value = proxyText;
      byId("update-proxy").value = proxyText;
      saveFormPreferences();
      status.textContent = `已读取并保存 ${data.count || 0} 条（不同线路 ${data.unique_count || 0} 条）`;
    } catch (error) {
      status.textContent = error.message || "代理订阅读取失败";
    } finally {
      button.disabled = false;
    }
  }

  function restoreFormPreferences() {
    try {
      const raw = localStorage.getItem(FORM_PREFERENCES_KEY);
      if (!raw) return;
      const preferences = JSON.parse(raw);
      if (!preferences || typeof preferences !== "object") return;
      if (typeof preferences.country === "string") byId("country").value = preferences.country;
      if (typeof preferences.payment_method === "string") byId("payment-method").value = preferences.payment_method;
      if (typeof preferences.checkout_proxy === "string") byId("checkout-proxy").value = preferences.checkout_proxy;
      if (typeof preferences.update_proxy === "string") byId("update-proxy").value = preferences.update_proxy;
      if (byId("proxy-source-url") && typeof preferences.proxy_source_url === "string") byId("proxy-source-url").value = preferences.proxy_source_url;
      const checkboxPreferences = [
        ["apply_checkout_update", "apply-update"],
        ["rotate_checkout_proxy", "rotate-checkout-proxy"],
        ["rotate_update_proxy", "rotate-update-proxy"],
        ["oaics_only", "oaics-only"],
      ];
      checkboxPreferences.forEach(([key, id]) => {
        if (typeof preferences[key] === "boolean") byId(id).checked = preferences[key];
      });
    } catch (error) {
      // Ignore malformed or unavailable browser storage and keep the defaults.
    }
    updateProxyCounts();
  }

  function restoreTaskViewMode() {
    try {
      const saved = localStorage.getItem(TASK_VIEW_MODE_KEY);
      taskViewMode = saved === "list" ? "list" : "card";
    } catch (error) {
      taskViewMode = "card";
    }
  }

  function saveTaskViewMode() {
    try {
      localStorage.setItem(TASK_VIEW_MODE_KEY, taskViewMode);
    } catch (error) {
      // Keep the selected view for the current page when storage is unavailable.
    }
  }

  function setTaskViewMode(mode) {
    taskViewMode = mode === "list" ? "list" : "card";
    saveTaskViewMode();
    elements.viewToggle.querySelectorAll("[data-view-mode]").forEach(button => {
      const active = button.dataset.viewMode === taskViewMode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    renderTasks();
  }

  function formOverrides() {
    const result = {
      apply_checkout_update: byId("apply-update").checked,
      oaics_only: byId("oaics-only").checked,
    };
    const values = [
      ["country", byId("country").value],
      ["payment_method", byId("payment-method").value],
      ["checkout_proxy", byId("checkout-proxy").value.trim()],
      ["update_proxy", byId("update-proxy").value.trim()],
    ];
    values.forEach(([key, value]) => {
      if (value) result[key] = value;
    });
    return result;
  }

  function buildTaskPayload(
    credential,
    overrides = formOverrides(),
    rotateCheckout = byId("rotate-checkout-proxy").checked,
    rotateUpdate = byId("rotate-update-proxy").checked,
  ) {
    const payload = { ...parseCredentialInput(credential), ...overrides };
    if (payload.checkout_proxy) {
      payload.checkout_proxy = selectProxyFromPool(payload.checkout_proxy, "checkout");
      if (rotateCheckout) payload.checkout_proxy = rotateCheckoutProxy(payload.checkout_proxy);
    }
    if (payload.update_proxy) {
      payload.update_proxy = selectProxyFromPool(payload.update_proxy, "update");
      if (rotateUpdate) payload.update_proxy = rotateUpdateProxy(payload.update_proxy);
    }
    return payload;
  }

  function generateSessionId(length) {
    const minimum = 10 ** (length - 1);
    const range = 9 * minimum;
    return String(Math.floor(minimum + Math.random() * range));
  }

  function generateSessionToken(length) {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let token = "";
    for (let index = 0; index < length; index += 1) {
      token += alphabet[Math.floor(Math.random() * alphabet.length)];
    }
    return token;
  }

  function rotateProxySession(proxy) {
    const value = String(proxy || "");
    const match = value.match(/^([a-z][a-z\d+.-]*:\/\/)([^/@]+)(@.*)$/i);
    if (!match) return value;
    const sidMatch = match[2].match(/(^|-)sid-([A-Za-z0-9]+)(?=-|:)/i);
    if (sidMatch) {
      const sidPrefix = sidMatch[0].slice(0, -sidMatch[2].length);
      const rotatedUserInfo = match[2].replace(
        sidMatch[0],
        `${sidPrefix}${generateSessionToken(sidMatch[2].length)}`,
      );
      return `${match[1]}${rotatedUserInfo}${match[3]}`;
    }
    const sessionMatch = match[2].match(/-(\d+)$/);
    if (!sessionMatch) return value;
    const sessionId = generateSessionId(sessionMatch[1].length);
    return `${match[1]}${match[2].slice(0, -sessionMatch[1].length)}${sessionId}${match[3]}`;
  }

  function rotateCheckoutProxy(proxy) {
    return rotateProxySession(proxy);
  }

  function rotateUpdateProxy(proxy) {
    return rotateProxySession(proxy);
  }

  function setFormError(message) {
    elements.formError.textContent = message || "";
  }

  function updateCredentialPreview() {
    const inspection = inspectCredentialInput(elements.credentialInput.value);
    elements.credentialStatus.textContent = inspection.message;
    elements.credentialStatus.classList.toggle("valid", inspection.valid);
    elements.credentialStatus.classList.toggle("invalid", !inspection.valid && Boolean(elements.credentialInput.value.trim()));
    elements.extractTokenButton.hidden = !inspection.isJson;
    elements.extractTokenButton.disabled = !inspection.valid;
    elements.copyTokenButton.disabled = !inspection.valid;
    elements.submitButton.disabled = !inspection.valid;
    elements.accountPreview.hidden = !inspection.accountEmail;
    elements.accountEmail.textContent = inspection.accountEmail || "";
  }

  function extractTokenToInput() {
    const inspection = inspectCredentialInput(elements.credentialInput.value);
    if (!inspection.valid || !inspection.accessToken) return;
    elements.credentialInput.value = inspection.accessToken;
    updateCredentialPreview();
    elements.credentialInput.focus();
  }

  async function copyAccessToken() {
    const inspection = inspectCredentialInput(elements.credentialInput.value);
    if (!inspection.valid || !inspection.accessToken) return;
    try {
      await navigator.clipboard.writeText(inspection.accessToken);
      setFormError("access_token 已复制");
      window.setTimeout(() => setFormError(""), 1800);
    } catch (error) {
      setFormError("复制失败，请手动复制 access_token");
    }
  }

  async function loadExistingTasks() {
    const response = await apiFetch("/api/tasks");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "历史任务加载失败");
    (data.tasks || []).forEach(snapshot => {
      const task = ensureTask(snapshot.task_id, Date.parse(snapshot.created_at || "") || Date.now());
      if (!task) return;
      Object.assign(task, snapshot, {
        createdAt: Date.parse(snapshot.created_at || "") || task.createdAt,
        updatedAt: Date.parse(snapshot.finished_at || snapshot.started_at || snapshot.created_at || "") || Date.now(),
      });
      task.progress = clampProgress(snapshot.progress ?? task.progress);
      if (snapshot.checkout_proxy) taskCheckoutProxies.set(snapshot.task_id, snapshot.checkout_proxy);
    });
    renderTasks();
  }

  async function submitTaskRequest(payload) {
    const response = await apiFetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "任务提交失败");
    return data;
  }

  function registerSubmittedTask(data, payload) {
    const task = ensureTask(data.task_id, Date.parse(data.created_at || "") || Date.now());
    Object.assign(task, data, {
      createdAt: Date.parse(data.created_at || "") || task.createdAt,
      updatedAt: Date.now(),
    });
    if (payload.checkout_proxy) taskCheckoutProxies.set(data.task_id, payload.checkout_proxy);
    if (payload.update_proxy) taskUpdateProxies.set(data.task_id, payload.update_proxy);
    return task;
  }

  async function submitTask(event) {
    event.preventDefault();
    setFormError("");
    elements.submitButton.disabled = true;
    try {
      const payload = buildTaskPayload(elements.credentialInput.value);
      registerSubmittedTask(await submitTaskRequest(payload), payload);
      elements.credentialInput.value = "";
      updateCredentialPreview();
      renderTasks();
    } catch (error) {
      setFormError(error.message || "任务提交失败");
    } finally {
      updateCredentialPreview();
    }
  }

  function batchEntryStatusLabel(status) {
    return {
      ready: "待提交",
      invalid: "无效",
      duplicate: "重复",
      submitting: "提交中",
      submitted: "已提交",
      failed: "提交失败",
    }[status] || status;
  }

  function batchEntryClass(status) {
    return `batch-status-${status}`;
  }

  function batchImportSummary() {
    const counts = batchImportEntries.reduce((result, entry) => {
      result[entry.status] = (result[entry.status] || 0) + 1;
      return result;
    }, {});
    const total = batchImportEntries.length;
    const ready = (counts.ready || 0) + (counts.submitting || 0) + (counts.submitted || 0);
    const submitted = counts.submitted || 0;
    const failed = counts.failed || 0;
    if (!total) return "粘贴账号后点击“检查账号”";
    const parts = [`共 ${total} 条`, `有效 ${ready} 条`, `重复 ${counts.duplicate || 0} 条`, `无效 ${counts.invalid || 0} 条`];
    if (submitted || failed) parts.push(`已提交 ${submitted} 条`, `提交失败 ${failed} 条`);
    return parts.join("，");
  }

  function renderBatchImportResults() {
    elements.batchImportSummary.textContent = batchImportSummary();
    const readyCount = batchImportEntries.filter(entry => entry.status === "ready").length;
    const failedCount = batchImportEntries.filter(entry => entry.status === "failed").length;
    elements.batchSubmitButton.disabled = batchImportSubmitting || !batchImportValidated || readyCount === 0;
    elements.batchValidateButton.disabled = batchImportSubmitting;
    elements.batchImportInput.disabled = batchImportSubmitting;
    elements.batchImportCloseButton.disabled = batchImportSubmitting;
    elements.batchImportCompletion.hidden = !batchImportFinished;
    elements.batchImportCompletion.textContent = failedCount
      ? `提交完成，有 ${failedCount} 条失败`
      : "全部提交完成";
    if (!batchImportEntries.length) {
      elements.batchImportResults.innerHTML = '<p class="quiet batch-import-empty">检查结果会显示在这里</p>';
      return;
    }
    elements.batchImportResults.innerHTML = batchImportEntries.map(entry => {
      const account = entry.accountEmail || "账号未解析";
      const detail = entry.taskId
        ? `${entry.message || "任务已创建"}（${entry.taskId.slice(0, 12)}）`
        : entry.message || "";
      return `<div class="batch-import-result ${batchEntryClass(entry.status)}"><span class="batch-import-line">第 ${entry.lineNumber} 行</span><div class="batch-import-entry"><strong>${escapeHtml(account)}</strong><span>${escapeHtml(detail)}</span></div><span class="batch-import-status">${escapeHtml(batchEntryStatusLabel(entry.status))}</span></div>`;
    }).join("");
  }

  function resetBatchImport() {
    batchImportEntries = [];
    batchImportValidated = false;
    batchImportSubmitting = false;
    batchImportFinished = false;
    if (!elements.batchImportInput) return;
    elements.batchImportInput.value = "";
    renderBatchImportResults();
  }

  function openBatchImport() {
    closeTaskDetails();
    resetBatchImport();
    elements.batchImportModal.hidden = false;
    document.body.classList.add("modal-open");
    elements.batchImportInput.focus();
  }

  function closeBatchImport() {
    if (batchImportSubmitting) return;
    elements.batchImportModal.hidden = true;
    document.body.classList.remove("modal-open");
    resetBatchImport();
  }

  function validateBatchImport() {
    if (batchImportSubmitting) return;
    const lines = String(elements.batchImportInput.value || "")
      .split(/\r\n|\n/)
      .map((raw, index) => ({ raw: raw.trim(), lineNumber: index + 1 }))
      .filter(entry => entry.raw);
    const seenAccounts = new Map();
    batchImportEntries = lines.map(entry => {
      try {
        const inspection = inspectCredentialInput(entry.raw);
        if (!inspection.valid) {
          return { ...entry, status: "invalid", message: inspection.message, accountEmail: "" };
        }
        const accountEmail = inspection.accountEmail || "";
        const accountKey = accountEmail.toLowerCase();
        if (seenAccounts.has(accountKey)) {
          return {
            ...entry,
            status: "duplicate",
            accountEmail,
            message: `与第 ${seenAccounts.get(accountKey)} 行账号重复`,
          };
        }
        const payload = parseCredentialInput(entry.raw);
        seenAccounts.set(accountKey, entry.lineNumber);
        return { ...entry, status: "ready", accountEmail, payload, message: "校验通过" };
      } catch (error) {
        return { ...entry, status: "invalid", message: error.message || "账号校验失败", accountEmail: "" };
      }
    });
    batchImportValidated = true;
    renderBatchImportResults();
  }

  async function submitBatchImport() {
    if (batchImportSubmitting || !batchImportValidated) return;
    const pending = batchImportEntries.filter(entry => entry.status === "ready");
    if (!pending.length) return;
    const overrides = formOverrides();
    const rotateCheckout = byId("rotate-checkout-proxy").checked;
    const rotateUpdate = byId("rotate-update-proxy").checked;
    batchImportSubmitting = true;
    batchImportFinished = false;
    renderBatchImportResults();
    for (const entry of pending) {
      entry.status = "submitting";
      entry.message = "正在创建任务";
      renderBatchImportResults();
      try {
        const payload = buildTaskPayload(entry.raw, overrides, rotateCheckout, rotateUpdate);
        const data = await submitTaskRequest(payload);
        registerSubmittedTask(data, payload);
        entry.status = "submitted";
        entry.taskId = data.task_id;
        entry.message = "任务已创建";
      } catch (error) {
        entry.status = "failed";
        entry.message = error.message || "任务提交失败";
      }
      renderTasks();
      renderBatchImportResults();
    }
    batchImportSubmitting = false;
    batchImportFinished = true;
    renderBatchImportResults();
  }

  function ensureTask(taskId, createdAt = Date.now()) {
    if (!taskId) return null;
    if (!tasks.has(taskId)) {
      tasks.set(taskId, {
        task_id: taskId,
        status: "queued",
        stage: "queued",
        progress: 0,
        message: "等待执行",
        createdAt,
        updatedAt: Date.now(),
      });
    }
    return tasks.get(taskId);
  }

  function reduceTaskEvent(event) {
    if (!event || event.type === "task.ping" || !event.task_id) return;
    if (event.type === "task.deleted") {
      tasks.delete(event.task_id);
      taskCheckoutProxies.delete(event.task_id);
      taskUpdateProxies.delete(event.task_id);
      revealedProxyTasks.delete(event.task_id);
      selectedTaskIds.delete(event.task_id);
      if (detailTaskId === event.task_id) closeTaskDetails();
      renderTasks();
      return;
    }
    const eventTime = Date.parse(event.timestamp || "") || Date.now();
    const task = ensureTask(event.task_id, eventTime);
    const data = event.data || {};
    task.updatedAt = eventTime;
    if (event.type === "task.created" || event.type === "task.started") {
      task.status = data.status || (event.type === "task.started" ? "running" : "queued");
      task.stage = event.type === "task.started" ? "running" : task.stage;
      task.progress = clampProgress(data.progress ?? task.progress);
      task.account_email = data.account_email || task.account_email || "";
      task.payment_method = data.payment_method || task.payment_method || "";
      task.billing_country = data.billing_country || task.billing_country || "";
      task.retry_of = data.retry_of || task.retry_of || "";
    } else if (event.type === "task.checkout_detected") {
      task.session_kind = data.session_kind || task.session_kind || "";
      task.progress = clampProgress(data.progress ?? task.progress);
      task.message = `已识别 ${checkoutKindLabel(task.session_kind)} 链接`;
    } else if (event.type === "task.stage") {
      task.status = data.status || task.status;
      task.stage = data.stage || task.stage;
      task.progress = clampProgress(data.progress ?? task.progress);
      task.message = stageLabel(task.stage);
    } else if (event.type === "task.log") {
      task.message = data.message || task.message;
    } else if (event.type === "task.cancel_requested") {
      task.status = "cancel_requested";
      task.message = "正在取消";
    } else if (event.type === "task.succeeded") {
      task.status = "succeeded";
      task.stage = "completed";
      task.progress = clampProgress(data.progress ?? 100);
      task.result = data.result || null;
      task.checkout_proxy = data.checkout_proxy || task.checkout_proxy || "";
      task.account_email = task.account_email || task.result?.account_email || "";
      task.session_kind = task.session_kind || task.result?.session_kind || "";
      task.finishedAt = eventTime;
      task.message = "任务完成";
    } else if (event.type === "task.failed") {
      task.status = "failed";
      task.stage = "failed";
      task.progress = clampProgress(data.progress ?? task.progress);
      task.message = "任务失败";
      task.error = data.error || "任务失败";
      task.network_error = Boolean(data.network_error);
    } else if (event.type === "task.cancelled") {
      task.status = "cancelled";
      task.stage = "cancelled";
      task.progress = clampProgress(data.progress ?? task.progress);
      task.message = "任务已取消";
    }
    renderTasks();
  }

  function matchesFilter(task) {
    if (taskFilter === "all") return true;
    if (taskFilter === "running") return !TERMINAL_STATES.has(task.status);
    if (taskFilter === "succeeded") return task.status === "succeeded";
    if (taskFilter === "failed") return task.status === "failed" || task.status === "cancelled";
    return true;
  }

  function setTaskFilter(filter) {
    taskFilter = filter;
    elements.taskFilters.querySelectorAll("[data-filter]").forEach(button => {
      const active = button.dataset.filter === taskFilter;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    renderTasks();
  }

  function emptyStateLabel() {
    return {
      all: "还没有任务",
      running: "暂无运行中的任务",
      succeeded: "暂无成功任务",
      failed: "暂无失败或取消任务",
    }[taskFilter] || "还没有任务";
  }

  function stageLabel(stage) {
    const labels = {
      queued: "等待执行", running: "开始执行", eligibility_check: "检查优惠资格", checkout: "创建 Checkout",
      checkout_update: "更新 Checkout", stripe_init: "初始化支付", elements_session: "准备支付方式",
      taxes: "同步税费", payment_confirmation: "确认支付方式", redirect_resolution: "解析跳转链接",
      completed: "任务完成", cancelled: "任务已取消", failed: "任务失败",
    };
    return labels[stage] || stage || "处理中";
  }

  function clampProgress(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(0, Math.min(100, Math.round(numeric)));
  }

  function connectTaskSocket() {
    // Password protection can be disabled. An empty password is still a valid
    // WebSocket auth payload, so only gate connection on completed HTTP auth.
    if (!authReady) return;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${window.location.host}/ws/tasks`);
    socket.addEventListener("open", function () {
      reconnectAttempt = 0;
      socket.send(JSON.stringify({ type: "auth", password: authPassword }));
    });
    socket.addEventListener("message", function (event) {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "auth.ok") {
          setConnection(true);
          return;
        }
        if (message.type === "auth.failed") {
          lockWorkbench("密码错误，请重新登录");
          return;
        }
        reduceTaskEvent(message);
      } catch (error) { /* ignore malformed event */ }
    });
    socket.addEventListener("close", function () {
      setConnection(false);
      if (authReady) scheduleReconnect();
    });
    socket.addEventListener("error", function () { setConnection(false); });
  }

  function scheduleReconnect() {
    if (!authReady || reconnectTimer) return;
    const delay = Math.min(10000, 500 * Math.pow(2, reconnectAttempt++));
    reconnectTimer = window.setTimeout(function () {
      reconnectTimer = null;
      connectTaskSocket();
    }, delay);
  }

  function setConnection(online) {
    elements.socketStatus.classList.toggle("online", online);
    elements.socketStatus.classList.toggle("offline", !online);
    elements.socketStatus.querySelector("span:last-child").textContent = online ? "实时连接" : "等待重连";
  }

  async function cancelTask(taskId) {
    try {
      const response = await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "取消失败");
      const task = ensureTask(taskId);
      task.status = data.status || "cancel_requested";
      task.message = task.status === "cancelled" ? "任务已取消" : "正在取消";
      renderTasks();
    } catch (error) {
      setFormError(error.message || "取消失败");
    }
  }

  async function performTaskRetry(taskId, rotateCheckoutIp, rotateUpdateIp) {
    const checkoutProxyInput = byId("checkout-proxy").value.trim();
    const updateProxyInput = byId("update-proxy").value.trim();
    if (rotateCheckoutIp && !checkoutProxyInput) {
      throw new Error("请先填写新的 Checkout Proxy");
    }
    if (rotateUpdateIp && byId("apply-update").checked && !updateProxyInput) {
      throw new Error("请先填写新的 Update Proxy");
    }
    const originalProxy = rotateCheckoutIp
      ? selectProxyFromPool(checkoutProxyInput, "checkout")
      : (taskCheckoutProxies.get(taskId) || checkoutProxyInput);
    const originalUpdateProxy = rotateUpdateIp
      ? selectProxyFromPool(updateProxyInput, "update")
      : (taskUpdateProxies.get(taskId) || updateProxyInput);
    const payload = {};
    if (originalProxy) {
      payload.checkout_proxy = rotateCheckoutIp ? rotateCheckoutProxy(originalProxy) : originalProxy;
    }
    if (originalUpdateProxy) {
      payload.update_proxy = rotateUpdateIp ? rotateUpdateProxy(originalUpdateProxy) : originalUpdateProxy;
    }
    const response = await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "重试失败");
    removeTaskLocally(taskId);
    const retriedTask = ensureTask(data.task_id, Date.parse(data.created_at || "") || Date.now());
    Object.assign(retriedTask, data, {
      createdAt: Date.parse(data.created_at || "") || retriedTask.createdAt,
      updatedAt: Date.now(),
    });
    if (payload.checkout_proxy) taskCheckoutProxies.set(data.task_id, payload.checkout_proxy);
    if (payload.update_proxy) taskUpdateProxies.set(data.task_id, payload.update_proxy);
    return data;
  }

  async function retryTask(taskId, rotateCheckoutIp, rotateUpdateIp) {
    try {
      await performTaskRetry(taskId, rotateCheckoutIp, rotateUpdateIp);
      renderTasks();
    } catch (error) {
      setFormError(error.message || "重试失败");
    }
  }

  function networkFailedTasks() {
    return Array.from(tasks.values()).filter(task => task.status === "failed" && task.network_error);
  }

  function updateBulkNetworkRetryControl() {
    const count = networkFailedTasks().length;
    elements.retryNetworkFailedTasksButton.disabled = count === 0 || bulkNetworkRetryPending;
    elements.retryNetworkFailedTasksButton.textContent = bulkNetworkRetryPending
      ? "正在重试网络失败..."
      : `重试网络失败${count ? `（${count}）` : ""}`;
  }

  async function retryAllNetworkFailedTasks() {
    const pending = networkFailedTasks();
    if (!pending.length || bulkNetworkRetryPending) return;
    if (!window.confirm(`确认重试 ${pending.length} 个网络失败任务吗？原任务将被删除。`)) return;
    bulkNetworkRetryPending = true;
    renderTasks();
    let succeeded = 0;
    let failed = 0;
    for (const task of pending) {
      if (!tasks.has(task.task_id)) continue;
      try {
        await performTaskRetry(task.task_id, true, true);
        succeeded += 1;
      } catch (error) {
        failed += 1;
      }
      renderTasks();
    }
    bulkNetworkRetryPending = false;
    renderTasks();
    const message = failed
      ? `已重试 ${succeeded} 个网络失败任务，${failed} 个重试失败`
      : `已重试 ${succeeded} 个网络失败任务`;
    setFormError(message);
    window.setTimeout(() => setFormError(""), 2500);
  }

  async function deleteTask(taskId) {
    try {
      const response = await apiFetch(`/api/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "删除失败");
      tasks.delete(taskId);
      taskCheckoutProxies.delete(taskId);
      revealedProxyTasks.delete(taskId);
      selectedTaskIds.delete(taskId);
      if (detailTaskId === taskId) closeTaskDetails();
      renderTasks();
    } catch (error) {
      setFormError(error.message || "删除失败");
    }
  }

  async function copyResult(url, button) {
    try {
      await navigator.clipboard.writeText(url);
      const previousTimer = copyFeedbackTimers.get(button);
      if (previousTimer) window.clearTimeout(previousTimer);
      button.dataset.copyLabel = button.dataset.copyLabel || button.textContent;
      button.textContent = "复制成功";
      button.classList.add("copy-success");
      const timer = window.setTimeout(() => {
        button.textContent = button.dataset.copyLabel || "复制";
        button.classList.remove("copy-success");
        copyFeedbackTimers.delete(button);
      }, 1800);
      copyFeedbackTimers.set(button, timer);
      setFormError("链接已复制");
      window.setTimeout(() => setFormError(""), 1800);
    } catch (error) {
      setFormError("复制失败，请手动复制链接");
    }
  }

  async function copyValue(value, successMessage, failureMessage) {
    try {
      await navigator.clipboard.writeText(value);
      setFormError(successMessage);
      window.setTimeout(() => setFormError(""), 1800);
    } catch (error) {
      setFormError(failureMessage);
    }
  }

  async function testProxy(taskId) {
    const task = tasks.get(taskId);
    const checkoutProxy = taskCheckoutProxies.get(taskId) || task?.checkout_proxy || "";
    if (!task || !checkoutProxy) return;
    task.proxyTest = { status: "testing" };
    renderTasks();
    try {
      const response = await apiFetch("/api/proxy/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ checkout_proxy: checkoutProxy }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "代理 IP 测试失败");
      const current = tasks.get(taskId);
      if (!current) return;
      current.proxyTest = { status: "succeeded", ...data };
    } catch (error) {
      const current = tasks.get(taskId);
      if (!current) return;
      current.proxyTest = { status: "failed", error: error.message || "代理 IP 测试失败" };
    }
    renderTasks();
  }

  function renderTaskActions(task, { includeDelete = true } = {}) {
    const deleteAction = includeDelete && DELETABLE_STATES.has(task.status)
      ? `<button class="secondary" data-delete="${escapeHtml(task.task_id)}">删除</button>`
      : "";
    const canRetrySucceeded = task.status === "succeeded" && hasNonZeroTaskAmount(task);
    if (task.status === "failed" || task.status === "cancelled" || canRetrySucceeded) {
      return `<div class="task-actions"><label class="retry-option"><input type="checkbox" data-retry-rotate="${escapeHtml(task.task_id)}" data-retry-checkout-rotate="${escapeHtml(task.task_id)}" checked><span>轮换 Checkout IP</span></label><label class="retry-option"><input type="checkbox" data-retry-update-rotate="${escapeHtml(task.task_id)}" checked><span>轮换 Update IP</span></label><button class="secondary" data-retry="${escapeHtml(task.task_id)}">重试</button>${deleteAction}</div>`;
    }
    if (deleteAction) return deleteAction;
    if (TERMINAL_STATES.has(task.status)) return "";
    return `<button class="secondary" data-cancel="${escapeHtml(task.task_id)}">取消</button>`;
  }

  function hasNonZeroTaskAmount(task) {
    const result = task.result || {};
    const amount = result.amount_due_minor ?? result.amount_due;
    return amount !== undefined && amount !== null && amount !== ""
      && Number.isFinite(Number(amount)) && Number(amount) !== 0;
  }

  function taskAmount(result) {
    const amount = result.amount_due == null ? "" : `${result.amount_due} ${result.currency || ""}`.trim();
    return amount || "—";
  }

  function taskCheckoutType(task, result) {
    return checkoutKindLabel(task.session_kind || result.session_kind || "");
  }

  function taskResultUrl(result) {
    return result.provider_url || result.paypal_url || result.gopay_url || result.gcash_url || "";
  }

  function succeededTasks() {
    return Array.from(tasks.values()).filter(task => task.status === "succeeded");
  }

  function pruneSelectedTasks() {
    const succeededIds = new Set(succeededTasks().map(task => task.task_id));
    selectedTaskIds.forEach(taskId => {
      if (!succeededIds.has(taskId)) selectedTaskIds.delete(taskId);
    });
  }

  function renderTaskSelection(task, className = "") {
    if (task.status !== "succeeded") return "";
    const checked = selectedTaskIds.has(task.task_id) ? " checked" : "";
    return `<label class="task-select ${className}" title="选择导出"><input type="checkbox" data-task-select="${escapeHtml(task.task_id)}" aria-label="选择导出 ${escapeHtml(task.task_id.slice(0, 12))}"${checked}></label>`;
  }

  function updateExportControls() {
    pruneSelectedTasks();
    const total = succeededTasks().length;
    const selected = selectedTaskIds.size;
    elements.selectedTaskCount.textContent = `已选 ${selected} 个`;
    elements.exportCsvButton.disabled = selected === 0;
    elements.selectAllSucceeded.disabled = total === 0;
    elements.selectAllSucceeded.checked = total > 0 && selected === total;
    elements.selectAllSucceeded.indeterminate = selected > 0 && selected < total;
  }

  function updateBulkDeleteControls() {
    const failedCount = Array.from(tasks.values()).filter(task => task.status === "failed" || task.status === "cancelled").length;
    const succeededCount = Array.from(tasks.values()).filter(task => task.status === "succeeded").length;
    elements.clearFailedTasksButton.disabled = failedCount === 0 || bulkDeletePending.has("failed");
    elements.clearSucceededTasksButton.disabled = succeededCount === 0 || bulkDeletePending.has("succeeded");
  }

  function removeTaskLocally(taskId) {
    tasks.delete(taskId);
    taskCheckoutProxies.delete(taskId);
    taskUpdateProxies.delete(taskId);
    revealedProxyTasks.delete(taskId);
    selectedTaskIds.delete(taskId);
    if (detailTaskId === taskId) closeTaskDetails();
  }

  async function bulkDeleteTasks(target) {
    const isFailedTarget = target === "failed";
    const count = Array.from(tasks.values()).filter(task => isFailedTarget
      ? task.status === "failed" || task.status === "cancelled"
      : task.status === "succeeded").length;
    if (!count || bulkDeletePending.has(target)) return;
    const label = isFailedTarget ? "失败或已取消" : "已完成";
    if (!window.confirm(`确认清空 ${count} 个${label}任务吗？此操作不可恢复。`)) return;
    bulkDeletePending.add(target);
    renderTasks();
    try {
      const response = await apiFetch("/api/tasks/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "批量清空失败");
      (data.task_ids || []).forEach(removeTaskLocally);
      renderTasks();
      setFormError(`已清空 ${data.deleted_count || 0} 个${label}任务`);
      window.setTimeout(() => setFormError(""), 1800);
    } catch (error) {
      setFormError(error.message || "批量清空失败");
    } finally {
      bulkDeletePending.delete(target);
      renderTasks();
    }
  }

  function toggleTaskSelection(taskId, selected) {
    const task = tasks.get(taskId);
    if (!task || task.status !== "succeeded") return;
    if (selected) selectedTaskIds.add(taskId);
    else selectedTaskIds.delete(taskId);
    updateExportControls();
  }

  function toggleAllSucceeded(selected) {
    if (selected) succeededTasks().forEach(task => selectedTaskIds.add(task.task_id));
    else selectedTaskIds.clear();
    renderTasks();
  }

  function csvCell(value) {
    return `"${String(value == null ? "" : value).replaceAll('"', '""')}"`;
  }

  function taskCompletionTime(task) {
    return task.finishedAt || Date.parse(task.finished_at || "") || task.updatedAt;
  }

  function taskCsvRow(task) {
    const result = task.result || {};
    const url = taskResultUrl(result);
    const account = task.account_email || result.account_email || "";
    return [
      result.amount_due,
      result.currency,
      account,
      url,
      task.payment_method || result.payment_method,
      taskCheckoutType(task, result),
      task.billing_country || result.billing_country,
      result.checkout_session_id,
      result.payment_method_id,
      result.stripe_redirect_url,
      formatDateTime(task.createdAt),
      formatDateTime(taskCompletionTime(task)),
      task.task_id,
    ];
  }

  function csvFileTimestamp(value = new Date()) {
    const pad = number => String(number).padStart(2, "0");
    return `${value.getFullYear()}${pad(value.getMonth() + 1)}${pad(value.getDate())}-${pad(value.getHours())}${pad(value.getMinutes())}${pad(value.getSeconds())}`;
  }

  function downloadSelectedCsv() {
    const selected = succeededTasks().filter(task => selectedTaskIds.has(task.task_id));
    if (!selected.length) {
      updateExportControls();
      return;
    }
    const rows = [CSV_HEADERS, ...selected.map(taskCsvRow)];
    const csv = `\ufeff${rows.map(row => row.map(csvCell).join(",")).join("\r\n")}\r\n`;
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `payment-links-${csvFileTimestamp()}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }

  function renderResultRow(url) {
    return `<div class="result-row"><div class="result-content"><span class="result-label">提取链接</span><a class="result-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a></div><button class="primary" data-copy="${escapeHtml(url)}">复制</button></div>`;
  }

  function isFailedTask(task) {
    return task.status === "failed" || task.status === "cancelled";
  }

  function taskFailureReason(task) {
    return task.error || (task.status === "cancelled" ? "任务已取消" : "任务失败");
  }

  function renderTaskError(task) {
    if (!isFailedTask(task)) return "";
    const reason = taskFailureReason(task);
    return `<p class="task-message error-text" title="${escapeHtml(reason)}">${escapeHtml(reason)}</p>`;
  }

  function renderNetworkErrorTag(task) {
    return task.network_error
      ? '<span class="status status-network" title="网络异常，建议重试">网络错误</span>'
      : "";
  }

  function renderTask(task) {
    const result = task.result || {};
    const url = taskResultUrl(result);
    const account = task.account_email || result.account_email || "账号未解析";
    const checkoutProxy = task.checkout_proxy || result.checkout_proxy || "";
    const sessionKind = task.session_kind || result.session_kind || "";
    const sessionKindHtml = sessionKind
      ? `<span class="checkout-kind">${escapeHtml(checkoutKindLabel(sessionKind))}</span>`
      : "";
    const amountMinor = result.amount_due_minor ?? result.amount_due;
    const hasAmount = amountMinor !== undefined && amountMinor !== null && amountMinor !== "";
    const nonZeroAmountTag = task.status === "succeeded" && hasAmount && Number.isFinite(Number(amountMinor)) && Number(amountMinor) !== 0
      ? '<span class="status status-nonzero">非0元订单</span>'
      : "";
    const progress = clampProgress(task.progress);
    const progressTone = task.status === "succeeded"
      ? "success"
      : task.status === "failed" || task.status === "cancelled"
      ? "error"
      : "active";
    const action = renderTaskActions(task);
    const isComplete = task.status === "succeeded";
    const isFailed = isFailedTask(task);
    const hasResult = isComplete && Boolean(url);
    const progressHtml = isComplete || isFailed
      ? ""
      : `<div class="task-progress progress-${progressTone}" aria-label="任务进度 ${progress}%"><div class="task-progress-header"><span>${escapeHtml(stageLabel(task.stage))}</span><strong>${progress}%</strong></div><div class="task-progress-track"><span class="task-progress-bar" style="width: ${progress}%"></span></div></div>`;
    const resultHtml = hasResult ? renderResultRow(url) : "";
    const resultDetails = task.status === "succeeded" ? renderResultDetails(result, checkoutProxy, task.task_id, task.proxyTest) : "";
    const cardClass = task.status === "succeeded" ? "task-card task-card-success" : "task-card";
    return `<article class="${cardClass}" data-task-container>
      <div class="task-header"><div><strong>${escapeHtml(task.payment_method || result.payment_method || "支付任务")}</strong><div class="task-account">账号：${escapeHtml(account)}</div><div class="task-meta">${sessionKindHtml}<span class="status status-${escapeHtml(task.status)}">${escapeHtml(statusLabel(task.status))}</span>${renderNetworkErrorTag(task)}${nonZeroAmountTag}<span>${escapeHtml(task.message || stageLabel(task.stage))}</span></div></div><div class="task-header-actions">${renderTaskSelection(task)}<span class="task-id">${escapeHtml(task.task_id.slice(0, 12))}</span></div></div>
      ${progressHtml}
      ${renderTaskError(task)}
      ${resultHtml}
      ${resultDetails}
      <div class="task-footer"><span class="task-time">${escapeHtml(formatTime(task.updatedAt))}</span>${action}</div>
    </article>`;
  }

  function renderTaskRow(task) {
    const result = task.result || {};
    const url = taskResultUrl(result);
    const account = task.account_email || result.account_email || "账号未解析";
    const status = statusLabel(task.status);
    const progress = clampProgress(task.progress);
    const amount = taskAmount(result);
    const progressTone = task.status === "succeeded"
      ? "success"
      : task.status === "failed" || task.status === "cancelled"
      ? "error"
      : "active";
    const listDeleteAction = DELETABLE_STATES.has(task.status)
      ? `<button class="secondary task-row-delete" data-delete="${escapeHtml(task.task_id)}">删除</button>`
      : "";
    const isComplete = task.status === "succeeded";
    const isFailed = isFailedTask(task);
    const hasResult = isComplete && Boolean(url);
    const progressOrResult = isComplete
      ? (hasResult
      ? `<div class="task-row-result"><a class="result-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a><button class="primary" data-copy="${escapeHtml(url)}">复制</button></div>`
      : `<span class="task-row-result-empty">未返回链接</span>`)
      : isFailed
      ? `<span class="task-row-error" title="${escapeHtml(taskFailureReason(task))}">${escapeHtml(taskFailureReason(task))}</span>`
      : `<strong>${progress}% · ${escapeHtml(stageLabel(task.stage))}</strong><div class="task-progress-track"><span class="task-progress-bar" style="width: ${progress}%"></span></div>`;
    return `<article class="task-row progress-${progressTone}" data-task-container>
      <div class="task-row-field task-row-account"><span>账号</span><strong title="${escapeHtml(account)}">${escapeHtml(account)}</strong></div>
      <div class="task-row-field"><span>支付方式</span><strong>${escapeHtml(task.payment_method || result.payment_method || "支付任务")}</strong></div>
      <div class="task-row-field"><span>类型</span><strong>${escapeHtml(taskCheckoutType(task, result))}</strong></div>
      <div class="task-row-field task-row-status"><span>状态</span><div class="task-row-status-value"><strong class="status status-${escapeHtml(task.status)}">${escapeHtml(status)}</strong>${renderNetworkErrorTag(task)}</div></div>
      <div class="task-row-field ${isComplete ? "task-row-result-field" : isFailed ? "task-row-error-field" : "task-row-progress"}"><span>${isComplete ? "提取链接" : isFailed ? "错误原因" : "进度"}</span>${progressOrResult}</div>
      <div class="task-row-field"><span>金额</span><strong>${escapeHtml(amount)}</strong></div>
      <div class="task-row-field"><span>提交时间</span><strong>${escapeHtml(formatTime(task.createdAt))}</strong></div>
      <div class="task-row-actions">${renderTaskSelection(task)}<button class="primary" data-details="${escapeHtml(task.task_id)}">详情</button>${renderTaskActions(task, { includeDelete: false })}${listDeleteAction}</div>
    </article>`;
  }

  function renderResultDetails(result, checkoutProxy, taskId, proxyTest) {
    const amount = result.amount_due == null ? "" : `${result.amount_due} ${result.currency || ""}`.trim();
    const details = [
      ["Checkout 会话", result.checkout_session_id],
      ["会话类型", result.session_kind],
      ["账单国家", result.billing_country],
      ["金额 / 币种", amount],
      ["最小金额单位", result.amount_due_minor],
      ["支付方式 ID", result.payment_method_id],
      ["Stripe 跳转地址", result.stripe_redirect_url],
    ].filter(([, value]) => value !== undefined && value !== null && String(value) !== "");
    const proxyVisible = revealedProxyTasks.has(taskId);
    const testing = proxyTest?.status === "testing";
    const proxyHtml = checkoutProxy
      ? `<div class="detail-item detail-item-wide"><span>Checkout Proxy</span><div class="detail-value-action"><button class="secondary" data-test-proxy="${escapeHtml(taskId)}"${testing ? " disabled" : ""}>${testing ? "测试中..." : "测试 IP"}</button><button class="secondary" data-copy-proxy="${escapeHtml(checkoutProxy)}">复制</button><strong class="proxy-value" title="${proxyVisible ? escapeHtml(checkoutProxy) : "Checkout Proxy 已隐藏"}">${proxyVisible ? escapeHtml(checkoutProxy) : "已隐藏"}</strong><button class="secondary" data-toggle-proxy="${escapeHtml(taskId)}">${proxyVisible ? "隐藏" : "显示"}</button></div>${renderProxyTest(proxyTest)}</div>`
      : "";
    if (!details.length && !proxyHtml) return "";
    return `<div class="result-details">${proxyHtml}${details.map(([label, value]) => `<div class="detail-item"><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join("")}</div>`;
  }

  function renderModalDetail(label, value, wide = false) {
    if (value === undefined || value === null || String(value) === "") return "";
    return `<div class="detail-item${wide ? " detail-item-wide" : ""}"><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`;
  }

  function renderNetworkErrorDetail(task) {
    return task.network_error
      ? '<div class="detail-item"><span>错误类型</span><strong class="status status-network" title="网络异常，建议重试">网络错误</strong></div>'
      : "";
  }

  function renderTaskDetailsModal() {
    const task = detailTaskId ? tasks.get(detailTaskId) : null;
    if (!task) {
      closeTaskDetails();
      return;
    }
    const result = task.result || {};
    const account = task.account_email || result.account_email || "账号未解析";
    const progress = clampProgress(task.progress);
    const progressTone = task.status === "succeeded"
      ? "success"
      : task.status === "failed" || task.status === "cancelled"
      ? "error"
      : "active";
    const checkoutProxy = task.checkout_proxy || result.checkout_proxy || "";
    const url = taskResultUrl(result);
    const details = [
      renderModalDetail("任务 ID", task.task_id, true),
      renderModalDetail("账号", account),
      renderModalDetail("支付方式", task.payment_method || result.payment_method),
      renderModalDetail("Checkout 类型", taskCheckoutType(task, result)),
      renderModalDetail("状态", statusLabel(task.status)),
      renderNetworkErrorDetail(task),
      renderModalDetail("当前阶段", stageLabel(task.stage)),
      renderModalDetail("账单国家", task.billing_country || result.billing_country),
      renderModalDetail("金额 / 币种", taskAmount(result)),
      renderModalDetail("Checkout 会话", result.checkout_session_id),
      renderModalDetail("支付方式 ID", result.payment_method_id),
      renderModalDetail("Stripe 跳转地址", result.stripe_redirect_url, true),
      renderModalDetail("提交时间", formatDateTime(task.createdAt)),
    ].join("");
    const isComplete = task.status === "succeeded";
    const isFailed = isFailedTask(task);
    const hasResult = isComplete && Boolean(url);
    const progressHtml = isComplete || isFailed
      ? ""
      : `<div class="modal-progress progress-${progressTone}"><div class="task-progress-header"><span>${escapeHtml(stageLabel(task.stage))}</span><strong>${progress}%</strong></div><div class="task-progress-track"><span class="task-progress-bar" style="width: ${progress}%"></span></div></div>`;
    const resultHtml = hasResult ? renderResultRow(url) : "";
    elements.taskDetailsContent.innerHTML = `${progressHtml}<div class="result-details modal-detail-grid">${details}</div>${renderTaskError(task)}${resultHtml}${task.status === "succeeded" ? renderResultDetails(result, checkoutProxy, task.task_id, task.proxyTest) : ""}<div class="task-modal-actions" data-task-container>${renderTaskActions(task)}</div>`;
  }

  function openTaskDetails(taskId) {
    if (!tasks.has(taskId)) return;
    detailTaskId = taskId;
    renderTaskDetailsModal();
    elements.taskDetailsModal.hidden = false;
    document.body.classList.add("modal-open");
    elements.taskDetailsClose.focus();
  }

  function closeTaskDetails() {
    detailTaskId = "";
    if (elements.taskDetailsModal) elements.taskDetailsModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  function renderProxyTest(proxyTest) {
    if (!proxyTest || proxyTest.status === "testing") {
      return proxyTest?.status === "testing" ? '<div class="proxy-test-result testing">正在测试代理 IP...</div>' : "";
    }
    if (proxyTest.status === "failed") {
      return `<div class="proxy-test-result failed">${escapeHtml(proxyTest.error || "代理 IP 测试失败")}</div>`;
    }
    const country = [proxyTest.country, proxyTest.country_code].filter(Boolean).join(" ") || "未知";
    const region = [proxyTest.region, proxyTest.region_code].filter(Boolean).join(" ") || "未知";
    return `<div class="proxy-test-result succeeded"><span>IP：<strong>${escapeHtml(proxyTest.ip || "未知")}</strong></span><span>国家：<strong>${escapeHtml(country)}</strong></span><span>地区：<strong>${escapeHtml(region)}</strong></span></div>`;
  }

  function statusLabel(status) {
    return { queued: "排队中", running: "运行中", cancel_requested: "取消中", succeeded: "成功", failed: "失败", cancelled: "已取消" }[status] || status;
  }

  function checkoutKindLabel(sessionKind) {
    return sessionKind === "stripe_checkout" ? "CS" : sessionKind === "openai_custom_checkout" ? "OAICS" : sessionKind || "未知";
  }

  function formatTime(value) {
    if (!value) return "";
    return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatDateTime(value) {
    if (!value) return "";
    return new Date(value).toLocaleString([], {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function renderTasks() {
    const all = Array.from(tasks.values()).sort((a, b) => b.createdAt - a.createdAt);
    const visible = all.filter(matchesFilter);
    pruneSelectedTasks();
    const scrollTop = elements.taskList.scrollTop;
    const pageScrollX = window.scrollX;
    const pageScrollY = window.scrollY;
    const runningCount = all.filter(task => !TERMINAL_STATES.has(task.status)).length;
    const succeededCount = all.filter(task => task.status === "succeeded").length;
    const failedCount = all.filter(task => task.status === "failed" || task.status === "cancelled").length;
    const completedCount = succeededCount + failedCount;
    elements.taskCount.textContent = `${visible.length} 个任务`;
    elements.runningCount.textContent = String(runningCount);
    elements.succeededCount.textContent = String(succeededCount);
    elements.failedCount.textContent = String(failedCount);
    elements.successRate.textContent = completedCount
      ? `${((succeededCount / completedCount) * 100).toFixed(1)}%`
      : "—";
    elements.emptyState.textContent = emptyStateLabel();
    elements.emptyState.hidden = visible.length > 0;
    elements.taskList.classList.toggle("list-view", taskViewMode === "list");
    elements.taskList.querySelectorAll(".task-card, .task-row").forEach(card => card.remove());
    const renderer = taskViewMode === "list" ? renderTaskRow : renderTask;
    elements.taskList.insertAdjacentHTML("afterbegin", visible.map(renderer).join(""));
    elements.taskList.scrollTop = scrollTop;
    window.scrollTo(pageScrollX, pageScrollY);
    window.requestAnimationFrame(() => window.scrollTo(pageScrollX, pageScrollY));
    updateExportControls();
    updateBulkNetworkRetryControl();
    updateBulkDeleteControls();
    if (detailTaskId) renderTaskDetailsModal();
  }

  function handleTaskSelection(event) {
    const checkbox = event.target.closest("[data-task-select]");
    if (!checkbox) return;
    toggleTaskSelection(checkbox.dataset.taskSelect, checkbox.checked);
  }

  function handleTaskAction(event) {
    const details = event.target.closest("[data-details]");
    const cancel = event.target.closest("[data-cancel]");
    const retry = event.target.closest("[data-retry]");
    const remove = event.target.closest("[data-delete]");
    const toggleProxy = event.target.closest("[data-toggle-proxy]");
    const testProxyButton = event.target.closest("[data-test-proxy]");
    const copyProxy = event.target.closest("[data-copy-proxy]");
    const copy = event.target.closest("[data-copy]");
    if (details) openTaskDetails(details.dataset.details);
    if (cancel) cancelTask(cancel.dataset.cancel);
    if (retry) {
      const container = retry.closest("[data-task-container]");
      const checkoutOption = container?.querySelector("[data-retry-checkout-rotate]");
      const updateOption = container?.querySelector("[data-retry-update-rotate]");
      retryTask(
        retry.dataset.retry,
        checkoutOption ? checkoutOption.checked : true,
        updateOption ? updateOption.checked : true,
      );
    }
    if (remove) deleteTask(remove.dataset.delete);
    if (testProxyButton) testProxy(testProxyButton.dataset.testProxy);
    if (toggleProxy) {
      const taskId = toggleProxy.dataset.toggleProxy;
      if (revealedProxyTasks.has(taskId)) revealedProxyTasks.delete(taskId);
      else revealedProxyTasks.add(taskId);
      renderTasks();
    }
    if (copyProxy) copyValue(copyProxy.dataset.copyProxy, "Checkout Proxy 已复制", "复制失败，请手动复制 Proxy");
    if (copy) copyResult(copy.dataset.copy, copy);
  }

  function bindEvents() {
    elements.authForm.addEventListener("submit", function (event) {
      event.preventDefault();
      authenticate(elements.authPassword.value);
    });
    elements.logoutButton.addEventListener("click", logout);
    elements.taskForm.addEventListener("submit", submitTask);
    elements.credentialInput.addEventListener("input", updateCredentialPreview);
    ["country", "payment-method", "checkout-proxy", "update-proxy", "proxy-source-url"].forEach(id => {
      const field = byId(id);
      field.addEventListener("change", saveFormPreferences);
      field.addEventListener("input", saveFormPreferences);
    });
    byId("refresh-proxy-source").addEventListener("click", refreshProxySource);
    ["apply-update", "rotate-checkout-proxy", "rotate-update-proxy", "oaics-only"].forEach(id => {
      byId(id).addEventListener("change", saveFormPreferences);
    });
    elements.extractTokenButton.addEventListener("click", extractTokenToInput);
    elements.batchImportButton.addEventListener("click", openBatchImport);
    elements.batchImportInput.addEventListener("input", function () {
      if (batchImportSubmitting) return;
      batchImportEntries = [];
      batchImportValidated = false;
      renderBatchImportResults();
    });
    elements.batchValidateButton.addEventListener("click", validateBatchImport);
    elements.batchSubmitButton.addEventListener("click", submitBatchImport);
    elements.taskFilters.addEventListener("click", function (event) {
      const button = event.target.closest("[data-filter]");
      if (button) setTaskFilter(button.dataset.filter);
    });
    elements.viewToggle.addEventListener("click", function (event) {
      const button = event.target.closest("[data-view-mode]");
      if (button) setTaskViewMode(button.dataset.viewMode);
    });
    elements.taskList.addEventListener("click", handleTaskAction);
    elements.taskList.addEventListener("change", handleTaskSelection);
    elements.taskDetailsContent.addEventListener("click", handleTaskAction);
    elements.selectAllSucceeded.addEventListener("change", function (event) {
      toggleAllSucceeded(event.target.checked);
    });
    elements.exportCsvButton.addEventListener("click", downloadSelectedCsv);
    elements.retryNetworkFailedTasksButton.addEventListener("click", retryAllNetworkFailedTasks);
    elements.clearFailedTasksButton.addEventListener("click", () => bulkDeleteTasks("failed"));
    elements.clearSucceededTasksButton.addEventListener("click", () => bulkDeleteTasks("succeeded"));
    elements.taskDetailsClose.addEventListener("click", closeTaskDetails);
    elements.taskDetailsModal.addEventListener("click", function (event) {
      if (event.target.closest("[data-modal-close]")) closeTaskDetails();
    });
    elements.batchImportCloseButton.addEventListener("click", closeBatchImport);
    elements.batchImportModal.addEventListener("click", function (event) {
      if (event.target.closest("[data-batch-modal-close]")) closeBatchImport();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && detailTaskId) closeTaskDetails();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    elements.authGate = byId("auth-gate");
    elements.authForm = byId("auth-form");
    elements.authPassword = byId("auth-password");
    elements.authSubmit = byId("auth-submit");
    elements.authError = byId("auth-error");
    elements.workbench = byId("workbench");
    elements.logoutButton = byId("logout-button");
    elements.taskForm = byId("task-form");
    elements.credentialInput = byId("credential-input");
    elements.submitButton = byId("submit-button");
    elements.formError = byId("form-error");
    elements.credentialStatus = byId("credential-status");
    elements.accountPreview = byId("account-preview");
    elements.accountEmail = byId("account-email");
    elements.extractTokenButton = byId("extract-token-button");
    elements.copyTokenButton = byId("copy-token-button");
    elements.batchImportButton = byId("batch-import-button");
    elements.socketStatus = byId("socket-status");
    elements.taskList = byId("task-list");
    elements.emptyState = byId("empty-state");
    elements.taskCount = byId("task-count");
    elements.runningCount = byId("running-count");
    elements.succeededCount = byId("succeeded-count");
    elements.failedCount = byId("failed-count");
    elements.successRate = byId("success-rate");
    elements.taskFilters = byId("task-filters");
    elements.viewToggle = byId("view-toggle");
    elements.selectAllSucceeded = byId("select-all-succeeded");
    elements.selectedTaskCount = byId("selected-task-count");
    elements.exportCsvButton = byId("export-csv-button");
    elements.retryNetworkFailedTasksButton = byId("retry-network-failed-tasks");
    elements.clearFailedTasksButton = byId("clear-failed-tasks");
    elements.clearSucceededTasksButton = byId("clear-succeeded-tasks");
    elements.taskDetailsModal = byId("task-details-modal");
    elements.taskDetailsClose = byId("task-details-close");
    elements.taskDetailsContent = byId("task-details-content");
    elements.batchImportModal = byId("batch-import-modal");
    elements.batchImportCloseButton = byId("batch-import-close");
    elements.batchImportCompletion = byId("batch-import-completion");
    elements.batchImportInput = byId("batch-import-input");
    elements.batchImportSummary = byId("batch-import-summary");
    elements.batchImportResults = byId("batch-import-results");
    elements.batchValidateButton = byId("batch-import-validate");
    elements.batchSubmitButton = byId("batch-import-submit");
    restoreFormPreferences();
    restoreTaskViewMode();
    bindEvents();
    elements.copyTokenButton.addEventListener("click", copyAccessToken);
    updateCredentialPreview();
    setTaskViewMode(taskViewMode);
    elements.logoutButton.hidden = true;
    const savedPassword = readSavedPassword();
    elements.authPassword.value = savedPassword;
    authenticate(savedPassword);
  });
})();
