// 把 DOM 节点导出为图片(默认 PNG,可选 JPG)。
// 用 modern-screenshot(SVG foreignObject):浏览器直接渲染真实 DOM,文字与屏幕一致,
// 原生支持 oklch,无需像 html2canvas 那样清洗自定义变量。仅用系统字体,故关掉字体内嵌。
// 导出前等字体就绪,避免中文回退导致字形不一致。pages/CoverPage.tsx 复用同一套函数。

import { domToJpeg, domToPng } from 'modern-screenshot'

interface ExportOptions {
  format?: 'png' | 'jpg'
  quality?: number
}

export async function renderNodeToDataUrl(
  node: HTMLElement,
  opts: ExportOptions = {},
): Promise<string> {
  const { format = 'png', quality } = opts
  if (document.fonts?.ready) await document.fonts.ready
  const options = { scale: 2, backgroundColor: '#ffffff', font: false as const }
  return format === 'jpg' ? domToJpeg(node, { ...options, quality }) : domToPng(node, options)
}

export async function exportNodePng(
  node: HTMLElement,
  filename: string,
  opts: ExportOptions = {},
): Promise<void> {
  const url = await renderNodeToDataUrl(node, opts)
  const a = document.createElement('a')
  a.download = filename
  a.href = url
  a.click()
}
