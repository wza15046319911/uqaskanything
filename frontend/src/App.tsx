import { NavLink, Route, Routes } from 'react-router-dom'
import { Button, Toast, useTheme } from '@heroui/react'
import AskPage from './pages/AskPage'
import SimPage from './pages/SimPage'
import CoverPage from './pages/CoverPage'

function navCls({ isActive }: { isActive: boolean }): string {
  return `rounded-full px-4 py-1.5 text-[13.5px] font-semibold transition-colors ${
    isActive
      ? 'bg-accent-soft text-accent-soft-foreground'
      : 'text-muted hover:bg-default-soft hover:text-foreground'
  }`
}

export default function App() {
  const { resolvedTheme, setTheme } = useTheme('light')
  const dark = resolvedTheme === 'dark'

  return (
    <div className="min-h-dvh bg-background font-sans text-foreground">
      <Toast.Provider />
      <nav className="relative flex items-center justify-center gap-1 px-5 pt-4">
        <NavLink to="/" end className={navCls}>
          UQ 问答
        </NavLink>
        <NavLink to="/sim" className={navCls}>
          选课模拟器
        </NavLink>
        <NavLink to="/cover" className={navCls}>
          封面生成
        </NavLink>
        <Button
          isIconOnly
          size="sm"
          variant="ghost"
          aria-label="Toggle dark mode"
          className="absolute right-5"
          onPress={() => setTheme(dark ? 'light' : 'dark')}
        >
          {dark ? (
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32 1.41-1.41" />
            </svg>
          ) : (
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
          )}
        </Button>
      </nav>
      <Routes>
        <Route path="/" element={<AskPage />} />
        <Route path="/sim" element={<SimPage />} />
        <Route path="/cover" element={<CoverPage />} />
      </Routes>
    </div>
  )
}
