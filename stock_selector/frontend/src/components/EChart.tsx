/**
 * ECharts 通用容器组件。
 *
 * 直接使用 echarts 核心库（不依赖第三方 React 封装），
 * 负责实例生命周期、容器尺寸自适应与配置更新。
 */
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

export interface EChartProps {
  /** ECharts option */
  option: echarts.EChartsOption
  /** 高度，默认 360px */
  height?: number | string
  /** 是否在数据变化时清空旧配置（默认 true） */
  notMerge?: boolean
  /** 图表加载中遮罩 */
  loading?: boolean
  /** 点击事件回调 */
  onEvents?: Record<string, (params: any) => void>
  style?: React.CSSProperties
}

export default function EChart({
  option,
  height = 360,
  notMerge = true,
  loading = false,
  onEvents,
  style,
}: EChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  // 初始化与销毁
  useEffect(() => {
    if (!containerRef.current) return
    const chart = echarts.init(containerRef.current, undefined, { renderer: 'canvas' })
    chartRef.current = chart

    const resize = () => chart.resize()
    window.addEventListener('resize', resize)

    return () => {
      window.removeEventListener('resize', resize)
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  // 配置更新
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.setOption(option, notMerge)
  }, [option, notMerge])

  // loading 状态
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    if (loading) {
      chart.showLoading('default', { text: '加载中…', color: '#1677ff' })
    } else {
      chart.hideLoading()
    }
  }, [loading])

  // 事件绑定
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !onEvents) return
    Object.entries(onEvents).forEach(([event, handler]) => {
      chart.on(event, handler)
    })
    return () => {
      Object.keys(onEvents).forEach((event) => chart.off(event))
    }
  }, [onEvents])

  return <div ref={containerRef} style={{ width: '100%', height, ...style }} />
}
