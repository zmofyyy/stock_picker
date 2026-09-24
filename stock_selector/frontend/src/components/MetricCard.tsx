/**
 * 指标卡片与统计数字格式化工具。
 */
import { Card, Statistic, Tooltip } from 'antd'
import { ArrowDownOutlined, ArrowUpOutlined } from '@ant-design/icons'

export interface MetricCardProps {
  title: string
  value: number | string | null | undefined
  suffix?: string
  precision?: number
  /** 是否按百分比展示（值应为小数，如 0.1234 → 12.34%） */
  percent?: boolean
  /** 正数显示为红色（A 股习惯：涨红跌绿） */
  colorBySign?: boolean
  /** 反向：正数显示绿色（用于回撤等「越小越好」的指标） */
  invert?: boolean
  tip?: string
  loading?: boolean
}

export default function MetricCard({
  title,
  value,
  suffix = '',
  precision = 2,
  percent = false,
  colorBySign = false,
  invert = false,
  tip,
  loading = false,
}: MetricCardProps) {
  const numeric = typeof value === 'string' ? Number(value) : value
  const isNumber = typeof numeric === 'number' && Number.isFinite(numeric)

  let display: number | string = '-'
  if (isNumber) {
    display = percent ? Number((numeric * 100).toFixed(precision)) : Number(numeric.toFixed(precision))
  } else if (value !== null && value !== undefined) {
    display = value
  }

  let color: string | undefined
  if (colorBySign && isNumber) {
    const positive = numeric > 0
    const good = invert ? !positive : positive
    color = numeric === 0 ? undefined : good ? '#cf1322' : '#3f8600'
  }

  const card = (
    <Card size="small" loading={loading}>
      <Statistic
        title={title}
        value={display}
        precision={isNumber ? precision : undefined}
        suffix={percent ? '%' : suffix}
        valueStyle={color ? { color } : undefined}
        prefix={
          colorBySign && isNumber && numeric !== 0 ? (
            numeric > 0 ? <ArrowUpOutlined /> : <ArrowDownOutlined />
          ) : undefined
        }
      />
    </Card>
  )

  return tip ? <Tooltip title={tip}>{card}</Tooltip> : card
}

/** 百分比格式化 */
export function pct(value?: number | null, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return `${(value * 100).toFixed(digits)}%`
}

/** 数字格式化 */
export function num(value?: number | null, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return value.toFixed(digits)
}

/** 金额格式化（千分位） */
export function money(value?: number | null, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** 涨跌颜色（A 股：红涨绿跌） */
export function changeColor(value?: number | null): string | undefined {
  if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return undefined
  return value > 0 ? '#cf1322' : '#3f8600'
}
