import App from './App.js'

const React = window.React
const ReactDOM = window.ReactDOM

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到 #root 挂载节点')
}

ReactDOM.createRoot(container).render(React.createElement(React.StrictMode, null, React.createElement(App)))