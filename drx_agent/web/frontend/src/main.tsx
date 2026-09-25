import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import './index.css'

const root = document.getElementById('root');
if (!root) throw new Error('页面根节点不存在');

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
