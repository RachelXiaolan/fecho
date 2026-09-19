# 日报编辑器（CodeMirror 6）

日报编辑框的实时渲染层。写成 Obsidian 那套语义：**平时渲染成样子，光标移到哪一行，哪一行才露出 Markdown 原文**。

- 源码就是 `entry.js` 一个文件
- 产物是 `../fecho/presets/vendor/cm6/editor.min.js`，**已经提交进仓库**，
  部署时不需要装 node——只有改编辑器时才要重新构建

## 改完怎么重新构建

```
cd editor
npm install          # 第一次才要
./build.sh
```

`build.sh` 会把产物写回 `fecho/presets/vendor/cm6/editor.min.js`，记得一起提交。

## 配色

主题里的颜色**全部走 CSS 变量**（`var(--ink)` `var(--surface)` `var(--ed-code-bg)` …），
定义在 `fecho/presets/dashboard.html` 的 `:root` 和 `:root[data-theme="dark"]` 里。
所以切换白天/黑夜时编辑器不需要重建，变量变了样式自己就跟着变。

## 页面怎么调

```js
FechoEditor.create(根元素, 原文, {onImages: files => ...})   // 建
FechoEditor.value()                                          // 取原文（会先把表格里没失焦的改动写回）
FechoEditor.insert('![图|480](地址)')                        // 在光标处插一段
FechoEditor.focus()
FechoEditor.destroy()
```

## 两个坑，改的时候注意

1. **表格不能跟着选区重建。** 表格是可以直接编辑的（单元格 contenteditable），
   跟着选区重建会把正在输入的焦点弄丢，所以 `tableField` 只在 `tr.docChanged` 时重建。
2. **widget 里不要记死文档坐标。** widget 的 DOM 会被复用，而它记下的位置会随上文编辑漂移；
   按旧坐标写回正文会把别处的内容冲掉。要改正文时一律用 `posOfWidget()` 现查位置。
