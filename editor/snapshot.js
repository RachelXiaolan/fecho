// 日报截图：把渲染好的日报整块画成 PNG（多长都行，不用滚动拼接）。
// 打包成 fecho/presets/vendor/snapshot/snapshot.min.js，页面上是 window.FechoSnapshot
import {toBlob} from 'html-to-image'

window.FechoSnapshot = {
  // node：要截的元素；skip：返回 true 的元素不画（折叠箭头这类）
  toPng(node, {background, skip, pixelRatio = 2} = {}) {
    return toBlob(node, {
      pixelRatio,
      backgroundColor: background,
      cacheBust: true,
      filter: (el) => !(skip && el.nodeType === 1 && skip(el)),
    })
  },
}
