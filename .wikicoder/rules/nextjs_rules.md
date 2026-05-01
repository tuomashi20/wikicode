# Next.js 与 React 组件化开发强制约束

当你在处理基于 Next.js (App Router)、React 或类似前端组件化框架的项目时，必须严格遵守以下法则：

## 1. 杜绝 HTML 骨架幻觉（防 Hydration Error）
当你被要求“创建/重写一个页面”或“实现核心 UI”（例如编辑 `src/app/page.tsx` 或任何 `.tsx` 组件）时：
- **绝对禁止**在文件中包含完整的 `<html>`、`<head>` 或 `<body>` 等全局 HTML 骨架标签！
- **仅需返回组件的核心内容**（如 `<main>`、`<div>` 或具体的 React 组件结构）。
- **原因**：框架会自动将你的组件内容渲染到全局的 `layout.tsx` 中。如果你在页面组件里再次定义了 HTML 骨架，将会导致 `<html>` 标签互相嵌套，从而引发灾难性的 Hydration Error (水合错误) 与 DOM 冲突。

## 2. 区分 Layout 与 Page
- **Layout (如 `layout.tsx`)**：负责全局样式载入（如字体、CSS）、元数据（Metadata）以及全局的 `<html>` 和 `<body>` 标签。
- **Page (如 `page.tsx`)**：仅负责当前路由下的具体业务视图，它会被作为 `children` 传入 Layout。

## 3. 服务端/客户端组件分离
- 如果组件内使用了 React Hooks（如 `useState`, `useEffect`）或处理了浏览器事件（如 `onClick`），必须在文件顶部声明 `"use client";`。
- 如果无需状态交互，优先保持为服务端组件（无需声明）。
