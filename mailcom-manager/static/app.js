const state = { accounts: [], query: '', activeAccount: null, aliasAccount: null, aliases: [], folder: 'INBOX', bulkAliasJob: null, bulkAliasTimer: null }
const $ = (id) => document.getElementById(id)
let toastTimer

function toast(message, type = 'success') {
  const element = $('toast')
  element.textContent = message
  element.className = `toast show${type === 'error' ? ' error' : ''}`
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { element.className = 'toast' }, 3200)
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    ...options,
  })
  let data
  try { data = await response.json() } catch { data = null }
  if (!response.ok) {
    const detail = data?.detail
    throw new Error(detail?.message || detail || `请求失败（HTTP ${response.status}）`)
  }
  return data
}

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function actionButton(label, handler, kind = '') {
  const button = document.createElement('button')
  button.type = 'button'
  button.textContent = label
  if (kind) button.className = kind
  button.addEventListener('click', handler)
  return button
}

function registrationLine(email) {
  const params = new URLSearchParams({ email })
  return `${email}----${window.location.origin}/api/mail/latest?${params}`
}

async function copyRegistrationLine(email) {
  await navigator.clipboard.writeText(registrationLine(email))
  toast('独立接码 URL 已复制')
}

function renderAccounts() {
  const body = $('accountRows')
  body.replaceChildren()
  $('emptyState').hidden = state.accounts.length > 0
  $('resultCount').textContent = `共 ${state.accounts.length} 个邮箱`

  for (const account of state.accounts) {
    const row = document.createElement('tr')

    const identity = document.createElement('td')
    const email = document.createElement('strong')
    email.textContent = account.email
    const storage = document.createElement('small')
    storage.textContent = 'DPAPI 加密凭据'
    identity.append(email, storage)

    const statusCell = document.createElement('td')
    const status = document.createElement('span')
    status.className = `status ${account.status}`
    status.textContent = account.status === 'online' ? '连接正常' : account.status === 'failed' ? '连接失败' : '尚未测试'
    statusCell.append(status)

    const count = document.createElement('td')
    count.textContent = account.messageCount ?? '—'

    const aliasCount = document.createElement('td')
    aliasCount.textContent = account.aliasCount ?? 0

    const checked = document.createElement('td')
    checked.textContent = formatTime(account.lastCheckedAt)

    const errorCell = document.createElement('td')
    const error = document.createElement('span')
    error.className = 'error-text'
    error.textContent = account.lastError || '—'
    error.title = account.lastError || ''
    errorCell.append(error)

    const actions = document.createElement('td')
    actions.className = 'right'
    const wrap = document.createElement('div')
    wrap.className = 'row-actions'
    wrap.append(
      actionButton('测试', () => testOne(account)),
      actionButton('收件箱', () => openMailbox(account)),
      actionButton(`分裂管理 (${account.aliasCount ?? 0})`, () => openAliasManager(account)),
      actionButton('复制URL', () => copyRegistrationLine(account.email).catch((error) => toast(error.message, 'error'))),
      actionButton('复制邮箱', () => navigator.clipboard.writeText(account.email).then(() => toast('邮箱已复制'))),
      actionButton('删除', () => removeAccount(account), 'danger'),
    )
    actions.append(wrap)
    row.append(identity, statusCell, count, aliasCount, checked, errorCell, actions)
    body.append(row)
  }
}

async function loadStats() {
  const stats = await api('/api/stats')
  $('statTotal').textContent = stats.total
  $('statOnline').textContent = stats.online
  $('statFailed').textContent = stats.failed
  $('statUnknown').textContent = stats.unknown
  $('statAliases').textContent = stats.aliases
}

async function loadAccounts() {
  const params = new URLSearchParams({ q: state.query, page: '1', pageSize: '100' })
  const data = await api(`/api/accounts?${params}`)
  state.accounts = data.items
  $('resultCount').textContent = `共 ${data.total} 个邮箱`
  renderAccounts()
  await loadStats()
}

async function testOne(account) {
  toast(`正在测试 ${account.email}…`)
  try {
    const result = await api(`/api/accounts/${account.id}/test`, { method: 'POST' })
    toast(result.ok ? `连接成功，收件箱 ${result.messageCount} 封邮件` : result.error.message, result.ok ? 'success' : 'error')
  } catch (error) { toast(error.message, 'error') }
  await loadAccounts()
}

async function testAll() {
  const button = $('testAllButton')
  button.disabled = true
  button.textContent = '测试中…'
  try {
    const result = await api('/api/accounts/test-all', { method: 'POST', body: JSON.stringify({ ids: [] }) })
    toast(`测试完成：正常 ${result.online}，失败 ${result.failed}`)
  } catch (error) { toast(error.message, 'error') }
  finally { button.disabled = false; button.textContent = '测试全部' }
  await loadAccounts()
}

async function copyRegistrationLines() {
  try {
    const response = await fetch('/api/export/registration-lines')
    if (!response.ok) throw new Error(`导出失败（HTTP ${response.status}）`)
    const content = await response.text()
    await navigator.clipboard.writeText(content)
    const count = content ? content.split(/\r?\n/).filter(Boolean).length : 0
    toast(`已复制 ${count} 条注册机格式`)
  } catch (error) { toast(error.message, 'error') }
}

async function pushServerSnapshot(event) {
  event.preventDefault()
  const host = $('serverSyncHost').value.trim()
  const port = Number.parseInt($('serverSyncPort').value, 10)
  const username = $('serverSyncUsername').value.trim()
  const password = $('serverSyncPassword').value
  if (!host || !username || !password || !Number.isInteger(port)) {
    return toast('请填写完整的服务器 SSH 信息', 'error')
  }
  const button = $('submitServerSync')
  const output = $('serverSyncResult')
  button.disabled = true
  button.textContent = '正在加密并推送…'
  output.hidden = true
  try {
    const result = await api('/api/server-sync', {
      method: 'POST',
      body: JSON.stringify({ host, port, username, password }),
    })
    $('serverSyncPassword').value = ''
    output.hidden = false
    output.textContent = `推送完成：主邮箱 ${result.accounts} 个，分裂邮箱 ${result.aliases} 个；服务器指纹 ${result.hostKeySha256.slice(0, 16)}…`
    toast('服务器邮箱快照已更新')
  } catch (error) {
    $('serverSyncPassword').value = ''
    toast(error.message, 'error')
  } finally {
    button.disabled = false
    button.textContent = '开始推送'
  }
}

function renderBulkAliasJob(job) {
  state.bulkAliasJob = job
  const panel = $('bulkAliasProgress')
  const button = $('bulkCreateAliasesButton')
  if (!job) {
    panel.hidden = true
    button.disabled = false
    return
  }
  panel.hidden = false
  const running = ['queued', 'running'].includes(job.status)
  button.disabled = running
  button.textContent = running ? '正在补足…' : '一键补足未满'
  $('bulkAliasBar').style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`
  $('bulkAliasSummary').textContent = `${job.completed} / ${job.total} · ${job.progress || 0}%`
  const active = Array.isArray(job.activeAccounts) ? job.activeAccounts.filter(Boolean) : []
  $('bulkAliasCurrent').textContent = active.length
    ? `运行中 ${active.length}/${job.concurrency || 1}：${active.join('、')}`
    : running ? '正在准备下一个邮箱…' : '任务已结束'
  $('bulkAliasResult').textContent = `成功 ${job.succeeded} · 失败 ${job.failed} · 新建 ${job.created}`
  $('bulkAliasTitle').textContent = job.status === 'completed'
    ? '未满邮箱已全部补足'
    : job.status === 'completed_with_errors'
      ? '批量补足完成（部分失败）'
      : `并发补足到 10 个地址（${job.concurrency || 1} 路）`
}

async function pollBulkAliasJob(jobId) {
  clearTimeout(state.bulkAliasTimer)
  try {
    const job = await api(`/api/aliases/auto-create-all/${jobId}`)
    renderBulkAliasJob(job)
    if (['queued', 'running'].includes(job.status)) {
      state.bulkAliasTimer = setTimeout(() => pollBulkAliasJob(jobId), 2000)
    } else {
      toast(`补足完成：成功 ${job.succeeded} 个，失败 ${job.failed} 个，新建 ${job.created} 个别名`, job.failed ? 'error' : 'success')
      await loadAccounts()
    }
  } catch (error) {
    $('bulkCreateAliasesButton').disabled = false
    $('bulkCreateAliasesButton').textContent = '一键补足未满'
    toast(error.message, 'error')
  }
}

async function startBulkAliases() {
  const button = $('bulkCreateAliasesButton')
  button.disabled = true
  button.textContent = '正在创建任务…'
  try {
    const job = await api('/api/aliases/auto-create-all', {
      method: 'POST',
      body: JSON.stringify({
        targetTotal: 10,
        concurrency: Number.parseInt($('aliasConcurrencySelect').value, 10) || 2,
      }),
    })
    renderBulkAliasJob(job)
    if (!job.total) {
      toast('所有邮箱都已经分裂满 10 个地址')
      return
    }
    toast(`已开始处理 ${job.total} 个未满邮箱，${job.concurrency || 2} 路并发创建`)
    await pollBulkAliasJob(job.id)
  } catch (error) {
    button.disabled = false
    button.textContent = '一键补足未满'
    toast(error.message, 'error')
  }
}

async function resumeBulkAliases() {
  try {
    const result = await api('/api/aliases/auto-create-all/active')
    $('bulkCreateAliasesButton').hidden = false
    if (!result.job) return
    renderBulkAliasJob(result.job)
    if (['queued', 'running'].includes(result.job.status)) {
      await pollBulkAliasJob(result.job.id)
    }
  } catch { $('bulkCreateAliasesButton').hidden = true }
}

function renderAliases() {
  const list = $('aliasList')
  list.replaceChildren()
  if (!state.aliases.length) {
    const empty = document.createElement('div')
    empty.className = 'alias-empty'
    empty.textContent = '还没有分裂邮箱。导入已经能投递到这个主邮箱的别名即可。'
    list.append(empty)
    return
  }
  for (const alias of state.aliases) {
    const item = document.createElement('article')
    item.className = 'alias-item'
    const identity = document.createElement('div')
    const email = document.createElement('strong')
    email.textContent = alias.email
    const label = document.createElement('small')
    label.textContent = alias.label || '独立接码 URL'
    identity.append(email, label)
    const actions = document.createElement('div')
    actions.className = 'row-actions'
    actions.append(
      actionButton('复制URL', () => copyRegistrationLine(alias.email).catch((error) => toast(error.message, 'error'))),
      actionButton('删除', () => removeAlias(alias), 'danger'),
    )
    item.append(identity, actions)
    list.append(item)
  }
}

async function loadAliases() {
  if (!state.aliasAccount) return
  const data = await api(`/api/accounts/${state.aliasAccount.id}/aliases`)
  state.aliases = data.items
  renderAliases()
}

async function openAliasManager(account) {
  state.aliasAccount = account
  state.aliases = []
  $('aliasTitle').textContent = `${account.email} · 分裂邮箱`
  $('aliasImportText').value = ''
  $('aliasList').innerHTML = '<div class="alias-empty">正在加载…</div>'
  $('aliasDialog').showModal()
  try { await loadAliases() } catch (error) { toast(error.message, 'error') }
}

async function importAliases() {
  if (!state.aliasAccount) return
  const rawText = $('aliasImportText').value
  if (!rawText.trim()) return toast('请先填写分裂邮箱', 'error')
  const button = $('importAliasesButton')
  button.disabled = true
  try {
    const result = await api(`/api/accounts/${state.aliasAccount.id}/aliases/import`, {
      method: 'POST',
      body: JSON.stringify({ rawText }),
    })
    toast(`已导入 ${result.imported} 个，重复 ${result.duplicateCount} 个`)
    $('aliasImportText').value = ''
    await Promise.all([loadAliases(), loadAccounts()])
  } catch (error) { toast(error.message, 'error') }
  finally { button.disabled = false }
}

async function autoCreateAliases() {
  if (!state.aliasAccount) return
  const button = $('autoCreateAliasesButton')
  button.disabled = true
  button.textContent = '正在登录 mail.com 并创建…'
  try {
    const result = await api(`/api/accounts/${state.aliasAccount.id}/aliases/auto-create`, {
      method: 'POST',
      body: JSON.stringify({ targetTotal: 10 }),
    })
    toast(`已完成：新建 ${result.created} 个，当前分裂邮箱 ${result.aliasCount} 个`)
    await Promise.all([loadAliases(), loadAccounts()])
  } catch (error) { toast(error.message, 'error') }
  finally {
    button.disabled = false
    button.textContent = '自动补足到 10 个'
  }
}

async function removeAlias(alias) {
  if (!confirm(`删除分裂邮箱 ${alias.email}？`)) return
  try {
    await api(`/api/aliases/${alias.id}`, { method: 'DELETE' })
    toast('分裂邮箱已删除')
    await Promise.all([loadAliases(), loadAccounts()])
  } catch (error) { toast(error.message, 'error') }
}

async function removeAccount(account) {
  if (!confirm(`删除邮箱 ${account.email}？`)) return
  try {
    await api(`/api/accounts/${account.id}`, { method: 'DELETE' })
    toast('邮箱已删除')
    await loadAccounts()
  } catch (error) { toast(error.message, 'error') }
}

async function openMailbox(account) {
  state.activeAccount = account
  state.folder = 'INBOX'
  $('mailTitle').textContent = account.email
  document.querySelectorAll('#folderTabs button').forEach((button) => button.classList.toggle('active', button.dataset.folder === 'INBOX'))
  $('mailDialog').showModal()
  await loadMessages()
}

function renderMessages(items) {
  const list = $('messageList')
  list.replaceChildren()
  if (!items.length) {
    const empty = document.createElement('div')
    empty.className = 'mail-loading'
    empty.textContent = '这个文件夹里没有邮件'
    list.append(empty)
    return
  }
  for (const item of items) {
    const card = document.createElement('article')
    card.className = 'message-card'
    const header = document.createElement('header')
    const subject = document.createElement('h3')
    subject.textContent = item.subject
    const time = document.createElement('time')
    time.textContent = formatTime(item.receivedAt)
    header.append(subject, time)
    const meta = document.createElement('div')
    meta.className = 'message-meta'
    const sender = document.createElement('span')
    sender.textContent = `来自：${item.sender || '未知'}`
    const recipient = document.createElement('span')
    recipient.textContent = `收件人：${item.recipients || '未提供'}`
    meta.append(sender, recipient)
    const preview = document.createElement('p')
    preview.className = 'message-preview'
    preview.textContent = item.preview || '无正文预览'
    card.append(header, meta, preview)
    if (item.verificationCode) {
      const chip = document.createElement('div')
      chip.className = 'code-chip'
      const value = document.createElement('span')
      value.textContent = item.verificationCode
      chip.append(value, actionButton('复制验证码', () => navigator.clipboard.writeText(item.verificationCode).then(() => toast('验证码已复制'))))
      card.append(chip)
    }
    list.append(card)
  }
}

async function loadMessages() {
  if (!state.activeAccount) return
  $('messageList').innerHTML = '<div class="mail-loading">正在只读连接 mail.com…</div>'
  try {
    const data = await api(`/api/accounts/${state.activeAccount.id}/messages?folder=${encodeURIComponent(state.folder)}&limit=20`)
    renderMessages(data.items)
  } catch (error) {
    $('messageList').innerHTML = ''
    const message = document.createElement('div')
    message.className = 'mail-loading'
    message.textContent = error.message
    $('messageList').append(message)
    toast(error.message, 'error')
  }
}

$('importButton').addEventListener('click', () => {
  $('importText').value = ''
  $('importResult').hidden = true
  $('importDialog').showModal()
})

$('importForm').addEventListener('submit', async (event) => {
  event.preventDefault()
  const rawText = $('importText').value
  if (!rawText.trim()) return toast('请先粘贴邮箱和密码', 'error')
  const button = $('submitImport')
  button.disabled = true
  try {
    const result = await api('/api/accounts/import', { method: 'POST', body: JSON.stringify({ rawText }) })
    const output = $('importResult')
    output.hidden = false
    output.textContent = `总计 ${result.total} · 导入 ${result.imported} · 重复 ${result.duplicateCount} · 错误 ${result.errorCount}`
    toast(`成功导入 ${result.imported} 个邮箱`)
    await loadAccounts()
    if (!result.errorCount) setTimeout(() => $('importDialog').close(), 650)
  } catch (error) { toast(error.message, 'error') }
  finally { button.disabled = false }
})

$('refreshButton').addEventListener('click', () => loadAccounts().catch((error) => toast(error.message, 'error')))
$('copyRegistrationButton').addEventListener('click', copyRegistrationLines)
$('testAllButton').addEventListener('click', testAll)
$('serverSyncButton').addEventListener('click', () => {
  $('serverSyncPassword').value = ''
  $('serverSyncResult').hidden = true
  $('serverSyncDialog').showModal()
})
$('serverSyncForm').addEventListener('submit', pushServerSnapshot)
$('closeServerSyncDialog').addEventListener('click', () => $('serverSyncDialog').close())
$('cancelServerSync').addEventListener('click', () => $('serverSyncDialog').close())
$('bulkCreateAliasesButton').addEventListener('click', startBulkAliases)
$('closeImportDialog').addEventListener('click', () => $('importDialog').close())
$('cancelImport').addEventListener('click', () => $('importDialog').close())
$('closeMailDialog').addEventListener('click', () => $('mailDialog').close())
$('closeAliasDialog').addEventListener('click', () => $('aliasDialog').close())
$('importAliasesButton').addEventListener('click', importAliases)
$('autoCreateAliasesButton').addEventListener('click', autoCreateAliases)
$('reloadMailButton').addEventListener('click', loadMessages)
$('folderTabs').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-folder]')
  if (!button) return
  state.folder = button.dataset.folder
  document.querySelectorAll('#folderTabs button').forEach((item) => item.classList.toggle('active', item === button))
  await loadMessages()
})

let searchTimer
$('searchInput').addEventListener('input', (event) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    state.query = event.target.value
    loadAccounts().catch((error) => toast(error.message, 'error'))
  }, 260)
})

Promise.all([
  loadAccounts(),
  resumeBulkAliases(),
]).catch((error) => toast(error.message, 'error'))
