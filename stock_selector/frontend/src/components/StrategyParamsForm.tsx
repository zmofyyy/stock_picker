/**
 * 策略参数表单。
 *
 * 根据后端返回的 param_schema 动态渲染表单控件，
 * 策略参数完全不硬编码在前端。
 */
import { Form, InputNumber, Select, Space, Switch, Tooltip, Typography } from 'antd'
import { QuestionCircleOutlined } from '@ant-design/icons'
import type { ParamSchema, StrategyMeta } from '@/api'

const { Text } = Typography

export interface StrategyParamsFormProps {
  /** 策略列表 */
  strategies: StrategyMeta[]
  /** 当前选中的策略名 */
  value?: string
  /** 参数值 */
  params?: Record<string, unknown>
  onChange?: (name: string, params: Record<string, unknown>) => void
  /** 是否显示策略选择框 */
  showStrategySelect?: boolean
  layout?: 'horizontal' | 'vertical' | 'inline'
}

export default function StrategyParamsForm({
  strategies,
  value,
  params = {},
  onChange,
  showStrategySelect = true,
  layout = 'vertical',
}: StrategyParamsFormProps) {
  const current = strategies.find((s) => s.name === value)

  const renderField = (key: string, schema: ParamSchema, currentValue: unknown) => {
    const label = (
      <Space size={4}>
        <span>{schema.label || key}</span>
        {schema.help ? (
          <Tooltip title={schema.help}>
            <QuestionCircleOutlined style={{ color: '#999' }} />
          </Tooltip>
        ) : null}
      </Space>
    )

    const type = schema.type ?? 'str'
    let control: React.ReactNode
    if (type === 'bool') {
      control = (
        <Switch
          checked={Boolean(currentValue)}
          onChange={(checked) => onChange?.(value ?? '', { ...params, [key]: checked })}
        />
      )
    } else if (type === 'int' || type === 'float') {
      control = (
        <InputNumber
          style={{ width: '100%' }}
          value={currentValue as number}
          min={schema.min}
          max={schema.max}
          step={schema.step ?? (type === 'int' ? 1 : 0.1)}
          precision={type === 'int' ? 0 : undefined}
          onChange={(v) => onChange?.(value ?? '', { ...params, [key]: v })}
        />
      )
    } else {
      control = (
        <Select
          style={{ width: '100%' }}
          value={currentValue as string}
          onChange={(v) => onChange?.(value ?? '', { ...params, [key]: v })}
          options={(current?.param_schema?.[key] as any)?.options ?? []}
        />
      )
    }

    return (
      <Form.Item key={key} label={label} style={{ marginBottom: 12 }}>
        {control}
      </Form.Item>
    )
  }

  return (
    <div>
      {showStrategySelect ? (
        <Form layout={layout}>
          <Form.Item label="策略" style={{ marginBottom: 12 }}>
            <Select
              value={value}
              onChange={(name) => {
                const target = strategies.find((s) => s.name === name)
                onChange?.(name, { ...(target?.effective_params ?? target?.default_params ?? {}) })
              }}
              options={strategies.map((s) => ({
                value: s.name,
                label: `${s.display_name}（${s.name}）`,
              }))}
            />
          </Form.Item>
        </Form>
      ) : null}

      {current ? (
        <>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            {current.description}
          </Text>
          <Form layout={layout}>
            {Object.entries(current.param_schema || {}).map(([key, schema]) =>
              renderField(key, schema, params[key] ?? current.effective_params?.[key]),
            )}
          </Form>
        </>
      ) : (
        <Text type="secondary">请先选择一个策略</Text>
      )}
    </div>
  )
}
