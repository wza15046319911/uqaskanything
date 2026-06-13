import type { Transition, Variants } from 'motion/react'

export const riseIn: Variants = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0 },
}

export const easeOut: Transition = { duration: 0.3, ease: 'easeOut' }

export const layoutEase: Transition = { duration: 0.45, ease: [0.22, 1, 0.36, 1] }

export function riseDelay(i: number, step = 0.045, max = 0.4): number {
  return Math.min(i * step, max)
}
