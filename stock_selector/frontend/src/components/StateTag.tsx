/**
 * 状态 / 风险 / 信号 的彩色标签组件。
 */
import { Tag, Tooltip } from 'antd'

/** 状态 → 颜色 */
const STATE_COLORS: Record<string, string> = {
  候选: 'default',
  观察: 'blue',
  触发: 'cyan',
  买入: 'green',
  持仓: 'success',
  减仓: 'orange',
  清仓: 'default',
  止盈: 'gold',
  止损: 'red',
  失效: 'default',
  暂停: 'purple',
}

/** 风险等级 → 颜色 */
const RISK_COLORS: Record<string, string> = {
  normal: 'green',
  warning: 'orange',
  danger: 'red',
}

const RISK_LABELS: Record<string, string> = {
  normal: '正常',
  warning: '警示',
  danger: '危险',
}

export function StateTag({ state, tooltip }: { state?: string | null; tooltip?: string }) {
  if (!state) return <Tag>未知</Tag>
  const tag = <Tag color={STATE_COLORS[state] ?? 'default'}>{state}</Tag>
  return tooltip ? <Tooltip title={tooltip}>{tag}</Tooltip> : tag
}

export function RiskTag({ level, flags }: { level?: string | null; flags?: string[] }) {
  const key = level || 'normal'
  const tag = <Tag color={RISK_COLORS[key] ?? 'default'}>{RISK_LABELS[key] ?? key}</Tag>
  return flags && flags.length ? <Tooltip title={flags.join('；')}>{tag}</Tooltip> : tag
}

export function SignalTag({ signal, text }: { signal?: number | null; text?: string }) {
  if (signal === 1) return <Tag color="red">买入</Tag>
  if (signal === -1) return <Tag color="green">卖出</Tag>
  return <Tag>{text || '无信号'}</Tag>
}

export function AlertLevelTag({ level }: { level?: string | null }) {
  const map: Record<string, { color: string; text: string }> = {
    info: { color: 'blue', text: '信息' },
    warning: { color: 'orange', text: '警示' },
    danger: { color: 'red', text: '危险' },
  }
  const cfg = map[level || 'info'] ?? map.info
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}

export default StateTag
