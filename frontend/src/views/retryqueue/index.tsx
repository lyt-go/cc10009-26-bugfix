import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/retryqueue'
const columns = [
  '条目编号',
  '失败URL',
  '失败原因码',
  '首次失败时间',
  '已重试次数',
  '下次重试时间',
  '退避级别',
  '最终处置'
]

// 动作按当前状态收敛：只有重试中能收敛为成功或最终失败，终态只能重新加入队列。
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  草稿: ['加入队列'],
  等待重试: ['发起重试'],
  重试中: ['重试成功', '标记最终失败'],
  重试成功: ['重新加入队列'],
  最终失败: ['重新加入队列']
}
const ALL_ACTIONS = ['加入队列', '发起重试', '重试成功', '标记最终失败', '重新加入队列']
const STATUSES = ['草稿', '等待重试', '重试中', '重试成功', '最终失败']

type Row = Record<string, string | number | null | unknown>
type Attempt = Record<string, string | number | null | boolean>
type Result = Record<string, string | number | null | boolean>
type Stats = Record<string, number>

const emptyStats: Stats = {
  条目总数: 0,
  等待重试数: 0,
  重试中数: 0,
  重试成功数: 0,
  最终失败数: 0,
  重试成功率: 0,
  尝试记录数: 0,
  最终失败清单数: 0
}

function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback
  return String(value)
}

function statusOf(row: Row): string {
  return String(row.status ?? '草稿')
}

export default function RetryqueueView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<Stats>(emptyStats)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({ keyword: '', status: '', url: '' })
  const [selected, setSelected] = useState<Row | null>(null)
  const [attempts, setAttempts] = useState<Attempt[]>([])
  const [results, setResults] = useState<Result[]>([])
  const [detailError, setDetailError] = useState('')
  const [bulkText, setBulkText] = useState('')
  const [bulkReason, setBulkReason] = useState('')
  const [bulkJob, setBulkJob] = useState('')
  const [bulkResult, setBulkResult] = useState<Record<string, unknown>[]>([])
  const [finalFailures, setFinalFailures] = useState<Record<string, unknown>[]>([])
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(
      Object.entries(filters).filter(([, value]) => value !== '')
    ).toString()
    try {
      const [listResponse, statsResponse, finalResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/stats`),
        request(`${ENDPOINT}/final-failures`)
      ])
      if (!listResponse.ok) throw new Error('重试条目列表读取失败')
      if (!statsResponse.ok) throw new Error('重试队列指标读取失败')
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      const statsPayload = await statsResponse.json()
      setStats({ ...emptyStats, ...statsPayload })
      if (finalResponse.ok) {
        const finalPayload = await finalResponse.json()
        setFinalFailures(finalPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试队列列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const loadDetail = useCallback(async (row: Row) => {
    setSelected(row)
    setDetailError('')
    setAttempts([])
    setResults([])
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('重试条目明细读取失败')
      const payload = await response.json()
      setSelected(payload)
      setAttempts(payload['尝试记录'] ?? [])
      setResults(payload['重试结果'] ?? [])
    } catch (error) {
      setDetailError(error instanceof Error ? error.message : '重试条目明细读取失败')
    }
  }, [])

  async function runAction(action: string, row: Row) {
    if (busy) return
    setBusy(true)
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } })
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '重试队列动作未生效，请稍后重试')
      }
      setSuccessMessage(String(payload.message ?? '操作已生效'))
      await reload()
      if (selected && String(selected.id) === String(row.id)) {
        await loadDetail({ id: row.id })
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试队列操作失败')
    } finally {
      setBusy(false)
    }
  }

  async function submitBulk() {
    const urls = bulkText
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean)
    if (urls.length === 0) {
      setErrorMessage('请先填写至少一个失败URL')
      return
    }
    setBusy(true)
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/bulk`, {
        method: 'POST',
        body: JSON.stringify({ urls, reason: bulkReason, job: bulkJob })
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || '批量提交失败')
      }
      setBulkResult(payload.items ?? [])
      setSuccessMessage(String(payload.message ?? '批量提交已完成'))
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批量提交失败')
    } finally {
      setBusy(false)
    }
  }

  async function drainQueue() {
    if (busy) return
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/drain`, { method: 'POST' })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) throw new Error(payload.message || '重试队列执行失败')
      setSuccessMessage(String(payload.message ?? '重试队列已执行'))
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试队列执行失败')
    } finally {
      setBusy(false)
    }
  }

  async function syncExecution() {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/resync`, { method: 'POST' })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) throw new Error(payload.message || '同步失败')
      setSuccessMessage(String(payload.message ?? '已同步'))
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '同步失败')
    } finally {
      setBusy(false)
    }
  }

  const cards = useMemo(
    () => [
      { label: '重试条目总数', value: stats.条目总数 ?? 0 },
      { label: '等待重试数', value: stats.等待重试数 ?? 0 },
      { label: '重试成功率', value: `${Math.round((stats.重试成功率 ?? 0) * 100)}%` },
      { label: '最终失败清单', value: stats.最终失败清单数 ?? 0 }
    ],
    [stats]
  )

  return (
    <section className="page" data-module="retryqueue">
      <header className="page-head">
        <div>
          <h2>重试队列管理</h2>
          <p className="page-desc">
            管理采集失败URL的重试队列：只有「重试中」能收敛为成功或最终失败，每次状态变化都会留下尝试记录。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" disabled={busy} onClick={() => void drainQueue()}>
            执行重试队列
          </button>
          <button className="btn" type="button" disabled={busy} onClick={() => void syncExecution()}>
            同步网页执行侧
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出重试队列清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {cards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>条目编号 / 失败URL</span>
          <input
            value={filters.keyword}
            placeholder="按条目编号或失败URL检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>失败URL</span>
          <input
            value={filters.url}
            placeholder="按失败URL检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, url: event.target.value }))}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({ keyword: '', status: '', url: '' })}>
          重置条件
        </button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const status = statusOf(row)
            const allowed = ACTIONS_BY_STATUS[status] ?? []
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => (
                  <td key={column}>{text(row[column])}</td>
                ))}
                <td>{status}</td>
                <td className="row-actions">
                  <button className="link" type="button" onClick={() => void loadDetail(row)}>
                    详情
                  </button>
                  {ALL_ACTIONS.filter((action) => allowed.includes(action)).map((action) => (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={busy}
                      onClick={() => void runAction(action, row)}
                    >
                      {action}
                    </button>
                  ))}
                  {allowed.length === 0 && <span className="muted">终态不可流转</span>}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无重试队列数据，可先批量登记失败URL</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条重试队列记录</span>
        {successMessage && <span className="ok-text">{successMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <section className="panel">
        <h3>批量提交失败URL</h3>
        <p className="page-desc">一行一个URL（也支持逗号分隔），整组登记后逐条返回尝试次数与最终处置。</p>
        <div className="filter-bar">
          <label className="filter-item grow">
            <span>失败URL</span>
            <textarea
              rows={4}
              value={bulkText}
              placeholder={'https://example.com/a.html\nhttps://example.com/b.html'}
              onChange={(event) => setBulkText(event.target.value)}
            />
          </label>
          <label className="filter-item">
            <span>失败原因码</span>
            <input value={bulkReason} placeholder="如 TIMEOUT / HTTP_503" onChange={(event) => setBulkReason(event.target.value)} />
          </label>
          <label className="filter-item">
            <span>作业编号</span>
            <input value={bulkJob} placeholder="如 CRAW-0002" onChange={(event) => setBulkJob(event.target.value)} />
          </label>
          <button className="btn primary" type="button" disabled={busy} onClick={() => void submitBulk()}>
            整组提交重试
          </button>
        </div>
        {bulkResult.length > 0 && (
          <table className="data-table">
            <thead>
              <tr><th>失败URL</th><th>条目编号</th><th>尝试次数</th><th>当前状态</th><th>最终处置</th></tr>
            </thead>
            <tbody>
              {bulkResult.map((item, index) => (
                <tr key={`${String(item['失败URL'])}-${index}`}>
                  <td>{text(item['失败URL'])}</td>
                  <td>{text(item['条目编号'])}</td>
                  <td>{text(item['尝试次数'], '0')}</td>
                  <td>{text(item['当前状态'] ?? item['message'])}</td>
                  <td>{text(item['最终处置'], '—')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {selected && (
        <section className="panel">
          <header className="panel-head">
            <h3>重试详情 · {text(selected['条目编号'])}</h3>
            <button className="btn ghost" type="button" onClick={() => setSelected(null)}>关闭</button>
          </header>
          <dl className="detail-grid">
            <div><dt>当前状态</dt><dd>{text(selected.status)}</dd></div>
            <div><dt>失败URL</dt><dd>{text(selected['失败URL'])}</dd></div>
            <div><dt>失败原因码</dt><dd>{text(selected['失败原因码'])}</dd></div>
            <div><dt>首次失败时间</dt><dd>{text(selected['首次失败时间'])}</dd></div>
            <div><dt>已重试次数</dt><dd>{text(selected['已重试次数'], '0')}</dd></div>
            <div><dt>退避级别</dt><dd>{text(selected['退避级别'], '0')}</dd></div>
            <div><dt>下次重试时间</dt><dd>{text(selected['下次重试时间'])}</dd></div>
            <div><dt>最终处置</dt><dd>{text(selected['最终处置'])}</dd></div>
          </dl>
          {detailError && <p className="error-text">{detailError}</p>}
          <h4>尝试记录</h4>
          <table className="data-table">
            <thead>
              <tr><th>尝试编号</th><th>尝试序号</th><th>动作</th><th>状态前</th><th>状态后</th><th>结果</th><th>记录时间</th></tr>
            </thead>
            <tbody>
              {attempts.map((attempt) => (
                <tr key={String(attempt['尝试编号'])}>
                  <td>{text(attempt['尝试编号'])}</td>
                  <td>{text(attempt['尝试序号'], '0')}</td>
                  <td>{text(attempt['动作'])}</td>
                  <td>{text(attempt['状态前'])}</td>
                  <td>{text(attempt['状态后'])}</td>
                  <td>{text(attempt['结果状态'])}</td>
                  <td>{text(attempt['记录时间'])}</td>
                </tr>
              ))}
              {attempts.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>暂无尝试记录</td></tr>
              )}
            </tbody>
          </table>
          <h4>重试结果</h4>
          <table className="data-table">
            <thead>
              <tr><th>结果编号</th><th>尝试序号</th><th>结果状态</th><th>最终处置</th><th>完成时间</th><th>备注</th></tr>
            </thead>
            <tbody>
              {results.map((result) => (
                <tr key={String(result['结果编号'])}>
                  <td>{text(result['结果编号'])}</td>
                  <td>{text(result['尝试序号'], '0')}</td>
                  <td>{text(result['结果状态'])}{result['冲突标志'] ? '（冲突版本）' : ''}</td>
                  <td>{text(result['最终处置'])}</td>
                  <td>{text(result['完成时间'])}</td>
                  <td>{text(result['冲突说明'] ?? result['最终处置'])}</td>
                </tr>
              ))}
              {results.length === 0 && (
                <tr><td className="empty-state" colSpan={6}>暂无重试结果</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <section className="panel">
        <h3>最终失败清单</h3>
        <p className="page-desc">成功结果与最终失败清单按失败URL互斥，同一URL不会两边同时出现。</p>
        <table className="data-table">
          <thead>
            <tr><th>清单编号</th><th>条目编号</th><th>失败URL</th><th>失败原因码</th><th>已重试次数</th><th>最终处置</th><th>最终失败时间</th></tr>
          </thead>
          <tbody>
            {finalFailures.map((row) => (
              <tr key={String(row['清单编号'])}>
                <td>{text(row['清单编号'])}</td>
                <td>{text(row['条目编号'])}</td>
                <td>{text(row['失败URL'])}</td>
                <td>{text(row['失败原因码'])}</td>
                <td>{text(row['已重试次数'], '0')}</td>
                <td>{text(row['最终处置'])}</td>
                <td>{text(row['最终失败时间'])}</td>
              </tr>
            ))}
            {finalFailures.length === 0 && (
              <tr><td className="empty-state" colSpan={7}>最终失败清单为空</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </section>
  )
}
