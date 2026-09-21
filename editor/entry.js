import {EditorState, StateField, Prec, EditorSelection} from '@codemirror/state'
import {EditorView, Decoration, WidgetType, ViewPlugin, keymap, drawSelection, highlightActiveLine} from '@codemirror/view'
import {defaultKeymap, history, historyKeymap, indentMore, indentLess} from '@codemirror/commands'
import {markdown, markdownLanguage} from '@codemirror/lang-markdown'
import {syntaxTree, syntaxHighlighting, HighlightStyle, foldService, foldCode, unfoldCode, foldedRanges, indentUnit} from '@codemirror/language'
import {tags as t} from '@lezer/highlight'

// ---------- 小工具 ----------
const activeLines = (state) => {
  const set = new Set()
  for (const r of state.selection.ranges) {
    const a = state.doc.lineAt(r.from).number, b = state.doc.lineAt(r.to).number
    for (let n = a; n <= b; n++) set.add(n)
  }
  return set
}

// widget 的 DOM 还在，但它记下的文档坐标会随上文编辑漂移；要改正文就现查一次
const posOfWidget = (view, dom) => { try { return view.posAtDOM(dom) } catch (_) { return null } }

const imageRangeAt = (view, dom) => {
  const pos = posOfWidget(view, dom)
  if (pos == null) return null
  let found = null
  syntaxTree(view.state).iterate({
    from: Math.max(0, pos - 1), to: Math.min(view.state.doc.length, pos + 1),
    enter: (n) => { if (n.name === 'Image' && !found) found = {from: n.from, to: n.to} },
  })
  return found
}

class Widget extends WidgetType {
  constructor(build, key) { super(); this.build = build; this.key = key }
  eq(other) { return other.key === this.key }
  toDOM() { return this.build() }
  ignoreEvent() { return false }
}
const widget = (key, build, opts = {}) => Decoration.replace({widget: new Widget(build, key), ...opts})

// 日报的一个缩进层级固定是两个空格。竖线属于祖先分支：直接子项从父项列起画一条，
// 再往里一层便保留父线、并在直接父项的列再叠一条。
function indentationDepth(text) {
  const leading = (/^[\t ]*/.exec(text) || [''])[0]
  return Math.floor(leading.replace(/\t/g, '    ').length / 2)
}

function addIndentGuides(line, add) {
  const depth = indentationDepth(line.text)
  if (depth < 1 || !line.text.trim()) return
  add(line.from, line.from, Decoration.widget({
    side: -2,
    widget: new Widget(() => {
      const guides = document.createElement('span')
      guides.className = 'md-indent-guides'
      // 当前在第 2 层时，给顶层和一级父项各留一条线；线落在祖先的起点。
      for (let level = 0; level < depth; level++) {
        const guide = document.createElement('span')
        guide.className = 'md-indent-guide'
        guide.style.left = (level * 2) + 'ch'
        guides.appendChild(guide)
      }
      return guides
    }, 'indent-guides-' + depth),
  }))
}

// ---------- 行首标记：标题 / 项目符号 / 序号 / 勾选框 / 引用 ----------
function blockDecorations(view, active, add) {
  const {state} = view
  for (const {from, to} of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = state.doc.lineAt(pos)
      const text = line.text
      const on = active.has(line.number)      // 光标在这一行：露出原始标记
      addIndentGuides(line, add)
      let m
      if ((m = /^(#{1,6})\s+/.exec(text))) {
        add(line.from, line.from, Decoration.line({class: 'md-h md-h' + m[1].length}))
        if (!on) add(line.from, line.from + m[0].length, Decoration.replace({}))
      } else {
        // 也接受 `[ ] 1. 内容`：虽然不是 CommonMark 的惯用顺序，但日报里已经有这种写法，
        // 编辑时照样显示成「编号 + 勾选框」，光标移进来才露出原文。
        const taskFirst=/^(\s*)\[(.)\](\s+)(\d+[.)])(\s+)(.*)$/.exec(text)
        if (taskFirst) {
          const mark = taskFirst[2]
          const done = /^[xX]$/.test(mark), checked = mark !== ' '
          add(line.from, line.from, Decoration.line({class: 'md-task'}))
          const bodyFrom = line.from + taskFirst[0].length
          if (done && !on && bodyFrom < line.to) add(bodyFrom, line.to, Decoration.mark({class: 'md-task-done'}))
          if (!on) {
            const indent = line.from + taskFirst[1].length, label = taskFirst[4]
            add(indent, bodyFrom, widget('numcb' + label + mark, () => {
              const wrap = document.createElement('span')
              const num = document.createElement('span'); num.className = 'md-num'; num.textContent = label
              const box = document.createElement('span')
              box.className = 'md-checkbox' + (checked ? ' on' : '') + (done ? ' done' : '')
              box.dataset.pos = String(indent); box.textContent = checked ? '✓' : ''
              wrap.appendChild(num); wrap.appendChild(box); return wrap
            }))
          }
        } else if ((m = /^(\s*)([-*+]|\d+[.)])(\s+)\[(.)\]\s?/.exec(text))) {
        // 待办：无序 `- [ ]` 和有序 `1. [x]` 都算。有序的要既显示序号又显示勾选框
        const mark = m[4]
        const done = /^[xX]$/.test(mark), checked = mark !== ' '
        add(line.from, line.from, Decoration.line({class: 'md-task'}))
        // 后面还有字才划掉。`- [x] ` 这种空的一项范围是零长度，
        // CM6 不收空的 mark 装饰，会连累整个渲染层被禁用
        const bodyFrom = line.from + m[0].length
        if (done && !on && bodyFrom < line.to) {
          add(bodyFrom, line.to, Decoration.mark({class: 'md-task-done'}))
        }
        if (!on) {
          const indent = line.from + m[1].length
          const markerEnd = indent + m[2].length + m[3].length
          if (/^\d/.test(m[2])) {
            const label = m[2]
            add(indent, markerEnd, widget('num' + label, () => {
              const s = document.createElement('span'); s.className = 'md-num'; s.textContent = label; return s }))
          } else {
            add(indent, markerEnd, Decoration.replace({}))
          }
          add(markerEnd, line.from + m[0].length, widget('cb' + mark, () => {
            const box = document.createElement('span')
            box.className = 'md-checkbox' + (checked ? ' on' : '') + (done ? ' done' : '')
            box.dataset.pos = String(indent)
            box.textContent = checked ? '✓' : ''        // [x] [a] [/] 都画对勾，不显示字符本身
            return box
          }))
        }
        } else if ((m = /^(\s*)([-*+])(\s+)/.exec(text))) {
        if (!on) {
          const start = line.from + m[1].length
          add(start, start + m[2].length + m[3].length,
              widget('bullet', () => { const s = document.createElement('span'); s.className = 'md-bullet'; s.textContent = '•'; return s }))
        }
        } else if ((m = /^(\s*)(\d+)([.)])(\s+)/.exec(text))) {
        if (!on) {
          const start = line.from + m[1].length
          const label = m[2] + m[3]
          add(start, start + label.length + m[4].length,
              widget('num' + label, () => { const s = document.createElement('span'); s.className = 'md-num'; s.textContent = label; return s }))
        }
        } else if ((m = /^(\s*)(>\s?)/.exec(text))) {
        add(line.from, line.from, Decoration.line({class: 'md-quote'}))
        if (!on) add(line.from + m[1].length, line.from + m[0].length, Decoration.replace({}))
        } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(text) && text.trim()) {
        if (!on) add(line.from, line.to, widget('hr', () => { const s = document.createElement('span'); s.className = 'md-hr'; return s }))
        }
      }
      if (line.to + 1 > to) break
      pos = line.to + 1
    }
  }
}

// ---------- 行内：加粗 / 斜体 / 删除线 / 行内代码 / 链接 / 图片 / 表格 ----------
const HIDE_MARKS = new Set(['EmphasisMark', 'CodeMark', 'StrikethroughMark'])

function inlineDecorations(view, active, add) {
  const {state} = view
  for (const {from, to} of view.visibleRanges) {
    syntaxTree(state).iterate({
      from, to,
      enter: (node) => {
        const lineOn = (p) => active.has(state.doc.lineAt(p).number)
        if (node.name === 'Image') {
          // 图片永远保持渲染：日报里的图是 data URI，一旦露出原文就是一屏乱码。
          // 改大小用右下角的把手，不需要看见原文
          const raw = state.doc.sliceString(node.from, node.to)
          const m = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(raw)
          if (!m) return
          const parts = m[1].split('|')
          let width = ''
          if (parts.length > 1 && /^\d+(x\d+)?$/.test(parts[parts.length - 1].trim())) width = parts.pop().trim().split('x')[0]
          const alt = parts.join('|'), url = m[2]
          add(node.from, node.to, widget('img' + raw, () => {
            const wrap = document.createElement('span')
            wrap.className = 'md-img'
            const img = document.createElement('img')
            img.src = url; img.alt = alt
            if (width) img.style.width = width + 'px'
            const grip = document.createElement('span')
            grip.className = 'md-img-grip'
            grip.title = '拖动改图片宽度'
            const badge = document.createElement('span')
            badge.className = 'md-img-size'
            grip.addEventListener('pointerdown', (e) => {
              e.preventDefault(); e.stopPropagation()
              grip.setPointerCapture(e.pointerId)
              const startX = e.clientX, startW = img.getBoundingClientRect().width
              img.style.maxWidth = 'none'
              wrap.classList.add('resizing')
              badge.textContent = Math.round(startW) + 'px'
              const move = (ev) => {
                const w = Math.max(60, Math.round(startW + ev.clientX - startX))
                img.style.width = w + 'px'; badge.textContent = w + 'px'
              }
              const up = () => {
                grip.removeEventListener('pointermove', move)
                grip.removeEventListener('pointerup', up)
                grip.removeEventListener('pointercancel', up)
                img.style.maxWidth = ''
                wrap.classList.remove('resizing')
                const w = Math.round(img.getBoundingClientRect().width)
                const view = EditorView.findFromDOM(wrap.closest('.cm-editor'))
                // 位置不能用建 widget 时记下的：上文一改就漂了，按旧坐标写回会把别处的正文冲掉
                const at = view && imageRangeAt(view, wrap)
                if (at) view.dispatch({changes: {from: at.from, to: at.to,
                  insert: '![' + alt + '|' + w + '](' + url + ')'}})
              }
              grip.addEventListener('pointermove', move)
              grip.addEventListener('pointerup', up)
              grip.addEventListener('pointercancel', up)
            })
            wrap.appendChild(img); wrap.appendChild(badge); wrap.appendChild(grip)
            return wrap
          }), true)
          return false
        }
        if (HIDE_MARKS.has(node.name) && !lineOn(node.from)) {
          add(node.from, node.to, Decoration.replace({}))
        }
        // 文字本身也要上样式，不是只把 ** 藏起来就完事
        if (node.name === 'StrongEmphasis') add(node.from, node.to, Decoration.mark({class: 'md-strong'}))
        if (node.name === 'Emphasis') add(node.from, node.to, Decoration.mark({class: 'md-em'}))
        if (node.name === 'Strikethrough') add(node.from, node.to, Decoration.mark({class: 'md-strike'}))
        if (node.name === 'InlineCode') add(node.from, node.to, Decoration.mark({class: 'md-code'}))
        // 代码块：整块上底色和等宽字，围栏那两行不编辑时藏起来
        if (node.name === 'FencedCode') {
          const first = state.doc.lineAt(node.from), last = state.doc.lineAt(node.to)
          let on = false
          for (let n = first.number; n <= last.number; n++) if (active.has(n)) on = true
          for (let n = first.number; n <= last.number; n++) {
            const l = state.doc.line(n)
            const edge = (n === first.number ? ' md-code-top' : '') + (n === last.number ? ' md-code-bottom' : '')
            add(l.from, l.from, Decoration.line({class: 'md-code-line' + edge}))
          }
          if (!on) {
            add(first.from, first.to, Decoration.replace({}))
            if (last.number !== first.number) add(last.from, last.to, Decoration.replace({}))
          }
          return false
        }
        if (node.name === 'Link' && !lineOn(node.from)) {
          const raw = state.doc.sliceString(node.from, node.to)
          const m = /^\[([^\]]*)\]\(([^)]+)\)$/.exec(raw)
          if (m) {
            add(node.from, node.from + 1, Decoration.replace({}))
            add(node.from + 1 + m[1].length, node.to, Decoration.replace({}))
            add(node.from + 1, node.from + 1 + m[1].length, Decoration.mark({class: 'md-link'}))
          }
          // 这里**不能**停下：链接文字里还可能有 **加粗**、`代码`，
          // 不往下走的话那些标记永远藏不掉，光标没过去也露着原文
        }
      },
    })
  }
}


// ---------- 表格 ----------
// 和别的块不一样：光标移进去也不变回 Markdown，一直保持表格的样子。
// 代价是表格必须能直接改——单元格是 contenteditable，焦点离开表格时才写回原文。
// 列宽行高 Markdown 本身没有语法，存成表格后面跟的一行 HTML 注释。
const TABLE_SIZE_RE = /^<!--\s*fecho-table\s+(.*?)\s*-->\s*$/

function parseTableSizes(text) {
  const m = TABLE_SIZE_RE.exec(text)
  if (!m) return null
  const out = {w: [], h: []}
  for (const part of m[1].split(/\s+/)) {
    const eq = part.indexOf('=')
    if (eq < 0) continue
    const key = part.slice(0, eq)
    const nums = part.slice(eq + 1).split(',').map((n) => Math.max(0, Math.round(+n) || 0))
    if (key === 'w') out.w = nums
    if (key === 'h') out.h = nums
  }
  return out
}

// 单元格里的 | 要转义，换行压成空格——表格一行就是一行
const cellOut = (s) => s.replace(/\s+/g, ' ').replace(/\|/g, '\\|').trim()
const splitCells = (row) => {
  const s = row.trim().replace(/^\|/, '').replace(/\|$/, '')
  const out = []
  let cur = ''
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue }
    if (s[i] === '|') { out.push(cur.trim()); cur = ''; continue }
    cur += s[i]
  }
  out.push(cur.trim())
  return out
}

class TableWidget extends WidgetType {
  constructor(raw, sizes) { super(); this.raw = raw; this.sizes = sizes }
  eq(other) { return other.raw === this.raw }
  ignoreEvent() { return true }          // 单元格里的输入交给浏览器，CM6 不插手
  toDOM() { return buildTableDOM(this.raw, this.sizes) }
}

function buildTableDOM(raw, sizes) {
  const lines = raw.split('\n').filter((r) => r.includes('|'))
  const head = splitCells(lines[0] || '')
  const body = lines.slice(2).map(splitCells)

  const table = document.createElement('table')
  table.className = 'md-table'
  table.contentEditable = 'false'

  const colgroup = document.createElement('colgroup')
  head.forEach((_, i) => {
    const col = document.createElement('col')
    const w = sizes && sizes.w[i]
    if (w) col.style.width = w + 'px'
    colgroup.appendChild(col)
  })
  table.appendChild(colgroup)

  const allRows = []
  const headRow = table.createTHead().insertRow()
  allRows.push(headRow)
  head.forEach((text) => { headRow.appendChild(dressCell(document.createElement('th'), text, true)) })
  const tb = table.createTBody()
  body.forEach((cells) => {
    const tr = tb.insertRow()
    allRows.push(tr)
    head.forEach((_, i) => { tr.appendChild(dressCell(document.createElement('td'), cells[i] || '', false)) })
  })
  allRows.forEach((tr, i) => { const h = sizes && sizes.h[i]; if (h) tr.style.height = h + 'px' })

  // 表格在文档里的真实范围：位置会随上文编辑漂移，所以每次提交时现找，不记死
  const rangeNow = (view) => {
    const first = view.state.doc.lineAt(view.posAtDOM(table))
    let last = first
    for (let n = first.number + 1; n <= view.state.doc.lines; n++) {
      const ln = view.state.doc.line(n)
      if (ln.text.includes('|') || TABLE_SIZE_RE.test(ln.text)) last = ln
      else break
    }
    return {from: first.from, to: last.to}
  }

  const serialize = () => {
    // 把手是单元格的子元素，取文字前先剔掉，免得把它们算进内容里
    const textOf = (c) => {
      const copy = c.cloneNode(true)
      copy.querySelectorAll('.md-col-grip,.md-row-grip').forEach((g) => g.remove())
      return cellOut(copy.textContent)
    }
    const cells = (tr) => [...tr.cells].map(textOf)
    const heads = cells(table.tHead.rows[0])
    const rows = [...table.tBodies[0].rows].map(cells)
    const line = (cs) => '| ' + cs.join(' | ') + ' |'
    const out = [line(heads), line(heads.map(() => '---')), ...rows.map(line)]
    const ws = [...colgroup.children].map((c) => parseInt(c.style.width, 10) || 0)
    const hs = [table.tHead.rows[0], ...table.tBodies[0].rows].map((r) => parseInt(r.style.height, 10) || 0)
    if (ws.some(Boolean) || hs.some(Boolean)) {
      out.push('<!-- fecho-table w=' + ws.join(',') + ' h=' + hs.join(',') + ' -->')
    }
    return out.join('\n')
  }

  const commit = () => {
    const root = table.closest('.cm-editor')
    const view = root && EditorView.findFromDOM(root)
    if (!view) return
    const {from, to} = rangeNow(view)
    const next = serialize()
    if (next === view.state.doc.sliceString(from, to)) return
    view.dispatch({changes: {from, to, insert: next}})
  }
  table.__commit = commit

  // 焦点整个离开表格才写回，边打字边写回会把 widget 重建掉、焦点就没了
  table.addEventListener('focusout', (e) => {
    if (!table.contains(e.relatedTarget)) commit()
  })
  table.addEventListener('keydown', (e) => {
    // 格子是 contenteditable，⌘B 会被浏览器接管、插一个 <b> 标签进来；
    // 而写回原文时只读纯文本，那层格式会悄无声息地消失。宁可不让它生效
    if ((e.metaKey || e.ctrlKey) && 'biu'.includes(e.key.toLowerCase())) {
      e.preventDefault(); return
    }
    if (e.key === 'Enter') { e.preventDefault(); return }          // 单元格就一行
    if (e.key === 'Tab') {                                          // Tab 在单元格之间走
      const all = [...table.querySelectorAll('th,td')]
      const i = all.indexOf(e.target.closest('th,td'))
      const next = all[i + (e.shiftKey ? -1 : 1)]
      if (next) { e.preventDefault(); next.focus() }
    }
  })

  addResizeGrips(table, colgroup, commit)
  return table
}

function dressCell(cell, text, isHead) {
  cell.textContent = text
  cell.contentEditable = 'true'
  cell.spellcheck = false
  if (isHead) cell.appendChild(makeGrip('md-col-grip'))
  cell.appendChild(makeGrip('md-row-grip'))
  return cell
}
function makeGrip(cls) {
  const g = document.createElement('span')
  g.className = cls
  g.contentEditable = 'false'
  return g
}

function addResizeGrips(table, colgroup, commit) {
  table.addEventListener('pointerdown', (e) => {
    const col = e.target.closest('.md-col-grip')
    const row = e.target.closest('.md-row-grip')
    if (!col && !row) return
    e.preventDefault(); e.stopPropagation()
    const grip = col || row
    grip.setPointerCapture(e.pointerId)
    table.classList.add('sizing')

    const cell = grip.closest('th,td')
    const index = [...cell.parentElement.cells].indexOf(cell)
    const target = col ? colgroup.children[index] : cell.parentElement
    const startX = e.clientX, startY = e.clientY
    const box = cell.getBoundingClientRect()
    const startW = box.width, startH = cell.parentElement.getBoundingClientRect().height

    const move = (ev) => {
      if (col) target.style.width = Math.max(48, Math.round(startW + ev.clientX - startX)) + 'px'
      else target.style.height = Math.max(26, Math.round(startH + ev.clientY - startY)) + 'px'
    }
    const up = () => {
      grip.removeEventListener('pointermove', move)
      grip.removeEventListener('pointerup', up)
      grip.removeEventListener('pointercancel', up)
      table.classList.remove('sizing')
      commit()
    }
    grip.addEventListener('pointermove', move)
    grip.addEventListener('pointerup', up)
    grip.addEventListener('pointercancel', up)
  })
}

// 表格是块级装饰（跨行），CM6 规定这类只能由 StateField 提供，不能从 ViewPlugin 出
function buildTables(state) {
  const items = []
  syntaxTree(state).iterate({
    from: 0, to: state.doc.length,
    enter: (node) => {
      if (node.name !== 'Table') return
      const startLine = state.doc.lineAt(node.from)
      let endLine = state.doc.lineAt(node.to)
      let sizes = null
      if (endLine.number < state.doc.lines) {
        const next = state.doc.line(endLine.number + 1)
        const got = parseTableSizes(next.text)
        if (got) { sizes = got; endLine = next }   // 把尺寸注释一起吞进 widget，正文里就看不见了
      }
      const raw = state.doc.sliceString(startLine.from, endLine.to)
      items.push({from: startLine.from, to: endLine.to,
        deco: Decoration.replace({widget: new TableWidget(raw, sizes), block: true})})
      return false
    },
  })
  return Decoration.set(items.map((it) => it.deco.range(it.from, it.to)), true)
}

// 只在正文变了的时候重建。跟着选区重建会把正在编辑的单元格焦点弄丢
const tableField = StateField.define({
  create: (state) => buildTables(state),
  update: (deco, tr) => tr.docChanged ? buildTables(tr.state) : deco.map(tr.changes),
  provide: (f) => EditorView.decorations.from(f),
})

// ---------- 折叠：标题按层级，列表按缩进 ----------
const foldRule = foldService.of((state, lineStart, lineEnd) => {
  const line = state.doc.lineAt(lineStart)
  const h = /^(#{1,6})\s/.exec(line.text)
  if (h) {
    const level = h[1].length
    for (let n = line.number + 1; n <= state.doc.lines; n++) {
      const m = /^(#{1,6})\s/.exec(state.doc.line(n).text)
      if (m && m[1].length <= level) return n - 1 > line.number ? {from: lineEnd, to: state.doc.line(n - 1).to} : null
    }
    return line.number < state.doc.lines ? {from: lineEnd, to: state.doc.length} : null
  }
  const li = /^(\s*)(?:\[(.)\]\s+\d+[.)]|[-*+]|\d+[.)])\s/.exec(line.text)
  if (li) {
    const indent = li[1].length
    let end = null
    for (let n = line.number + 1; n <= state.doc.lines; n++) {
      const l = state.doc.line(n)
      if (!l.text.trim()) break
      if (/^\s*/.exec(l.text)[0].length <= indent) break
      end = l.to
    }
    return end ? {from: lineEnd, to: end} : null
  }
  return null
})

// 箭头锚在当前项的标记前；编号宽度、字体或缩进变化时都不会压进正文。
function foldArrows(view, add) {
  const {state} = view
  const folded = foldedRanges(state)
  for (const {from, to} of view.visibleRanges) {
    let pos = from
    while (pos <= to) {
      const line = state.doc.lineAt(pos)
      const range = state.facet(foldService).reduce((acc, f) => acc || f(state, line.from, line.to), null)
      if (range) {
        let isFolded = false
        folded.between(range.from, range.to, () => { isFolded = true })
        const markerFrom = line.from + (/^[\t ]*/.exec(line.text) || [''])[0].length
        add(markerFrom, markerFrom, Decoration.widget({
          side: -1,
          widget: new Widget(() => {
            const b = document.createElement('span')
            b.className = 'md-fold' + (isFolded ? ' folded' : '')
            b.dataset.line = String(line.from)
            b.textContent = '▾'
            return b
          }, 'fold' + markerFrom + isFolded),
        }))
      }
      if (line.to + 1 > to) break
      pos = line.to + 1
    }
  }
}

// ---------- 组装 ----------
const livePreview = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = this.build(view) }
  update(u) { if (u.docChanged || u.selectionSet || u.viewportChanged) this.decorations = this.build(u.view) }
  build(view) {
    const active = view.hasFocus ? activeLines(view.state) : new Set()
    const items = []
    // 零长度的 mark 装饰会让 CM6 抛错、把整个插件停掉，整页就一个标记都不渲染了。
    // deco.point 是 CM6 自己的判别：mark 是 false，widget 和行装饰是 true（它们本来就可以零长）
    const add = (from, to, deco, block) => {
      if (from === to && deco.point === false) return
      items.push({from, to, deco, block})
    }
    blockDecorations(view, active, add)
    inlineDecorations(view, active, add)
    foldArrows(view, add)
    return Decoration.set(items.map(it => it.deco.range(it.from, it.to)), true)
  }
}, {decorations: v => v.decorations})

// 点勾选框切换状态；点箭头折叠
const clicks = EditorView.domEventHandlers({
  mousedown(event, view) {
    const box = event.target.closest('.md-checkbox')
    if (box) {
      const at = posOfWidget(view, box)
      if (at == null) { event.preventDefault(); return true }
      const line = view.state.doc.lineAt(at)
      // 无序的 `- [ ]` 和有序的 `2. [ ]` 都要能点
      const taskFirst = /^(\s*)\[(.)\](\s+)(\d+[.)])(\s+)/.exec(line.text)
      const m = taskFirst || /^(\s*)([-*+]|\d+[.)])(\s+)\[(.)\]/.exec(line.text)
      if (m) {
        const markPos = taskFirst ? line.from + taskFirst[1].length + 1
          : line.from + m[1].length + m[2].length + m[3].length + 1
        const now = view.state.doc.sliceString(markPos, markPos + 1)
        // 点一下默认勾成 [a]：打勾但不划掉。已经勾上的再点回 [ ]
        view.dispatch({changes: {from: markPos, to: markPos + 1, insert: now === ' ' ? 'a' : ' '}})
      }
      event.preventDefault(); return true
    }
    const arrow = event.target.closest('.md-fold')
    if (arrow) {
      const pos = posOfWidget(view, arrow)
      if (pos == null) { event.preventDefault(); return true }
      view.dispatch({selection: {anchor: view.state.doc.lineAt(pos).from}})
      if (arrow.classList.contains('folded')) unfoldCode(view); else foldCode(view)
      event.preventDefault(); return true
    }
    return false
  },
})

// ---------- 列表的键盘行为（从 0.8.1 的 textarea 版原样搬过来，照 Obsidian 的习惯） ----------
// 这几个都是纯函数：进去是 (全文, 光标)，出来是新的全文和新光标位置
const LIST_LINE = /^(\s*)(?:([-*+])|(\d+)([.)]))(\s+)(\[.\]\s+)?(.*)$/
const INDENT = '  '
const indentWidth = (s) => s.replace(/\t/g, '    ').length
function lineBounds(value, pos) {
  const start = value.lastIndexOf('\n', pos - 1) + 1
  let end = value.indexOf('\n', pos)
  if (end < 0) end = value.length
  return [start, end]
}

// 只重排光标所在的这一个列表（遇到空行就算另一个列表），每一层从这一层第一项的编号往下数
function renumberList(value, caret) {
  const lines = value.split('\n'), row = value.slice(0, caret).split('\n').length - 1
  const inBlock = (line) => line.trim() !== '' && (LIST_LINE.test(line) || /^\s/.test(line))
  if (!inBlock(lines[row] || '')) return {value, caret}
  let a = row, b = row
  while (a > 0 && inBlock(lines[a - 1])) a--
  while (b < lines.length - 1 && inBlock(lines[b + 1])) b++
  const col = caret - (value.lastIndexOf('\n', caret - 1) + 1), stack = []
  let shift = 0
  for (let i = a; i <= b; i++) {
    const m = lines[i].match(LIST_LINE)
    if (!m) continue
    const ind = indentWidth(m[1])
    while (stack.length && stack[stack.length - 1].indent > ind) stack.pop()
    let top = stack.length && stack[stack.length - 1].indent === ind ? stack[stack.length - 1] : null
    if (m[3] === undefined) { if (top) top.next = null; else stack.push({indent: ind, next: null}); continue }
    let num
    if (top && top.next !== null) num = top.next
    else { num = parseInt(m[3], 10); if (top) stack.pop(); top = {indent: ind, next: 0}; stack.push(top) }
    top.next = num + 1
    if (String(num) !== m[3]) {
      const d = String(num).length - m[3].length
      if (i < row || (i === row && col > m[1].length)) shift += d
      lines[i] = m[1] + num + lines[i].slice(m[1].length + m[3].length)
    }
  }
  return {value: lines.join('\n'), caret: caret + shift}
}

function listEnter(value, start, end) {
  const [ls, le] = lineBounds(value, start)
  if (end > le) return null                               // 选中了好几行：按普通回车
  const line = value.slice(ls, le), m = line.match(LIST_LINE)
  if (!m) return null
  if (start - ls < line.length - m[7].length) return null  // 光标还在「- [ ] 」这些符号里：按普通回车
  if (!m[7].trim() && end === le) {                        // 空的一项再按回车：有缩进往外提一层，没缩进就退出列表
    const lead = m[1].match(/(\t| {1,2})$/)
    const replaced = lead ? line.slice(0, m[1].length - lead[0].length) + line.slice(m[1].length) : ''
    return renumberList(value.slice(0, ls) + replaced + value.slice(le), ls + replaced.length)
  }
  const marker = m[2] ? m[2] : (parseInt(m[3], 10) + 1) + m[4]
  const insert = '\n' + m[1] + marker + m[5] + (m[6] ? '[ ] ' : '')   // 待办接着出一个没做的 [ ]
  return renumberList(value.slice(0, start) + insert + value.slice(end), start + insert.length)
}

function listIndent(value, start, end, outdent) {
  const ls = lineBounds(value, start)[0]
  const le = lineBounds(value, end > start && value[end - 1] === '\n' ? end - 1 : end)[1]
  const lines = value.slice(ls, le).split('\n')
  if (!lines.some((line) => LIST_LINE.test(line))) return null   // 不在列表里：Tab 照常缩进
  let firstDelta = null, total = 0
  const changed = lines.map((line) => {
    let next = line
    if (outdent) { const lead = line.match(/^(\t| {1,2})/); if (lead) next = line.slice(lead[0].length) }
    else if (line.trim()) next = INDENT + line
    // 挪到别的层级的编号项先当成 1.：接在同层上一项后面会被重排成接着数，新开一层就从 1 开始
    const m = next !== line && next.match(LIST_LINE)
    if (m && m[3] !== undefined && m[3] !== '1') next = m[1] + '1' + next.slice(m[1].length + m[3].length)
    const d = next.length - line.length
    if (firstDelta === null) firstDelta = d
    total += d
    return next
  })
  const v = value.slice(0, ls) + changed.join('\n') + value.slice(le)
  const s = Math.max(ls, start + firstDelta), e = Math.max(s, end + total)
  const a = renumberList(v, s), b = renumberList(a.value, e + (a.value.length - v.length))
  return {value: b.value, start: a.caret, end: Math.max(a.caret, b.caret)}
}

// 上面几个函数给的是「整篇新全文」。直接整篇替换会把 widget 全部重建、撤销也变成一大步，
// 所以掐掉首尾相同的部分，只把真正变了的那一段发给 CM6
function applyText(view, next, sel) {
  const cur = view.state.doc.toString()
  if (next === cur) { if (sel) view.dispatch({selection: sel}); return }
  const max = Math.min(cur.length, next.length)
  let a = 0
  while (a < max && cur[a] === next[a]) a++
  let b = 0
  while (b < max - a && cur[cur.length - 1 - b] === next[next.length - 1 - b]) b++
  view.dispatch({
    changes: {from: a, to: cur.length - b, insert: next.slice(a, next.length - b)},
    selection: sel,
  })
}

const listOnEnter = (view) => {
  const sel = view.state.selection.main
  const r = listEnter(view.state.doc.toString(), sel.from, sel.to)
  if (!r) return false
  applyText(view, r.value, {anchor: r.caret})
  return true
}
const listOnTab = (outdent) => (view) => {
  const sel = view.state.selection.main
  const r = listIndent(view.state.doc.toString(), sel.from, sel.to, outdent)
  if (!r) return false
  applyText(view, r.value, {anchor: r.start, head: r.end})
  return true
}

// 删一项、粘进来几行之后，把光标所在的那个列表重新数一遍
let renumbering = false
const autoRenumber = EditorView.updateListener.of((u) => {
  if (!u.docChanged || renumbering) return
  const interesting = u.transactions.some((tr) =>
    tr.isUserEvent('delete') || tr.isUserEvent('input.paste') || tr.isUserEvent('input.drop'))
  if (!interesting) return
  const value = u.state.doc.toString(), caret = u.state.selection.main.head
  const r = renumberList(value, caret)
  if (r.value === value) return
  // CM6 不让在 updateListener 里同步派发，挪到微任务里
  queueMicrotask(() => {
    renumbering = true
    try { applyText(u.view, r.value, {anchor: r.caret}) } finally { renumbering = false }
  })
})

// ---------- 加粗 / 斜体这类快捷键 ----------
// CM6 默认没有这些，得自己接。选中了就把两头包上标记，已经包着的再按一次去掉；
// 没选中就插一对标记、光标落中间，接着打字就是粗的
const wrapWith = (marker) => (view) => {
  const len = marker.length
  view.dispatch(view.state.changeByRange((range) => {
    const {from, to} = range
    const doc = view.state.doc
    const inside = doc.sliceString(from, to)
    // 选中的就是「**文字**」：把里面那层扒掉
    if (inside.length >= 2 * len && inside.startsWith(marker) && inside.endsWith(marker)) {
      return {
        changes: {from, to, insert: inside.slice(len, inside.length - len)},
        range: EditorSelection.range(from, to - 2 * len),
      }
    }
    // 只选中了文字、标记在外面：「**[文字]**」
    const before = doc.sliceString(Math.max(0, from - len), from)
    const after = doc.sliceString(to, Math.min(doc.length, to + len))
    if (before === marker && after === marker) {
      return {
        changes: [{from: from - len, to: from}, {from: to, to: to + len}],
        range: EditorSelection.range(from - len, to - len),
      }
    }
    return {
      changes: {from, to, insert: marker + inside + marker},
      range: range.empty ? EditorSelection.cursor(from + len)
                         : EditorSelection.range(from + len, to + len),
    }
  }))
  return true
}

// 配色全部走 CSS 变量：明暗切换时变量变了样式就跟着变，编辑器不用重建
const theme = EditorView.theme({
  '&': {backgroundColor: 'var(--surface)', color: 'var(--ink)', fontSize: '14.5px'},
  '.cm-content': {padding: '22px 26px', lineHeight: '1.75', caretColor: 'var(--primary)',
    fontFamily: 'inherit'},
  '.cm-scroller': {fontFamily: 'inherit'},
  '&.cm-focused': {outline: 'none'},
  '.cm-line': {padding: '1px 0', position: 'relative'},
  '.cm-activeLine': {backgroundColor: 'var(--ed-active)'},
  '.cm-selectionBackground, ::selection': {backgroundColor: 'var(--ed-sel) !important'},
  '.cm-cursor': {borderLeftColor: 'var(--primary)'},
  '.md-h': {fontWeight: '700', color: 'var(--ink)'},
  '.md-h1': {fontSize: '1.7em', lineHeight: '1.3'},
  '.md-h2': {fontSize: '1.4em', lineHeight: '1.35'},
  '.md-h3': {fontSize: '1.18em'},
  '.md-bullet': {color: 'var(--violet-ink)', paddingRight: '8px'},
  '.md-num': {color: 'var(--muted)', paddingRight: '8px', fontVariantNumeric: 'tabular-nums'},
  '.md-checkbox': {display: 'inline-grid', placeItems: 'center', width: '14px', height: '14px',
    marginRight: '8px', borderRadius: '3px', border: '1.5px solid var(--faint)', fontSize: '9px',
    color: 'transparent', verticalAlign: '-2px', cursor: 'pointer'},
  '.md-checkbox.on': {background: 'var(--primary)', borderColor: 'var(--primary)', color: '#fff'},
  '.md-task-done': {color: 'var(--muted)', textDecoration: 'line-through'},
  '.md-task-done .md-link': {color: 'var(--muted)'},
  '.md-quote': {borderLeft: '3px solid var(--line)', paddingLeft: '12px', color: 'var(--ink-soft)'},
  '.md-hr': {display: 'inline-block', width: '100%', borderTop: '1px solid var(--line)',
    verticalAlign: 'middle'},
  '.md-link': {color: 'var(--link)', textDecoration: 'underline', cursor: 'pointer'},
  '.md-img img': {maxWidth: '100%', borderRadius: '6px', display: 'block', margin: '6px 0'},
  '.md-table': {borderCollapse: 'collapse', margin: '6px 0', fontSize: '.94em', tableLayout: 'fixed'},
  '.md-table th, .md-table td': {border: '1px solid var(--line)', padding: '6px 12px', textAlign: 'left',
    position: 'relative', verticalAlign: 'top', overflow: 'hidden', textOverflow: 'ellipsis'},
  '.md-table th': {background: 'var(--surface-soft)', fontWeight: '600'},
  '.md-table th:focus, .md-table td:focus': {outline: '2px solid var(--primary)', outlineOffset: '-2px'},
  // 拖列宽 / 拖行高的把手：鼠标进表格才显出来
  '.md-col-grip': {position: 'absolute', top: '0', bottom: '0', right: '-4px', width: '8px',
    cursor: 'col-resize', userSelect: 'none'},
  '.md-row-grip': {position: 'absolute', left: '0', right: '0', bottom: '-4px', height: '8px',
    cursor: 'row-resize', userSelect: 'none'},
  '.md-col-grip::after': {content: '""', position: 'absolute', top: '3px', bottom: '3px', left: '3px',
    width: '2px', borderRadius: '1px', background: 'var(--primary)', opacity: '0',
    transition: 'opacity .12s'},
  '.md-row-grip::after': {content: '""', position: 'absolute', left: '3px', right: '3px', top: '3px',
    height: '2px', borderRadius: '1px', background: 'var(--primary)', opacity: '0',
    transition: 'opacity .12s'},
  '.md-table:hover .md-col-grip::after, .md-table:hover .md-row-grip::after': {opacity: '.35'},
  '.md-col-grip:hover::after, .md-row-grip:hover::after': {opacity: '1'},
  '.md-table.sizing': {userSelect: 'none'},
  '.md-indent-guides': {position: 'absolute', inset: '0', pointerEvents: 'none'},
  '.md-indent-guide': {position: 'absolute', top: '-1px', bottom: '-1px', width: '1px',
    background: 'color-mix(in srgb, var(--muted) 32%, transparent)'},
  '.md-fold': {display: 'inline-block', width: '12px', marginLeft: '-20px', marginRight: '8px',
    color: 'var(--muted)', cursor: 'pointer', opacity: '0', transition: 'opacity .12s',
    fontSize: '15px', lineHeight: '1.2', verticalAlign: '-1px'},
  '.cm-line:hover .md-fold, .md-fold.folded': {opacity: '1'},
  '.md-strong': {fontWeight: '700'},        // 不定颜色：链接里的加粗要保持链接色
  '.md-em': {fontStyle: 'italic'},
  '.md-strike': {textDecoration: 'line-through', color: 'var(--muted)'},
  '.md-code': {fontFamily: 'ui-monospace,SFMono-Regular,Menlo,monospace',
    background: 'var(--ed-code-bg)', borderRadius: '4px', padding: '1px 5px', fontSize: '.88em',
    color: 'var(--ed-code-ink)'},
  '.md-code-line': {fontFamily: 'ui-monospace,SFMono-Regular,Menlo,monospace',
    background: 'var(--ed-code-bg)', fontSize: '.92em', paddingLeft: '14px', paddingRight: '14px'},
  '.md-code-top': {paddingTop: '10px', borderRadius: '8px 8px 0 0'},
  '.md-code-bottom': {paddingBottom: '10px', borderRadius: '0 0 8px 8px'},
  '.md-img': {position: 'relative', display: 'inline-block'},
  '.md-img-grip': {position: 'absolute', right: '-7px', bottom: '-7px', width: '18px', height: '18px',
    borderRadius: '5px', background: 'var(--primary)', border: '2px solid var(--surface)',
    cursor: 'nwse-resize', opacity: '.65', boxShadow: '0 1px 4px rgba(0,0,0,.28)',
    transition: 'opacity .12s, transform .12s'},
  '.md-img:hover .md-img-grip': {opacity: '1', transform: 'scale(1.12)'},
  '.md-img-grip::after': {content: '""', position: 'absolute', inset: '-8px'},
  '.md-img-size': {position: 'absolute', right: '6px', top: '6px', padding: '2px 7px',
    borderRadius: '5px', background: 'rgba(20,18,28,.86)', color: '#fff', fontSize: '11px',
    fontWeight: '700', fontVariantNumeric: 'tabular-nums', pointerEvents: 'none', opacity: '0',
    transition: 'opacity .12s'},
  '.md-img.resizing .md-img-size': {opacity: '1'},
  '.cm-foldPlaceholder': {background: 'var(--surface-soft)', border: '0', color: 'var(--muted)',
    borderRadius: '4px', padding: '0 6px'},
})

const codeHighlight = HighlightStyle.define([
  {tag: t.keyword, color: 'var(--ed-kw)'},
  {tag: [t.string, t.special(t.string)], color: 'var(--ed-str)'},
  {tag: t.comment, color: 'var(--muted)', fontStyle: 'italic'},
  {tag: [t.number, t.bool, t.null], color: 'var(--ed-num)'},
  {tag: [t.function(t.variableName), t.definition(t.variableName)], color: 'var(--ed-fn)'},
  {tag: t.typeName, color: 'var(--ed-type)'},
  {tag: t.operator, color: 'var(--ed-op)'},
])

// ---------- 对外接口 ----------
// 页面只认这四个方法；内部换实现不影响调用方
let view = null
let onImages = null

const imageFiles = (list) => [...(list || [])].filter((f) => f && f.type.startsWith('image/'))

// 贴图：粘贴和拖入都交给页面去传，传完把 Markdown 插回光标处
const imageDrop = EditorView.domEventHandlers({
  paste(event) {
    const files = imageFiles(event.clipboardData && event.clipboardData.files)
    if (!files.length || !onImages) return false
    event.preventDefault(); onImages(files); return true
  },
  dragover(event) {
    if (event.dataTransfer && [...event.dataTransfer.types].includes('Files')) event.preventDefault()
    return false
  },
  drop(event) {
    const files = imageFiles(event.dataTransfer && event.dataTransfer.files)
    if (!files.length || !onImages) return false
    event.preventDefault(); onImages(files); return true
  },
})

const flushTables = () => {
  if (!view) return
  view.dom.querySelectorAll('.md-table').forEach((t) => { if (t.__commit) t.__commit() })
}

window.FechoEditor = {
  create(root, doc, opts = {}) {
    this.destroy()
    onImages = opts.onImages || null
    view = new EditorView({
      parent: root,
      state: EditorState.create({
        doc: doc || '',
        extensions: [
          history(), drawSelection(), highlightActiveLine(),
          // markdown() 自带一套 Prec.high 的回车绑定（insertNewlineContinueMarkup），
          // 规则和我们要的 Obsidian 行为不一样，所以这里必须用 highest 压过它
          Prec.highest(keymap.of([
            {key: 'Mod-b', run: wrapWith('**')},                 // 加粗
            {key: 'Mod-i', run: wrapWith('*')},                  // 斜体
            {key: 'Mod-e', run: wrapWith('`')},                  // 行内代码
            {key: 'Mod-Shift-x', run: wrapWith('~~')},           // 删除线
            {key: 'Enter', run: listOnEnter},                    // 列表行回车自动接着写
            {key: 'Tab', run: listOnTab(false), shift: listOnTab(true)},
            {key: 'Tab', run: indentMore, shift: indentLess},    // 不在列表里就是普通缩进
          ])),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          indentUnit.of('  '),
          markdown({base: markdownLanguage}),
          syntaxHighlighting(codeHighlight),
          foldRule, tableField, livePreview, clicks, theme, imageDrop, autoRenumber,
          EditorView.lineWrapping,
        ],
      }),
    })
    return view
  },
  destroy() {
    if (view) { view.destroy(); view = null }
    onImages = null
  },
  focus() { if (view) view.focus() },
  // 取原文前先把表格里还没失焦的改动写回去
  value() { flushTables(); return view ? view.state.doc.toString() : '' },
  // 插进来的东西要自己占一行，两头都补上换行，免得和上下文黏成一行
  insert(text) {
    if (!view) return
    const at = view.state.selection.main
    const doc = view.state.doc
    const before = at.from > 0 ? doc.sliceString(at.from - 1, at.from) : '\n'
    const after = at.to < doc.length ? doc.sliceString(at.to, at.to + 1) : '\n'
    const head = before === '\n' ? '' : '\n'
    const tail = after === '\n' ? '' : '\n'
    view.dispatch({changes: {from: at.from, to: at.to, insert: head + text + tail},
                   selection: {anchor: at.from + head.length + text.length}})
    view.focus()
  },
}
