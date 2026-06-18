// i18n setup: Chinese + English, default Chinese, language choice persisted in localStorage.
// Usage: import './i18n' at the top of main.tsx; use useTranslation() in components and i18n.t in non-component utils.

import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zh from './locales/zh.json'
import en from './locales/en.json'

export type Lang = 'zh' | 'en'

const STORAGE_KEY = 'lang'

function initialLang(): Lang {
  const saved = localStorage.getItem(STORAGE_KEY)
  return saved === 'en' || saved === 'zh' ? saved : 'zh'
}

function syncHtmlLang(lng: string): void {
  document.documentElement.lang = lng === 'en' ? 'en' : 'zh-CN'
}

void i18n.use(initReactI18next).init({
  resources: {
    zh: { translation: zh },
    en: { translation: en },
  },
  lng: initialLang(),
  fallbackLng: 'zh',
  interpolation: { escapeValue: false },
})

syncHtmlLang(i18n.language)
i18n.on('languageChanged', (lng) => {
  localStorage.setItem(STORAGE_KEY, lng)
  syncHtmlLang(lng)
})

export default i18n
