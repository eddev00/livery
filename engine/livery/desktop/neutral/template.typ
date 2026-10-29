// Livery neutral template (desktop). All styling lives here, in theme.toml and in
// the design layer; content.typ is plain pandoc output and never carries styling.
//
// Design levels, applied in this order (later wins):
//   1. project theme      theme.toml beside this file
//   2. named treatment    design.treatments.<name>, assigned to an element by ID
//   3. element override   design.overrides.<element ID>
//
// sys.inputs understood:
//   theme  = root-relative path to the theme (default: theme.toml beside this file)
//   design = root-relative path to the document's resolved design (treatments, assignments,
//            overrides and table shapes for this document); absent = theme only
//   verify = "true" for the bare render the content verifier reads: no header, footer or
//            title block, hyphenation and ligatures off, wide word spacing. Design rules
//            still apply, so a design that hides content fails the text check.
//
// Element markers: the converter brackets every block with `#metadata("<id>") <we-s>` and
// `#metadata("<id>") <we-e>`. They are tracked in a state so show rules know which
// element they are styling, and their positions are published as <we-map> (the
// selection map). Neither changes the layout or the text.

#import "custom.typ": custom-rules

#let _theme() = toml(sys.inputs.at("theme", default: "theme.toml"))
#let _design() = {
  let p = sys.inputs.at("design", default: "")
  let d = if p == "" { (:) } else { json(p) }
  (
    treatments: d.at("treatments", default: (:)),
    assign: d.at("assign", default: (:)),
    overrides: d.at("overrides", default: (:)),
    tables: d.at("tables", default: (:)),
  )
}

// Lengths are strings ("10pt") or numbers (points). Validated before they get here.
#let _len(v) = if type(v) == str { eval(v) } else { v * 1pt }
#let _color(t, s) = if s == none or s == "" { none } else if s.starts-with("#") { rgb(s) } else { rgb(t.colors.at(s)) }
#let _align(s) = (left: left, center: center, right: right, justify: left).at(s, default: left)

#let _cur = state("we-current-element", none)

// The style of one element: theme base, then its treatment, then its override.
#let _resolve(d, kind, id, base) = {
  let st = base
  if id != none {
    let name = d.assign.at(id, default: none)
    if name != none {
      let tr = d.treatments.at(name, default: none)
      if tr != none and tr.at("kind", default: kind) == kind { st = st + tr.at("style", default: (:)) }
    }
    st = st + d.overrides.at(id, default: (:))
  }
  st
}

// Element IDs start with their kind: h heading, p paragraph, t table, l list, f figure.
#let _is(id, prefix) = id != none and id.starts-with(prefix)

#let project(
  title: "",
  department: none,
  department_name: none,
  version: none,
  number: none,
  cover: none,
  body,
) = {
  let t = _theme()
  let d = _design()
  let verify = sys.inputs.at("verify", default: "false") == "true"
  let C(s) = _color(t, s)

  let has-elements(prefix) = (
    d.assign.keys().any(k => k.starts-with(prefix)) or d.overrides.keys().any(k => k.starts-with(prefix))
  )
  let styled-paragraphs = has-elements("p")

  set document(title: title)

  // ------------------------------------------------------------------ page chrome
  let chrome(cfg, extra) = {
    set text(font: t.fonts.heading, size: _len(cfg.size), fill: C(cfg.color))
    let label = cfg.text.replace("{title}", title)
    align(_align(cfg.align), extra(label))
  }
  set page(
    paper: t.page.paper,
    margin: (
      top: _len(t.page.margin_top),
      bottom: _len(t.page.margin_bottom),
      left: _len(t.page.margin_left),
      right: _len(t.page.margin_right),
    ),
    header: if verify or not t.header.enabled { none } else {
      context {
        if counter(page).get().first() > 1 {
          chrome(t.header, label => {
            label
            if t.header.rule { v(-0.4em); line(length: 100%, stroke: 0.4pt + C("rule")) }
          })
        }
      }
    },
    footer: if verify or not t.footer.enabled { none } else {
      chrome(t.footer, label => {
        let both = t.footer.number_format.matches(regex("[1aAiI]")).len() > 1
        let num = if t.footer.page_numbers {
          context counter(page).display(t.footer.number_format, both: both)
        } else { [] }
        if label == "" { num } else { grid(columns: (1fr, auto), align: (left, right), label, num) }
      })
    },
  )

  // ------------------------------------------------------------------- text
  let b = t.text.body
  set text(
    font: t.fonts.body,
    size: _len(b.size),
    weight: b.weight,
    fill: C(b.color),
    lang: t.typography.lang,
    hyphenate: if verify { false } else { t.typography.hyphenate },
    ligatures: if verify { false } else { t.typography.ligatures },
    spacing: if verify { 250% } else { 100% },
  )
  set par(
    justify: if verify { false } else { t.typography.justify },
    leading: _len(b.leading),
    spacing: _len(b.leading) + _len(b.space_after),
    first-line-indent: _len(t.typography.first_line_indent),
  )
  show raw: set text(font: t.fonts.mono)

  // Track the element being laid out. The marker itself stays in the document so its
  // position can be queried for the selection map.
  show <we-s>: it => { it; _cur.update(it.value) }
  show <we-e>: it => { it; _cur.update(none) }

  // ---------------------------------------------------------------- headings
  set heading(numbering: if verify or t.headings.numbering == "" { none } else { t.headings.numbering })
  show heading: it => context {
    let id = _cur.get()
    let key = "h" + str(calc.min(it.level, 4))
    let st = _resolve(d, "heading", if _is(id, "h") { id } else { none }, t.text.at(key))
    set text(
      font: st.at("font", default: t.fonts.heading),
      size: _len(st.size),
      weight: st.weight,
      fill: C(st.color),
      style: if st.at("italic", default: false) { "italic" } else { "normal" },
      hyphenate: false,
    )
    set align(_align(st.at("align", default: "left")))
    let inner = if st.at("upper", default: false) { upper(it) } else { it }
    block(above: _len(st.space_before), below: _len(st.space_after), sticky: true, inner)
  }

  // -------------------------------------------------------------- paragraphs
  // Only when the design styles single paragraphs: the lookup costs a context per paragraph.
  show par: it => if not styled-paragraphs { it } else {
    context {
      let id = _cur.get()
      if not _is(id, "p") { return it }
      let st = _resolve(d, "paragraph", id, (:))
      if st.len() == 0 { return it }
      set text(
        font: st.at("font", default: t.fonts.body),
        size: _len(st.at("size", default: b.size)),
        weight: st.at("weight", default: b.weight),
        fill: C(st.at("color", default: b.color)),
        style: if st.at("italic", default: false) { "italic" } else { "normal" },
      )
      set align(_align(st.at("align", default: "left")))
      block(
        above: _len(st.at("space_before", default: b.leading + " + " + b.space_after)),
        below: _len(st.at("space_after", default: b.leading + " + " + b.space_after)),
        it,
      )
    }
  }

  // ------------------------------------------------------------------ tables
  // A table is rebuilt once with its resolved style; the label stops the rebuild recursing.
  show table: it => {
    if it.has("label") and it.label == <we-styled> { return it }
    context {
      let id = _cur.get()
      let tid = if _is(id, "t") { id } else { none }
      let st = _resolve(d, "table", tid, t.tables)
      let shape = if tid == none { (:) } else { d.tables.at(tid, default: (:)) }
      let hr = shape.at("header_rows", default: 1)
      let f = it.fields()
      let kids = f.remove("children")
      let _ = f.remove("label", default: none)
      let ncols = if type(f.at("columns", default: auto)) == array { f.columns.len() } else { none }
      let cols = st.at("columns", default: none)
      if cols != none and ncols != none and cols.len() == ncols {
        f.columns = cols.map(c => c * 1fr)
      }
      let s = _len(st.stroke_width) + C(st.stroke_color)
      f.stroke = (
        grid: s,
        horizontal: (x: none, y: s),
        header: (x, y) => if y == hr - 1 { (bottom: s) } else { none },
        "none": none,
      ).at(st.rules, default: s)
      let hf = C(st.header_fill)
      let bf = C(st.at("body_fill", default: ""))
      let sf = C(st.at("stripe_fill", default: ""))
      f.fill = (x, y) => if y < hr { hf } else if sf != none and calc.odd(y - hr) { sf } else { bf }
      f.inset = _len(st.inset)
      f.align = _align(st.at("align", default: "left"))
      // Cells are narrow: a long word must break (hyphenated) rather than run into the next column.
      // The verify render keeps words whole: its text layer is compared word for word.
      set text(size: _len(st.font_size), hyphenate: not verify)
      show table.cell: c => if c.y < hr {
        set text(fill: C(st.header_color), weight: st.header_weight)
        set align(_align(st.at("header_align", default: st.at("align", default: "left"))))
        c
      } else { c }
      [#table(..f, ..kids) <we-styled>]
    }
  }

  // ------------------------------------------------- figures, lists, notes
  // Numbering, markers and supplements are chrome: the verify render keeps only the text
  // (the same contract as the engine's own template).
  set image(width: 100%)
  show figure.where(kind: image): set figure(supplement: none, numbering: none)
  show figure.where(kind: table): set block(breakable: true)
  show figure.caption: set text(size: _len(t.tables.font_size), fill: C("muted"))
  show figure.caption: it => if verify { it.body } else { it }
  show list.item: it => if verify { block(it.body) } else { it }
  show enum.item: it => if verify { block(it.body) } else { it }
  show footnote: it => if verify { [ #it.body ] } else { it }
  show footnote.entry: it => if verify { [] } else { it }
  set table.header(repeat: not verify)
  show super: it => if verify { it.body } else { it }
  show sub: it => if verify { it.body } else { it }

  // --------------------------------------------------------------- title block
  if not verify and t.title.enabled and title != "" {
    block(below: _len(t.title.space_after), {
      set text(font: t.fonts.heading, size: _len(t.title.size), weight: t.title.weight, fill: C(t.title.color))
      set par(justify: false)
      title
    })
  }

  show: custom-rules
  // Page chrome a custom design adds (a running chapter title, say) is not content: drop it
  // from the verify render again. This rule sits inside custom-rules, so it wins.
  show: it => if verify { set page(header: none, footer: none, background: none, foreground: none); it } else { it }
  body

  // ------------------------------------------------------------ selection map
  // Every marker's page and position, plus the text area, for the preview overlay.
  if not verify {
    context {
      let pos(m) = {
        let p = m.location().position()
        (id: m.value, page: p.page, x: p.x.pt(), y: p.y.pt())
      }
      [#metadata((
        starts: query(<we-s>).map(pos),
        ends: query(<we-e>).map(pos),
        page: (width: page.width.pt(), height: page.height.pt()),
        area: (
          left: _len(t.page.margin_left).pt(),
          top: _len(t.page.margin_top).pt(),
          right: page.width.pt() - _len(t.page.margin_right).pt(),
          bottom: page.height.pt() - _len(t.page.margin_bottom).pt(),
        ),
        pages: counter(page).final().first(),
      )) <we-map>]
    }
  }
}
