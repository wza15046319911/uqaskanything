import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
import { Button, ListBox, Select, Toast, useTheme } from '@heroui/react'
import { useTranslation } from 'react-i18next'
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
  const { t, i18n } = useTranslation()
  const lang = i18n.language === 'en' ? 'en' : 'zh'
  const isProd = import.meta.env.PROD

  return (
    <div className="min-h-dvh bg-background font-sans text-foreground">
      <Toast.Provider />
      <nav className="relative flex items-center justify-center gap-1 px-5 pt-4">
        {!isProd && (
          <>
            <NavLink to="/" end className={navCls}>
              {t('nav.ask')}
            </NavLink>
            <NavLink to="/sim" className={navCls}>
              {t('nav.sim')}
            </NavLink>
            <NavLink to="/cover" className={navCls}>
              {t('nav.cover')}
            </NavLink>
          </>
        )}
        <div className="absolute right-5 flex items-center gap-1.5">
          <Select
            className="w-28"
            aria-label={t('nav.toggleLang')}
            selectedKey={lang}
            onSelectionChange={(k) => k != null && i18n.changeLanguage(String(k))}
          >
            <Select.Trigger>
              <Select.Value />
              <Select.Indicator />
            </Select.Trigger>
            <Select.Popover>
              <ListBox>
                <ListBox.Item id="zh" textValue="中文">
                  中文
                  <ListBox.ItemIndicator />
                </ListBox.Item>
                <ListBox.Item id="en" textValue="English">
                  English
                  <ListBox.ItemIndicator />
                </ListBox.Item>
              </ListBox>
            </Select.Popover>
          </Select>
          <Button
            isIconOnly
            size="sm"
            variant="ghost"
            aria-label={t('nav.toggleTheme')}
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
        </div>
      </nav>
      <Routes>
        <Route path="/" element={<AskPage />} />
        {!isProd && <Route path="/sim" element={<SimPage />} />}
        {!isProd && <Route path="/cover" element={<CoverPage />} />}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  )
}
