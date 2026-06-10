import { NavLink, Route, Routes } from 'react-router-dom'
import AskPage from './pages/AskPage'
import SimPage from './pages/SimPage'

export default function App() {
  return (
    <>
      <nav className="topnav">
        <NavLink to="/" end>
          课程问答
        </NavLink>
        <NavLink to="/sim">选课模拟器</NavLink>
      </nav>
      <Routes>
        <Route path="/" element={<AskPage />} />
        <Route path="/sim" element={<SimPage />} />
      </Routes>
    </>
  )
}
