// Export a DOM node as an image (PNG by default, JPG optional).
// Uses modern-screenshot (SVG foreignObject): the browser renders the real DOM directly, so text matches the screen,
// oklch is supported natively, and there is no need to clean custom variables like html2canvas does. Only system fonts are used, so font embedding is turned off.
// Wait for fonts to be ready before exporting, to avoid glyph mismatch from Chinese fallback. pages/CoverPage.tsx reuses the same functions.

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
