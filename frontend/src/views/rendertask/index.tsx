import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/rendertask'
const columns = [
  '作业编号',
  '目标URL',
  '浏览器版本',
  '视口宽度',
  '视口高度',
  '等待策略',
  '脚本注入',
  '超时阈值秒'
]
const actions = ['提交排队', '启动渲染', '终止渲染']
const statuses = ['草稿', '排队中', '渲染中', '已完成', '已超时']

type Row = Record<string, string | number | null | unknown>
type RetryResult = Record<string, string | number | null | boolean>

function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback
  if (Array.isArray(value)) return value.length === 0 ? '—' : value.join('，')
  return String(value)
}

export default function RendertaskView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({ keyword: '', status: '' })
  const [failedUrls, setFailedUrls] = useState<Record<string, unknown>[]>([])
  const [retryResults, setRetryResults] = useState<RetryResult[]>([])
  const [busy, setBusy] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 2), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== '')).toString()
    try {
      const [listResponse, failureResponse, resultResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/failures`),
        request(`${ENDPOINT}/retry-results`)
      ])
      if (!listResponse.ok) throw new Error('渲染作业列表读取失败')
      const payload = await listResponse.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (failureResponse.ok) {
        const failurePayload = await failureResponse.json()
        setFailedUrls(failurePayload.items ?? [])
      }
      if (resultResponse.ok) {
        const resultPayload = await resultResponse.json()
        setRetryResults(resultPayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    if (busy) return
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } })
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) throw new Error(payload.message || '渲染任务动作未生效，请稍后重试')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '渲染任务操作失败')
    } finally {
      setBusy(false)
    }
  }

  async function syncFromRetry() {
    setBusy(true)
    setErrorMessage('')
    setSuccessMessage('')
    try {
      const response = await request(`${ENDPOINT}/sync`, { method: 'POST' })
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

  const cards = [
    { label: '渲染作业总数', value: total },
    { label: '失败URL集合', value: failedUrls.length },
    { label: '最终失败URL', value: failedUrls.filter((item) => item['重试状态'] === '最终失败').length }
  ]

  return (
    <section className="page" data-module="rendertask">
      <header className="page-head">
        <div>
          <h2>渲染任务管理</h2>
          <p className="page-desc">网页执行侧：读取重试结果并维护失败URL集合，成功结果与最终失败清单不会同时包含同一URL。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" disabled={busy} onClick={() => void syncFromRetry()}>
            同步重试结果
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出渲染任务清单
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
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select value={filters.status} onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}>
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({ keyword: '', status: '' })}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>失败URL集合</th>
            <th>重试状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{text(row[column])}</td>)}
              <td>{text(row['失败URL集合'])}</td>
              <td>{text(row['重试状态'])}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" disabled={busy} onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td className="empty-state" colSpan={columns.length + 3}>暂无渲染任务数据</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条渲染任务记录</span>
        {successMessage && <span className="ok-text">{successMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <section className="panel">
        <h3>失败URL集合</h3>
        <p className="page-desc">这些URL仍处于失败或重试中；重试成功后会自动从本集合移除。</p>
        <table className="data-table">
          <thead>
            <tr><th>作业编号</th><th>失败URL</th><th>重试状态</th><th>失败原因码</th><th>同步版本</th></tr>
          </thead>
          <tbody>
            {failedUrls.map((item, index) => (
              <tr key={`${String(item['失败URL'])}-${index}`}>
                <td>{text(item['作业编号'])}</td>
                <td>{text(item['失败URL'])}</td>
                <td>{text(item['重试状态'])}</td>
                <td>{text(item['最近失败原因码'])}</td>
                <td>{text(item['同步版本'], '0')}</td>
              </tr>
            ))}
            {failedUrls.length === 0 && (
              <tr><td className="empty-state" colSpan={5}>失败URL集合为空</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>重试结果</h3>
        <table className="data-table">
          <thead>
            <tr><th>结果编号</th><th>条目编号</th><th>失败URL</th><th>尝试序号</th><th>结果状态</th><th>最终处置</th><th>完成时间</th></tr>
          </thead>
          <tbody>
            {retryResults.map((result) => (
              <tr key={String(result['结果编号'])}>
                <td>{text(result['结果编号'])}</td>
                <td>{text(result['条目编号'])}</td>
                <td>{text(result['失败URL'])}</td>
                <td>{text(result['尝试序号'], '0')}</td>
                <td>{text(result['结果状态'])}{result['冲突标志'] ? '（冲突版本）' : ''}</td>
                <td>{text(result['最终处置'])}</td>
                <td>{text(result['完成时间'])}</td>
              </tr>
            ))}
            {retryResults.length === 0 && (
              <tr><td className="empty-state" colSpan={7}>暂无重试结果</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </section>
  )
}
