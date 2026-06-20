import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

export const XHS_THUMB_W = 120

interface XhsFeedPreviewProps {
  title: string
  author: string
  cover: ReactNode
}

interface SkeletonCardProps {
  imageHeight: number
  lines?: number
}

function SkeletonCard({ imageHeight, lines = 2 }: SkeletonCardProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="rounded-[10px] bg-[#e7e7ec]" style={{ height: imageHeight }} />
      <div className="h-2 w-[82%] rounded-full bg-[#e7e7ec]" />
      {lines > 1 ? <div className="h-2 w-[55%] rounded-full bg-[#e7e7ec]" /> : null}
    </div>
  )
}

export default function XhsFeedPreview({ title, author, cover }: XhsFeedPreviewProps) {
  const { t } = useTranslation()
  return (
    <div className="w-[272px] shrink-0 overflow-hidden rounded-[24px] border border-[#ececf0] bg-white shadow-[0_10px_40px_rgba(38,33,92,0.14)]">
      <div className="flex items-center justify-between px-4 pb-1 pt-2 text-[11px] font-semibold text-[#1d1d1f]">
        <span>21:43</span>
        <div className="flex items-center gap-1.5">
          <div className="flex items-end gap-[1.5px]">
            {[3, 5, 7, 9].map((h) => (
              <div key={h} className="w-[2px] rounded-[1px] bg-[#1d1d1f]" style={{ height: h }} />
            ))}
          </div>
          <div className="flex items-center gap-[1px]">
            <div className="h-[9px] w-[16px] rounded-[3px] border border-[#1d1d1f] p-[1.5px]">
              <div className="h-full w-[70%] rounded-[1px] bg-[#1d1d1f]" />
            </div>
            <div className="h-[4px] w-[1.5px] rounded-r bg-[#1d1d1f]" />
          </div>
        </div>
      </div>

      <div className="relative flex items-center justify-center py-2">
        <svg
          viewBox="0 0 24 24"
          className="absolute left-3 h-4 w-4"
          fill="none"
          stroke="#1d1d1f"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M15 18l-6-6 6-6" />
        </svg>
        <span className="text-[14px] font-medium text-[#1d1d1f]">{t('cover.xhs.header')}</span>
      </div>

      <div className="mx-3 flex items-center gap-1.5 rounded-lg bg-[#f4f4f6] px-2.5 py-1.5 text-[10px] text-[#9a9aa2]">
        <svg
          viewBox="0 0 24 24"
          className="h-3.5 w-3.5 shrink-0"
          fill="none"
          stroke="#b6b6bd"
          strokeWidth="2"
        >
          <circle cx="12" cy="12" r="9" />
          <path d="M12 11v5" strokeLinecap="round" />
          <circle cx="12" cy="7.5" r="0.7" fill="#b6b6bd" stroke="none" />
        </svg>
        <span>{t('cover.xhs.banner')}</span>
      </div>

      <div className="mt-2 flex gap-2 bg-[#f7f7f8] px-3 pb-3 pt-3">
        <div className="flex w-[120px] flex-col gap-2">
          <div className="overflow-hidden rounded-[10px] bg-white shadow-[0_1px_4px_rgba(0,0,0,0.06)]">
            <div className="relative aspect-[3/4] w-full overflow-hidden">
              <div className="absolute left-0 top-0">{cover}</div>
            </div>
            <div className="px-2 pb-2 pt-1.5">
              <p className="line-clamp-2 text-[11px] font-medium leading-snug text-[#33333a]">
                {title}
              </p>
              <div className="mt-1.5 flex items-center gap-1">
                <span className="h-4 w-4 shrink-0 rounded-full bg-gradient-to-br from-[#8a7ff0] to-[#c98bd6]" />
                <span className="flex-1 truncate text-[10px] text-[#9a9aa2]">{author}</span>
              </div>
            </div>
          </div>
          <SkeletonCard imageHeight={104} lines={1} />
        </div>

        <div className="flex w-[120px] flex-col gap-2">
          <SkeletonCard imageHeight={150} />
          <SkeletonCard imageHeight={92} />
        </div>
      </div>

      <div className="px-4 pb-4 pt-1">
        <div className="w-full rounded-full bg-[#ff2442] py-2 text-center text-[13px] font-semibold text-white">
          {t('cover.xhs.publish')}
        </div>
      </div>
    </div>
  )
}
