export {}

declare global {
  interface Window {
    React: typeof import('react')
    ReactDOM: any
  }
}