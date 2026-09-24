/**
 * 报告中心页面。
 *
 * 支持每日追踪报告、区间追踪报告、汇总报告；
 * Markdown 在浏览器中渲染，并可导出 Markdown / HTML / CSV。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Empty,
  Input,
  Radio,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { CopyOutlined, DownloadOutlined, FileTextOutlined, ReloadOutlined } from '@ant-design/icons'
import dayjs, { Dayjs } from 'dayjs'
import { api, downloadText } from '@/api'
import type { ReportData } from '@/api'
import MarkdownView from '@/components/MarkdownView'

const { Title, Text, Paragraph } = Typography

type Kind = 'daily' | 'range' | 'summary'

/** 报告类型说明 */
const KIND_HINT: Record<Kind, string> = {
  daily: '汇总指定交易日的信号、状态变更、风险提醒与持仓盈亏。',
  range: '展示指定股票在区间内的状态变化时间线（需填写股票代码）。',
  summary: '关注池中所有股票的当前状态、上次状态、变更时间与关键指标。',
}

export default function Reports() {
  const [kind, setKind] = useState<Kind>('daily')
  const [date, setDate] = useState<Dayjs>(dayjs())
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs().subtract(30, 'day'), dayjs()])
  const [code, setCode] = useState('')
  const [group, setGroup] = useState('')
  const [data, setData] = useState<ReportData | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const resp = await api.getReport({
        kind,
        date: kind === 'daily' ? date.format('YYYY-MM-DD') : undefined,
        code: kind === 'range' ? code.trim() || undefined : undefined,
        start: kind === 'range' ? range[0].format('YYYY-MM-DD') : undefined,
        end: kind === 'range' ? range[1].format('YYYY-MM-DD') : undefined,
        group: group.trim() || undefined,
      })
      setData(resp)
    } catch (error) {
      message.error(`生成报告失败：${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [kind, date, range, code, group])

  useEffect(() => {
    void load()
    // 仅在挂载与切换类型时自动加载，避免频繁请求
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind])

  /** 把报告行数据转成表格列 */
  const columns = useMemo(() => {
    const rows = data?.rows ?? []
    if (!rows.length) return []
    const keys = Object.keys(rows[0])
    return keys.map((k) => ({
      title: k,
      dataIndex: k,
      key: k,
      ellipsis: true,
      render: (v: unknown) => {
        if (v === null || v === undefined) return '-'
        if (Array.isArray(v)) return v.join('、')
        return String(v)
      },
    }))
  }, [data])

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title={
          <Space>
            <FileTextOutlined />
            <Title level={4} style={{ margin: 0 }}>
              报告中心
            </Title>
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Radio.Group
            value={kind}
            onChange={(e) => setKind(e.target.value as Kind)}
            optionType="button"
            buttonStyle="solid"
          >
            <Radio.Button value="daily">每日追踪报告</Radio.Button>
            <Radio.Button value="range">区间追踪报告</Radio.Button>
            <Radio.Button value="summary">汇总报告</Radio.Button>
          </Radio.Group>

          <Text type="secondary">{KIND_HINT[kind]}</Text>

          <Space wrap>
            {kind === 'daily' ? (
              <Space>
                <Text>交易日</Text>
                <DatePicker value={date} onChange={(v) => v && setDate(v)} allowClear={false} />
              </Space>
            ) : null}

            {kind === 'range' ? (
              <>
                <Space>
                  <Text>股票代码</Text>
                  <Input
                    placeholder="600000.SH（留空=全部）"
                    style={{ width: 200 }}
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                  />
                </Space>
                <DatePicker.RangePicker
                  value={range}
                  onChange={(v) => v && v[0] && v[1] && setRange([v[0], v[1]])}
                  allowClear={false}
                />
              </>
            ) : null}

            <Space>
              <Text>分组</Text>
              <Input
                placeholder="留空=全部"
                style={{ width: 140 }}
                value={group}
                onChange={(e) => setGroup(e.target.value)}
              />
            </Space>

            <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={load}>
              生成报告
            </Button>
          </Space>
        </Space>
      </Card>

      {data ? (
        <Card
          title={data.title}
          extra={
            <Space>
              <Button
                size="small"
                icon={<CopyOutlined />}
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(data.markdown)
                    message.success('Markdown 已复制到剪贴板')
                  } catch {
                    message.warning('浏览器不允许访问剪贴板，请手动复制')
                  }
                }}
              >
                复制 Markdown
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() => downloadText(`${data.kind}_report.md`, data.markdown, 'text/markdown')}
              >
                Markdown
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() => downloadText(`${data.kind}_report.html`, data.html, 'text/html')}
              >
                HTML
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                disabled={!data.rows.length}
                onClick={() => {
                  const rows = data.rows
                  const keys = Object.keys(rows[0] ?? {})
                  const csv = [
                    keys.join(','),
                    ...rows.map((r) =>
                      keys
                        .map((k) => {
                          const v = (r as Record<string, unknown>)[k]
                          const s = Array.isArray(v) ? v.join('|') : v == null ? '' : String(v)
                          return `"${s.replace(/"/g, '""')}"`
                        })
                        .join(','),
                    ),
                  ].join('\n')
                  downloadText(`${data.kind}_report.csv`, '\uFEFF' + csv, 'text/csv')
                }}
              >
                CSV
              </Button>
            </Space>
          }
        >
          <Descriptions size="small" column={4} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="生成时间">
              {dayjs(data.generated_at).format('YYYY-MM-DD HH:mm:ss')}
            </Descriptions.Item>
            {Object.entries(data.period ?? {}).map(([k, v]) => (
              <Descriptions.Item key={k} label={k}>
                {String(v ?? '-')}
              </Descriptions.Item>
            ))}
          </Descriptions>

          {Object.keys(data.summary ?? {}).length ? (
            <Space wrap style={{ marginBottom: 12 }}>
              {Object.entries(data.summary).map(([k, v]) => (
                <Tag color="geekblue" key={k}>
                  {k}：{typeof v === 'number' ? v.toFixed(2) : String(v)}
                </Tag>
              ))}
            </Space>
          ) : null}

          <Spin spinning={loading}>
            <MarkdownView content={data.markdown} title="报告预览" filename={`${data.kind}_report`} />
          </Spin>
        </Card>
      ) : (
        <Card>
          <Empty description={loading ? '正在生成报告…' : '尚未生成报告，请选择类型后点击「生成报告」'} />
        </Card>
      )}

      {data?.rows?.length ? (
        <Card title={`结构化数据（${data.rows.length} 行）`}>
          <Paragraph type="secondary">
            报告中的原始数据表，便于二次加工；可直接导出 CSV。
          </Paragraph>
          <Table
            size="small"
            rowKey={(_, i) => String(i)}
            columns={columns as never}
            dataSource={data.rows}
            scroll={{ x: 'max-content' }}
            pagination={{ pageSize: 15, showSizeChanger: true }}
          />
        </Card>
      ) : null}
    </Space>
  )
}
