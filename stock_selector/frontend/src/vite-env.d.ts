/// <reference types="vite/client" />

/** 让 TS 认识 .css 等非代码资源的导入 */
declare module '*.css' {
  const content: string
  export default content
}
